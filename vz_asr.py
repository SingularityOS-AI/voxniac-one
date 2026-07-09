"""
vz_asr.py — 4 batch ASR engines, per-utterance (POST of the full WAV), 8s timeout.

Used as the fail-loud fallback for STT when the live Deepgram nova-3 WebSocket
(vz_stt_live.py) cannot connect. Each function receives wav_bytes and returns
(text, elapsed_seconds). Any exception is left to propagate as-is (with its
original detail) so the caller can classify it (auth vs network vs unknown) and
decide on a message / retry. This is "fail loud": no error is ever swallowed here.
"""

import time

import requests

from vz_config import DEEPGRAM_API_KEY, GOOGLE_MODEL, GROQ_API_KEY

TIMEOUT_S = 8

try:
    from google.cloud import speech as _google_speech

    GOOGLE_SPEECH_AVAILABLE = True
except ImportError:
    _google_speech = None
    GOOGLE_SPEECH_AVAILABLE = False


# Order = default priority (the first available engine auto-selects).
# whisper-large-v3-turbo goes first: the most reliable batch STT (0% WER) and
# the fastest. Deepgram batch and Google Cloud Speech remain as comparison
# options, not as the default fallback engine.
ASR_ENGINES = {
    "1": ("groq_whisper_large_v3_turbo", "Groq (whisper-large-v3-turbo)"),
    "2": ("groq_whisper_large_v3", "Groq (whisper-large-v3)"),
    "3": ("deepgram_nova3", "Deepgram (nova-3)"),
    "4": ("google_cloud_speech", f"Google Cloud Speech ({GOOGLE_MODEL})"),
}


def _asr_groq(wav_bytes: bytes, model: str):
    t0 = time.time()
    r = requests.post(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        files={"file": ("utt.wav", wav_bytes, "audio/wav")},
        data={"model": model},
        timeout=TIMEOUT_S,
    )
    if not r.ok:
        raise RuntimeError(f"Groq ASR HTTP {r.status_code}: {r.text[:200]}")
    text = r.json()["text"]
    return text, time.time() - t0


def asr_groq_large_v3(wav_bytes: bytes):
    return _asr_groq(wav_bytes, "whisper-large-v3")


def asr_groq_large_v3_turbo(wav_bytes: bytes):
    return _asr_groq(wav_bytes, "whisper-large-v3-turbo")


def _deepgram_prerecorded(audio_bytes: bytes, content_type: str):
    """Shared Deepgram pre-recorded REST call (nova-3, smart formatting):
    POSTs raw audio bytes with the given Content-Type and returns
    (transcript, elapsed_seconds). Deepgram transcodes whatever container/
    codec the Content-Type declares server-side — no local transcoding
    needed for wav, webm/opus, etc."""
    t0 = time.time()
    r = requests.post(
        "https://api.deepgram.com/v1/listen?model=nova-3&smart_format=true",
        headers={
            "Authorization": f"Token {DEEPGRAM_API_KEY}",
            "Content-Type": content_type,
        },
        data=audio_bytes,
        timeout=TIMEOUT_S,
    )
    if not r.ok:
        raise RuntimeError(f"Deepgram ASR HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    text = data["results"]["channels"][0]["alternatives"][0]["transcript"]
    return text, time.time() - t0


def asr_deepgram_nova3(wav_bytes: bytes):
    return _deepgram_prerecorded(wav_bytes, "audio/wav")


def transcribe_prerecorded(audio_bytes: bytes, content_type: str = "audio/webm") -> str:
    """Phase 3.5 P2: transcribes an arbitrary recorded voice note (e.g. the
    Agent Setup mic button's webm/opus MediaRecorder blob) via Deepgram
    pre-recorded REST. Unlike asr_deepgram_nova3 (always audio/wav), this
    accepts whatever Content-Type the browser produced. Returns the
    transcript text only (elapsed time isn't needed by this caller); any
    exception propagates as-is (fail loud, per this module's contract)."""
    text, _elapsed = _deepgram_prerecorded(audio_bytes, content_type or "audio/webm")
    return text


def asr_google_cloud_speech(wav_bytes: bytes):
    if not GOOGLE_SPEECH_AVAILABLE:
        raise RuntimeError("google-cloud-speech is not installed")
    t0 = time.time()
    client = _google_speech.SpeechClient()
    audio = _google_speech.RecognitionAudio(content=wav_bytes)
    config = _google_speech.RecognitionConfig(
        encoding=_google_speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=16000,
        language_code="en-US",
        model=GOOGLE_MODEL,
    )
    response = client.recognize(config=config, audio=audio)
    text = " ".join(result.alternatives[0].transcript for result in response.results)
    return text, time.time() - t0


ASR_FUNCTIONS = {
    "groq_whisper_large_v3": asr_groq_large_v3,
    "groq_whisper_large_v3_turbo": asr_groq_large_v3_turbo,
    "deepgram_nova3": asr_deepgram_nova3,
    "google_cloud_speech": asr_google_cloud_speech,
}


def transcribe(engine_key: str, wav_bytes: bytes):
    """Dispatches to the matching ASR engine. engine_key must be a key of ASR_FUNCTIONS."""
    func = ASR_FUNCTIONS[engine_key]
    return func(wav_bytes)
