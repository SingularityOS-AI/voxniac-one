# VOXNIAC ONE — Refactor Plan (authoritative spec for this iteration)

**Goal:** turn the `voxniac-zero-ONE` fork into the **streaming voice-agent core of Voxniac**:
a transport-agnostic ASR→LLM→TTS cascade with **sub-800 ms perceived latency**, full
streaming at every stage, barge-in, and a Twilio Media Streams endpoint ready for real
outbound calls. Everything — code, comments, docstrings, logs, UI — **in English**.
No hardcoded persona in code: the agent profile lives in `agent_profile.json`.

Based on: `../docs/VOXNIAC_SPEC.md` (v11), `../docs/MASTER_PROMPT_VOXNIAC_ZERO.md`,
`../docs/CLAUDE_CODE_PROMPT_FASE2.md`, and `investigacion.md` (latency architecture).

**Verified today (2026-07-08):** Deepgram key works for BOTH nova-3 STT (HTTP 200) and
Aura-2 TTS (successful runs in the log). Groq/Fireworks/DeepInfra keys work. The `.env`
lives at `../.env` relative to this folder (already resolved by `vz_config.py`).

---

## Target architecture (from investigacion.md)

```
Transport A: Browser mic  ── PCM16 16 kHz ──┐
Transport B: Twilio Media Streams (mulaw 8k)┤
                                            ▼
                    ┌───────────── cascade.py (asyncio) ─────────────┐
                    │ Task A: audio in ─▶ Deepgram nova-3 live WS    │
                    │ Task B: final transcript ─▶ Fireworks LLM SSE  │
                    │         ─▶ sentence splitter ─▶ text queue     │
                    │ Task C: text queue ─▶ Deepgram Aura-2 live WS  │
                    │         ─▶ audio chunks ─▶ transport out       │
                    │ Barge-in: SpeechStarted while TTS playing ⇒    │
                    │   cancel LLM+TTS tasks, clear queues,          │
                    │   notify transport (browser event / Twilio     │
                    │   "clear")                                     │
                    └────────────────────────────────────────────────┘
```

Latency budget (voice-to-voice, browser): Deepgram endpointing ~300 ms + LLM TTFT
~200-500 ms (Kimi K2.6, persistent connection) + Aura first chunk ~100-150 ms
≈ **600-950 ms**. Measure honestly; report per-turn metrics.

## Stack decisions (locked)

| Stage | Engine | Config |
|---|---|---|
| STT | **Deepgram nova-3 live WebSocket** | browser: `encoding=linear16&sample_rate=16000`; twilio: `encoding=mulaw&sample_rate=8000`; both: `channels=1&interim_results=true&endpointing=300&smart_format=true&language=en&vad_events=true` |
| LLM | **Fireworks Kimi K2.6** (`accounts/fireworks/models/kimi-k2p6`, `reasoning_effort:"none"`) default; selector keeps deepseek-v4-flash / minimax-m2p7 / gpt-oss-20b (exact ids + efforts from current `vz_llm.py` — do not change them) | SSE streaming, `max_tokens:150`, persistent `requests.Session` to cut TTFT |
| TTS | **Deepgram Aura-2 live WebSocket** (`aura-2-thalia-en`) | browser: `encoding=linear16&sample_rate=24000&container=none`; twilio: `encoding=mulaw&sample_rate=8000&container=none`. Send `{"type":"Speak","text":…}` + `{"type":"Flush"}` per sentence; `{"type":"Clear"}` on barge-in |

Fallbacks (fail-loud, only on connection error): Groq whisper-large-v3-turbo batch (existing `vz_asr.py`) and Aura-2 REST (existing `vz_tts.py`). Keep both modules, translate them to English.

## File map (what to create / rewrite / delete)

**CREATE**
- `agent_profile.json` — `{"system_prompt": …, "agent_opening": …, "voice": "aura-2-thalia-en", "llm_model": "accounts/fireworks/models/kimi-k2p6"}`. Move the current SYSTEM_PROMPT + AGENT_OPENING text out of `vz_llm.py` into here verbatim (they are already English). Loaded bulletproof by `vz_config.py` (missing file ⇒ safe defaults, never crash).
- `vz_stt_live.py` — async Deepgram live STT client (`websockets` lib). API: `class LiveSTT: async connect(encoding, sample_rate), async send_audio(bytes), events via callbacks or async queue: on_partial(text), on_final(text, speech_final), on_speech_started(), async finalize(), async close()`. KeepAlive every 5 s while idle. Fail-loud errors with stage context.
- `vz_tts_live.py` — async Deepgram Aura-2 live TTS client. API: `class LiveTTS: async connect(encoding, sample_rate, voice), async speak(text) (Speak+Flush), audio chunks via async queue, async clear() (barge-in), async close()`.
- `cascade.py` — transport-agnostic orchestrator implementing the 3-task pipeline + barge-in above. Public contract:
  ```python
  class CascadeSession:
      def __init__(self, transport, stt_cfg, llm_cfg, tts_cfg, profile): ...
      async def start(self)            # speaks agent_opening first
      async def feed_audio(self, b)    # raw inbound audio
      async def stop(self)
  # transport duck-type: async send_audio(bytes), async send_event(dict), async clear_audio()
  ```
  Sentence splitter: regex `[.!?]` boundaries + 180-char force flush (port from current `server.py` `_extract_sentences`).
  Per-turn metrics event: `{"type":"metrics","stt_final_s","ttft_s","ttfa_s","e2e_s"}` where `e2e` = user speech_final → first TTS chunk sent to transport.
