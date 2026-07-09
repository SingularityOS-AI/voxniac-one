"""
cascade.py — Transport-agnostic ASR -> LLM -> TTS streaming cascade.

CascadeSession wires together three decoupled asyncio tasks around a
transport (BrowserTransport or TwilioTransport, see transports.py, duck-typed
as: async send_audio(bytes), async send_event(dict), async clear_audio()):

  Task A (_audio_in_worker):  feed_audio() -> _audio_in_queue -> Deepgram live
                               STT (vz_stt_live.LiveSTT), or the batch ASR
                               fallback loop if the live socket never connected.
  Task B (_turn_worker):      STT final transcript -> _final_queue -> Fireworks
                               LLM SSE (vz_llm.stream_chat) -> sentence splitter
                               -> Deepgram live TTS (vz_tts_live.LiveTTS) per
                               sentence, or the REST TTS fallback per sentence.
  Task C (_tts_forward_worker): TTS audio chunks -> tts.audio_queue -> transport
                               out, tracking per-turn TTFA/E2E metrics.

Barge-in: when Deepgram STT reports SpeechStarted while the agent is busy
(LLM generating or TTS still playing), the in-flight turn task is cancelled,
the LLM SSE thread is told to stop via a threading.Event, the live TTS buffer
is cleared, the transport is told to flush its playback, and a
{"type":"barge_in"} event is sent.

Every provider failure is sent to the transport as a {"type":"error",...}
event and the session keeps running — a failed turn never kills the call.
"""

import asyncio
import audioop
import io
import logging
import re
import threading
import time
import wave

from vz_asr import transcribe
from vz_llm import MODELOS_LLM, ConversationHistory, stream_chat
from vz_logger import log_turn
from vz_stt_live import LiveSTT, LiveSTTError
from vz_tts import sintetizar
from vz_tts_live import LiveTTS, LiveTTSError

logger = logging.getLogger("voxniac_one.cascade")

# ---------------------------------------------------------------------------
# Error classification + retry (ported from the previous server.py; used here
# for the batch/REST fallback provider calls).
# ---------------------------------------------------------------------------
_AUTH_ERROR_MARKERS = ("401", "402", "403", "payment", "credit", "balance", "unauthorized")
_NETWORK_ERROR_MARKERS = ("timeout", "connect", "network", "unreachable", "reset", "refused", "ssl", "dns")
RETRY_BACKOFF_S = 0.5


def classify_error(exc: Exception) -> str:
    """Classifies a provider exception as 'auth', 'network', or 'unknown'."""
    text = str(exc).lower()
    if any(marker in text for marker in _AUTH_ERROR_MARKERS):
        return "auth"
    if any(marker in text for marker in _NETWORK_ERROR_MARKERS):
        return "network"
    return "unknown"


def is_network_error(exc: Exception) -> bool:
    return classify_error(exc) == "network"


def call_with_retry(func, *args, **kwargs):
    """Runs func(*args, **kwargs); retries once after RETRY_BACKOFF_S on a network error."""
    try:
        return func(*args, **kwargs)
    except Exception as exc:
        if is_network_error(exc):
            time.sleep(RETRY_BACKOFF_S)
            return func(*args, **kwargs)
        raise


# ---------------------------------------------------------------------------
# Sentence splitter (ported from the previous server.py's _extract_sentences).
# Each completed sentence (boundary at . ! ?) is synthesized as soon as it's
# formed, without waiting for the full LLM response. If the buffer grows past
# FORCE_FLUSH_CHARS with no punctuation, it's force-flushed so latency never
# piles up on a single long, unpunctuated stretch of text.
# ---------------------------------------------------------------------------
_SENTENCE_END = re.compile(r"[.!?]")
FORCE_FLUSH_CHARS = 180


def _extract_sentences(buffer: str, force: bool = False):
    """Returns (complete_sentences, remainder). force=True drains the remainder too."""
    sentences = []
    while True:
        m = _SENTENCE_END.search(buffer)
        if not m:
            break
        end = m.end()
        sentence = buffer[:end].strip()
        if sentence:
            sentences.append(sentence)
        buffer = buffer[end:]
    if force and buffer.strip():
        sentences.append(buffer.strip())
        buffer = ""
    return sentences, buffer