- `transports.py` (or inside server.py) — `BrowserTransport` and `TwilioTransport` implementing the duck-type. Twilio: parse `connected/start/media/stop` events, base64 mulaw payloads in/out (`{"event":"media","media":{"payload":…}}`), `{"event":"clear"}` on barge-in, streamSid tracking.

**REWRITE**
- `server.py` — FastAPI: `GET /` (static), `GET /config`, `WS /ws/call` (browser), `WS /ws/twilio` (Media Streams). Browser protocol below. All English. Keep error taxonomy (auth/network/unknown) from current version.
- `vz_llm.py` — keep exact model table + streaming SSE; move prompts to profile; persistent Session; English.
- `vz_config.py` — English; add `agent_profile.json` loading (bulletproof); keep VAD clamps (still used as fallback config), keys, `engine_availability()`.
- `vz_logger.py` — English; log per-turn JSON (transcript, reply, metrics) + optional wav dump of TTS (concat chunks) into `recordings/`.
- `static/index.html`, `static/style.css`, `static/app.js` — **live call UI** (see below). Keep the warm cream/terracotta Voxniac palette from current `style.css`.
- `requirements.txt` — add `websockets>=12`.

**DELETE** (legacy console harness, superseded): `voxniac_zero.py`, `vz_audio.py`, `vz_wer.py`, `reference_scripts.json`, `RUN_VOXNIAC_ZERO.bat` → replace with `RUN_VOXNIAC_ONE.bat` (same interpreter cascade, runs `uvicorn server:app --port 8080`).

## Browser WS protocol (`/ws/call`)

Client → server:
- binary frames = PCM16 mono 16 kHz mic audio (continuous while call active)
- `{"type":"start_call","llm":"<model_id>"}` · `{"type":"end_call"}`

Server → client:
- `{"type":"call_started","opening":"<text>"}`
- `{"type":"stt_partial","text"}` · `{"type":"stt_final","text"}`
- `{"type":"agent_token","token"}` · `{"type":"agent_done","text","ttft"}`
- binary frames = PCM16 mono 24 kHz TTS audio (play immediately)
- `{"type":"barge_in"}` — client must stop playback + flush its audio buffer NOW
- `{"type":"metrics","stt_final_s","ttft_s","ttfa_s","e2e_s"}`
- `{"type":"error","stage","kind","detail"}`

## Live call UI (static/)

- One primary button: **Start call / End call**. No push-to-talk, no reference phrases, no WER — this is a live agent, mic stays open (AudioWorklet preferred, ScriptProcessor fallback), agent audio plays as it streams (Web Audio: queue PCM16 24 kHz chunks, schedule gapless via `AudioContext.currentTime`).
- Barge-in on `{"type":"barge_in"}`: stop all scheduled sources instantly.
- Live transcript panel (user partials in muted style → final solid; agent tokens streaming).
- Latency HUD: STT / TTFT / TTFA / E2E per turn + rolling history table.
- LLM selector only (populated from `/config`). Status pill (connected / in-call / speaking / listening).
- All English. Keep palette + fonts from the current `style.css` (Space Grotesk / Inter / JetBrains Mono, cream #faf6ef, terracotta #e8623c/#c94e2c).

## Non-negotiable principles (spec §6)

1. Fail loud — every provider failure surfaces with stage + detail; never silent.
2. Nothing hard-crashes the session — a failed turn leaves the call alive.
3. Bulletproof config — clamp/default, never raise.
4. Every stage inspectable — transcripts + replies + metrics logged per turn (JSONL).
5. English everywhere: code, comments, logs, UI, docs.

## Out of scope this iteration

Real Twilio call execution (endpoint must be ready + unit-testable, but wiring the
corporate number, TwiML app and outbound dialer comes next, reusing
`Neural Sales/src/ai/twilio_voice.py` + `outbound_caller.py` patterns). CRM, campaign
state machine, dynamic interview (SaaS Stage 1).