def _pcm_chunks_to_wav(chunks, encoding: str, sample_rate: int) -> bytes:
    """Concatenates raw audio chunks into one WAV byte string for logging/recording.
    mulaw chunks are decoded to 16-bit PCM first (the wave module only writes PCM)."""
    if not chunks:
        return b""
    raw = b"".join(chunks)
    if encoding == "mulaw":
        try:
            raw = audioop.ulaw2lin(raw, 2)
        except audioop.error:
            return b""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(raw)
    return buf.getvalue()


# Batch STT fallback tuning: how long a silence gap implies "utterance done".
FALLBACK_SILENCE_S = 0.9
FALLBACK_MIN_UTTER_S = 0.3
FALLBACK_ASR_ENGINE = "groq_whisper_large_v3_turbo"
FALLBACK_TTS_ENGINE = "aura2"


class CascadeSession:
    """One live call's ASR -> LLM -> TTS pipeline, bound to a transport.

    transport duck-type: async send_audio(bytes), async send_event(dict),
    async clear_audio().
    """

    def __init__(
        self, transport, stt_cfg: dict, llm_cfg: dict, tts_cfg: dict, profile: dict,
        call_id: "str | None" = None, channel: str = "unknown",
    ):
        self.transport = transport
        self.stt_cfg = stt_cfg  # {"encoding": "linear16"|"mulaw", "sample_rate": int}
        self.llm_cfg = dict(llm_cfg)  # {"model_id": str}
        self.tts_cfg = tts_cfg  # {"encoding": "linear16"|"mulaw", "sample_rate": int}
        self.profile = profile  # agent_profile.json contents (bulletproof-loaded)
        # Phase 3.5 P1: identifies this call in voxniac_one_log.jsonl (passed
        # through to log_turn() below) and in event_bus fan-out envelopes.
        # Optional/defaulted so existing callers that don't pass them keep
        # working unchanged.
        self.call_id = call_id
        self.channel = channel

        self.history = ConversationHistory()

        self.stt = LiveSTT(
            on_partial=self._on_stt_partial,
            on_final=self._on_stt_final,
            on_speech_started=self._on_speech_started,
            on_error=self._on_stt_error,
        )
        self.tts = LiveTTS(
            on_flushed=self._on_tts_flushed,
            on_error=self._on_tts_error,
        )

        self._stt_live_available = True
        self._tts_live_available = True

        self._audio_in_queue: asyncio.Queue = asyncio.Queue()
        self._final_queue: asyncio.Queue = asyncio.Queue()

        self._tasks = []
        self._current_turn_task = None
        self._current_stop_event = None

        self._agent_busy = False
        self._t_speech_started = None
        self._utterance_parts = []

        self._pending_flushes = 0
        self._all_flushed_event = asyncio.Event()
        self._all_flushed_event.set()

        self._turn_metrics = None
        self._turn_audio_chunks = []

        # Batch STT fallback state (only used if the live socket never connected).
        self._fallback_buf = bytearray()
        self._fallback_last_chunk_ts = None

        self._stopped = False
        self._loop = asyncio.get_event_loop()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def start(self):
        """Connects STT/TTS live sockets, starts the pipeline tasks, and speaks the opening."""
        self._agent_busy = True

        try:
            await self.tts.connect(
                encoding=self.tts_cfg["encoding"],
                sample_rate=self.tts_cfg["sample_rate"],
                voice=self.profile.get("voice", "aura-2-thalia-en"),
            )
        except LiveTTSError as exc:
            logger.error("start: live TTS connect failed, falling back to REST TTS: %s", exc)
            self._tts_live_available = False
            await self._send_error("tts", exc)

        try:
            await self.stt.connect(
                encoding=self.stt_cfg["encoding"],
                sample_rate=self.stt_cfg["sample_rate"],
            )
        except LiveSTTError as exc:
            logger.error("start: live STT connect failed, falling back to batch STT: %s", exc)
            self._stt_live_available = False
            await self._send_error("stt", exc)

        self._tasks.append(asyncio.create_task(self._audio_in_worker()))
        self._tasks.append(asyncio.create_task(self._tts_forward_worker()))
        self._tasks.append(asyncio.create_task(self._turn_worker()))
        if not self._stt_live_available:
            self._tasks.append(asyncio.create_task(self._fallback_stt_loop()))

        opening = self.profile.get("agent_opening", "")
        if opening:
            self.history.add_assistant(opening)
            sentences, _ = _extract_sentences(opening, force=True)
            for sentence in sentences:
                await self._speak_sentence(sentence)
            if self._pending_flushes > 0:
                try:
                    await asyncio.wait_for(self._all_flushed_event.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    logger.error("start: timed out waiting for the opening line to finish playing")

        self._agent_busy = False

    async def feed_audio(self, chunk: bytes):
        """Feeds one chunk of raw inbound audio (already in stt_cfg's encoding/sample_rate)."""
        if self._stopped or not chunk:
            return
        await self._audio_in_queue.put(chunk)

    async def stop(self):
        """Tears down the session: cancels tasks and closes both live sockets."""
        if self._stopped:
            return
        self._stopped = True
        if self._current_turn_task is not None and not self._current_turn_task.done():
            self._current_turn_task.cancel()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await self.stt.close()
        except Exception as exc:
            logger.error("stop: error closing live STT: %s", exc)
        try:
            await self.tts.close()
        except Exception as exc:
            logger.error("stop: error closing live TTS: %s", exc)

    # ------------------------------------------------------------------
    # Task A: inbound audio -> live STT (or batch fallback buffer)
    # ------------------------------------------------------------------
    async def _audio_in_worker(self):
        try:
            while True:
                chunk = await self._audio_in_queue.get()
                if self._stt_live_available:
                    try:
                        await self.stt.send_audio(chunk)
                    except LiveSTTError as exc:
                        logger.error("audio_in: live STT send failed, falling back to batch STT: %s", exc)
                        self._stt_live_available = False
                        await self._send_error("stt", exc)
                        self._tasks.append(asyncio.create_task(self._fallback_stt_loop()))
                        self._fallback_buf.extend(chunk)
                        self._fallback_last_chunk_ts = time.time()
                else:
                    self._fallback_buf.extend(chunk)
                    self._fallback_last_chunk_ts = time.time()
        except asyncio.CancelledError:
            pass

    async def _fallback_stt_loop(self):
        """Batch STT fallback: buffers PCM until a short silence gap, then transcribes
        via vz_asr (Groq Whisper) in a background thread. Only runs when the live
        Deepgram STT WebSocket failed to connect or dropped mid-call."""
        try:
            while not self._stopped:
                await asyncio.sleep(0.2)
                if not self._fallback_buf or self._fallback_last_chunk_ts is None:
                    continue
                gap = time.time() - self._fallback_last_chunk_ts
                sample_width = 2 if self.stt_cfg["encoding"] != "mulaw" else 1
                duration_s = len(self._fallback_buf) / (sample_width * self.stt_cfg["sample_rate"])
                if gap >= FALLBACK_SILENCE_S and duration_s >= FALLBACK_MIN_UTTER_S:
                    pcm_bytes = bytes(self._fallback_buf)
                    self._fallback_buf = bytearray()
                    t_final = time.time()
                    t_started = t_final - gap - duration_s
                    self._fallback_last_chunk_ts = None
                    asyncio.create_task(self._run_fallback_transcription(pcm_bytes, t_started, t_final))
        except asyncio.CancelledError:
            pass

    async def _run_fallback_transcription(self, pcm_bytes: bytes, t_started: float, t_final: float):
        wav_bytes = _pcm_chunks_to_wav([pcm_bytes], self.stt_cfg["encoding"], self.stt_cfg["sample_rate"])
        if not wav_bytes:
            return
        try:
            text, _lat = await asyncio.to_thread(
                call_with_retry, transcribe, FALLBACK_ASR_ENGINE, wav_bytes
            )
        except Exception as exc:
            logger.error("stt_fallback: batch transcription failed: %s", exc)
            await self._send_error("stt", exc)
            return
        text = (text or "").strip()
        if text:
            await self.transport.send_event({"type": "stt_partial", "text": text})
            self._final_queue.put_nowait((text, t_started, t_final))

    # ------------------------------------------------------------------
    # STT callbacks (invoked synchronously from LiveSTT's receive-loop task)
    # ------------------------------------------------------------------
    def _on_stt_partial(self, text: str):
        asyncio.create_task(self.transport.send_event({"type": "stt_partial", "text": text}))

    def _on_stt_final(self, text: str, speech_final: bool):
        text = (text or "").strip()
        if text:
            self._utterance_parts.append(text)
        if speech_final:
            full_text = " ".join(self._utterance_parts).strip()
            self._utterance_parts = []
            if full_text:
                t_final = time.time()
                t_started = self._t_speech_started or t_final
                self._final_queue.put_nowait((full_text, t_started, t_final))

    def _on_speech_started(self):
        self._t_speech_started = time.time()
        if self._agent_busy:
            asyncio.create_task(self._handle_barge_in())

    def _on_stt_error(self, exc: Exception):
        logger.error("stt_error: %s", exc)
        self._stt_live_available = False
        asyncio.create_task(self._send_error("stt", exc))
        if not any(getattr(t, "_is_fallback_stt", False) for t in self._tasks):
            fallback_task = asyncio.create_task(self._fallback_stt_loop())
            fallback_task._is_fallback_stt = True
            self._tasks.append(fallback_task)

    def _on_tts_error(self, exc: Exception):
        logger.error("tts_error: %s", exc)
        self._tts_live_available = False
        asyncio.create_task(self._send_error("tts", exc))

    def _on_tts_flushed(self):
        self._pending_flushes = max(0, self._pending_flushes - 1)
        if self._pending_flushes == 0:
            self._all_flushed_event.set()

    # ------------------------------------------------------------------
    # Barge-in
    # ------------------------------------------------------------------
    async def _handle_barge_in(self):
        if not self._agent_busy:
            return
        self._agent_busy = False
        logger.info("barge_in: user speech detected while the agent was busy, cancelling turn")

        if self._current_turn_task is not None and not self._current_turn_task.done():
            self._current_turn_task.cancel()
        if self._current_stop_event is not None:
            self._current_stop_event.set()

        try:
            await self.tts.clear()
        except Exception as exc:
            logger.error("barge_in: tts.clear() failed: %s", exc)

        while not self.tts.audio_queue.empty():
            try:
                self.tts.audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        self._pending_flushes = 0
        self._all_flushed_event.set()

        try:
            await self.transport.clear_audio()
        except Exception as exc:
            logger.error("barge_in: transport.clear_audio() failed: %s", exc)

        await self.transport.send_event({"type": "barge_in"})

    # ------------------------------------------------------------------
    # Task C: TTS audio out -> transport
    # ------------------------------------------------------------------
    async def _tts_forward_worker(self):
        try:
            while True:
                chunk = await self.tts.audio_queue.get()
                if self._turn_metrics is not None and self._turn_metrics["waiting_first_audio"]:
                    self._turn_metrics["ttfa_s"] = round(time.time() - self._turn_metrics["t0"], 3)
                try:
                    await self.transport.send_audio(chunk)
                except Exception as exc:
                    logger.error("transport_out: failed to send audio chunk: %s", exc)
                    continue
                self._turn_audio_chunks.append(chunk)
                if self._turn_metrics is not None and self._turn_metrics["waiting_first_audio"]:
                    self._turn_metrics["e2e_s"] = round(time.time() - self._turn_metrics["t0"], 3)
                    self._turn_metrics["waiting_first_audio"] = False
                    self._turn_metrics["first_audio_event"].set()
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Sentence -> TTS (live, with REST fallback)
    # ------------------------------------------------------------------
    async def _speak_sentence(self, sentence: str):
        sentence = sentence.strip()
        if not sentence:
            return
        self._pending_flushes += 1
        self._all_flushed_event.clear()

        if not self._tts_live_available:
            await self._speak_sentence_fallback(sentence)
            return

        try:
            await self.tts.speak(sentence)
        except LiveTTSError as exc:
            logger.error("tts_speak: live TTS send failed, falling back to REST TTS: %s", exc)
            self._tts_live_available = False
            await self._send_error("tts", exc)
            await self._speak_sentence_fallback(sentence)

    async def _speak_sentence_fallback(self, sentence: str):
        """REST Aura-2 fallback: synthesize the whole sentence, strip the WAV header,
        and forward the raw PCM directly (bypassing the live audio_queue)."""
        try:
            wav_bytes, _lat = await asyncio.to_thread(
                call_with_retry, sintetizar, FALLBACK_TTS_ENGINE, sentence
            )
        except Exception as exc:
            logger.error("tts_fallback: REST synthesis failed: %s", exc)
            await self._send_error("tts", exc)
            self._pending_flushes = max(0, self._pending_flushes - 1)
            if self._pending_flushes == 0:
                self._all_flushed_event.set()
            return

        try:
            with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                pcm = wf.readframes(wf.getnframes())
        except (wave.Error, EOFError, OSError):
            pcm = wav_bytes  # best-effort: forward whatever we got

        if self._turn_metrics is not None and self._turn_metrics["waiting_first_audio"]:
            self._turn_metrics["ttfa_s"] = round(time.time() - self._turn_metrics["t0"], 3)
        try:
            await self.transport.send_audio(pcm)
            self._turn_audio_chunks.append(pcm)
        except Exception as exc:
            logger.error("transport_out: failed to send fallback audio: %s", exc)
        if self._turn_metrics is not None and self._turn_metrics["waiting_first_audio"]:
            self._turn_metrics["e2e_s"] = round(time.time() - self._turn_metrics["t0"], 3)
            self._turn_metrics["waiting_first_audio"] = False
            self._turn_metrics["first_audio_event"].set()

        self._pending_flushes = max(0, self._pending_flushes - 1)
        if self._pending_flushes == 0:
            self._all_flushed_event.set()

    # ------------------------------------------------------------------
    # Task B: final transcript -> LLM -> sentence split -> TTS
    # ------------------------------------------------------------------
    async def _turn_worker(self):
        try:
            while True:
                item = await self._final_queue.get()
                user_text, t_started, t_final = item
                self._current_turn_task = asyncio.create_task(
                    self._run_turn(user_text, t_started, t_final)
                )
                try:
                    await self._current_turn_task
                except asyncio.CancelledError:
                    logger.info("turn: turn cancelled (barge-in)")
                except Exception as exc:
                    logger.error("turn: unhandled turn error: %s", exc)
                    await self._send_error("unknown", exc)
                finally:
                    self._current_turn_task = None
                    self._current_stop_event = None
                    self._agent_busy = False
        except asyncio.CancelledError:
            pass

    def _reasoning_effort_for(self, model_id: str) -> str:
        for _key, (mid, _label, effort) in MODELOS_LLM.items():
            if mid == model_id:
                return effort
        return "none"

    async def _run_turn(self, user_text: str, t_speech_started: float, t_final: float):
        self._agent_busy = True
        t0 = t_final
        stt_final_s = round(max(0.0, t_final - t_speech_started), 3)
        self._turn_metrics = {
            "t0": t0,
            "stt_final_s": stt_final_s,
            "ttft_s": None,
            "ttfa_s": None,
            "e2e_s": None,
            "waiting_first_audio": True,
            "first_audio_event": asyncio.Event(),
        }
        self._turn_audio_chunks = []

        self.history.add_user(user_text)
        await self.transport.send_event({"type": "stt_final", "text": user_text})

        model_id = self.llm_cfg.get("model_id") or self.profile.get("llm_model")
        reasoning_effort = self._reasoning_effort_for(model_id)

        token_queue: asyncio.Queue = asyncio.Queue()
        SENTINEL = object()
        stop_event = threading.Event()
        self._current_stop_event = stop_event
        loop = self._loop

        def on_token(delta):
            loop.call_soon_threadsafe(token_queue.put_nowait, delta)

        def run_llm():
            try:
                result = call_with_retry(
                    stream_chat, model_id, reasoning_effort, self.history,
                    on_token=on_token, stop_event=stop_event,
                )
                loop.call_soon_threadsafe(token_queue.put_nowait, ("__done__", result))
            except Exception as exc:
                loop.call_soon_threadsafe(token_queue.put_nowait, ("__error__", exc))
            finally:
                loop.call_soon_threadsafe(token_queue.put_nowait, SENTINEL)

        loop.run_in_executor(None, run_llm)

        llm_response = ""
        llm_error = None
        pending = ""

        while True:
            item = await token_queue.get()
            if item is SENTINEL:
                break
            if isinstance(item, tuple) and item[0] == "__done__":
                llm_response, _ttft, _total = item[1]
            elif isinstance(item, tuple) and item[0] == "__error__":
                llm_error = item[1]
            else:
                if self._turn_metrics["ttft_s"] is None:
                    self._turn_metrics["ttft_s"] = round(time.time() - t0, 3)
                await self.transport.send_event({"type": "agent_token", "token": item})
                pending += item
                sentences, pending = _extract_sentences(pending)
                if not sentences and len(pending) >= FORCE_FLUSH_CHARS:
                    sentences, pending = _extract_sentences(pending, force=True)
                for sentence in sentences:
                    await self._speak_sentence(sentence)

        if llm_error is not None:
            if self.history.messages and self.history.messages[-1]["role"] == "user":
                self.history.messages.pop()  # drop the unanswered user turn
            await self._send_error("llm", llm_error)
            return

        llm_response = (llm_response or "").strip()
        if self._turn_metrics["ttft_s"] is None:
            self._turn_metrics["ttft_s"] = round(time.time() - t0, 3)

        remaining, pending = _extract_sentences(pending, force=True)
        for sentence in remaining:
            await self._speak_sentence(sentence)

        await self.transport.send_event({
            "type": "agent_done",
            "text": llm_response,
            "ttft": self._turn_metrics["ttft_s"],
        })

        if not llm_response:
            self.history.add_assistant("")
            await self._send_error("llm", RuntimeError("Empty LLM response, TTS skipped."))
            return

        self.history.add_assistant(llm_response)

        if self._pending_flushes > 0:
            try:
                await asyncio.wait_for(self._all_flushed_event.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                logger.error("turn: timed out waiting for TTS to finish flushing this turn")

        try:
            await asyncio.wait_for(self._turn_metrics["first_audio_event"].wait(), timeout=6.0)
        except asyncio.TimeoutError:
            logger.error("metrics: timed out waiting for the first TTS audio chunk this turn")

        await self.transport.send_event({
            "type": "metrics",
            "stt_final_s": stt_final_s,
            "ttft_s": self._turn_metrics["ttft_s"],
            "ttfa_s": self._turn_metrics["ttfa_s"],
            "e2e_s": self._turn_metrics["e2e_s"],
        })

        try:
            wav_bytes = _pcm_chunks_to_wav(
                self._turn_audio_chunks, self.tts_cfg["encoding"], self.tts_cfg["sample_rate"]
            )
            log_turn({
                "call_id": self.call_id,
                "channel": self.channel,
                "llm_model": model_id,
                "user_text": user_text,
                "agent_text": llm_response,
                "stt_final_s": stt_final_s,
                "ttft_s": self._turn_metrics["ttft_s"],
                "ttfa_s": self._turn_metrics["ttfa_s"],
                "e2e_s": self._turn_metrics["e2e_s"],
                "tts_wav_bytes": wav_bytes,
            })
        except Exception as exc:
            logger.error("logging: unexpected failure logging turn (turn continues): %s", exc)

    # ------------------------------------------------------------------
    # Error surface
    # ------------------------------------------------------------------
    async def _send_error(self, stage: str, exc: Exception):
        kind = classify_error(exc)
        try:
            await self.transport.send_event({
                "type": "error",
                "stage": stage,
                "kind": kind,
                "detail": str(exc)[:200],
            })
        except Exception as send_exc:
            logger.error("send_error: could not deliver error event to transport: %s", send_exc)
