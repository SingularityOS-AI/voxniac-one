"""
vz_tts.py — 3 batch/REST TTS engines: Kokoro (DeepInfra), Deepgram Aura 2, Groq Orpheus.

Used as the fail-loud fallback for TTS when the live Deepgram Aura-2 WebSocket
(vz_tts_live.py) cannot connect or a send fails mid-call. Each function receives
text and returns (wav_bytes, elapsed_seconds), 8s timeout. Any exception is left
to propagate as-is (fail loud) so the caller can classify it and decide on a
message / retry.
"""

import base64
import time

import requests

from vz_config import DEEPGRAM_API_KEY, GROQ_API_KEY, KOKORO_API_KEY

TIMEOUT_S = 8

DEEPGRAM_TTS_MAX_CHARS = 2000


# ---------------------------------------------------------------------------
# 1. Kokoro (DeepInfra)
# ---------------------------------------------------------------------------
def synthesize_kokoro(text: str):
    """
    The response can be:
      (a) JSON with an "audio" field (base64 data-URI, e.g. "data:audio/wav;base64,...")
          or an "output" URL,
      (b) raw WAV bytes.
    Both cases are handled.
    """
    t0 = time.time()
    r = requests.post(
        "https://api.deepinfra.com/v1/inference/hexgrad/Kokoro-82M",
        headers={"Authorization": f"Bearer {KOKORO_API_KEY}"},
        json={"text": text},
        timeout=TIMEOUT_S,
    )
    elapsed = time.time() - t0

    if not r.ok:
        raise RuntimeError(f"Kokoro TTS HTTP {r.status_code}: {r.text[:200]}")

    wav_bytes = None
    try:
        data = r.json()
        audio_field = data.get("audio")
        if isinstance(audio_field, str) and audio_field:
            if "," in audio_field and audio_field.strip().lower().startswith("data:"):
                b64_part = audio_field.split(",", 1)[1]
            else:
                b64_part = audio_field
            wav_bytes = base64.b64decode(b64_part)
        elif isinstance(data.get("output"), str) and data["output"].startswith("http"):
            # URL to an audio file — download it.
            audio_resp = requests.get(data["output"], timeout=TIMEOUT_S)
            audio_resp.raise_for_status()
            wav_bytes = audio_resp.content
    except (ValueError, KeyError):
        wav_bytes = None

    if wav_bytes is None:
        if len(r.content) > 1000:
            wav_bytes = r.content
        else:
            raise RuntimeError(
                f"Kokoro TTS: response has no recognizable audio (len={len(r.content)}): {r.content[:200]}"
            )

    return wav_bytes, elapsed


# ---------------------------------------------------------------------------
# 2. Deepgram Aura 2
# ---------------------------------------------------------------------------
def synthesize_aura2(text: str):
    """Response = raw WAV bytes (r.content). Truncated to 2000 characters max."""
    truncated_text = text[:DEEPGRAM_TTS_MAX_CHARS]
    t0 = time.time()
    r = requests.post(
        "https://api.deepgram.com/v1/speak?model=aura-2-thalia-en&encoding=linear16&container=wav",
        headers={
            "Authorization": f"Token {DEEPGRAM_API_KEY}",
            "Content-Type": "application/json",
        },
        json={"text": truncated_text},
        timeout=TIMEOUT_S,
    )
    elapsed = time.time() - t0

    if not r.ok:
        raise RuntimeError(f"Deepgram Aura2 TTS HTTP {r.status_code}: {r.text[:200]}")

    wav_bytes = r.content
    if len(wav_bytes) < 100:
        raise RuntimeError(
            f"Deepgram Aura2 TTS: response too short to be valid audio (len={len(wav_bytes)})"
        )
    return wav_bytes, elapsed


# ---------------------------------------------------------------------------
# 3. Groq Orpheus
# ---------------------------------------------------------------------------
def synthesize_groq_orpheus(text: str):
    """Response = raw WAV bytes (r.content)."""
    t0 = time.time()
    r = requests.post(
        "https://api.groq.com/openai/v1/audio/speech",
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "canopylabs/orpheus-v1-english",
            "voice": "troy",
            "input": text,
            "response_format": "wav",
        },
        timeout=TIMEOUT_S,
    )
    elapsed = time.time() - t0

    if not r.ok:
        raise RuntimeError(f"Groq Orpheus TTS HTTP {r.status_code}: {r.text[:200]}")

    wav_bytes = r.content
    if len(wav_bytes) < 100:
        raise RuntimeError(
            f"Groq Orpheus TTS: response too short to be valid audio (len={len(wav_bytes)})"
        )
    return wav_bytes, elapsed


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
MOTORES_TTS = {
    "kokoro": ("kokoro", "Kokoro-82M (DeepInfra)", synthesize_kokoro),
    "aura2": ("aura2", "Deepgram Aura 2 (Thalia)", synthesize_aura2),
    "groq_orpheus": ("groq_orpheus", "Groq Orpheus (Troy)", synthesize_groq_orpheus),
}


def sintetizar(engine_key: str, text: str):
    """Dispatches to the matching TTS engine. engine_key must be a key of MOTORES_TTS."""
    if engine_key not in MOTORES_TTS:
        raise RuntimeError(f"Unknown TTS engine: {engine_key!r}")
    _, _, func = MOTORES_TTS[engine_key]
    return func(text)


# Backward compatibility: the console version called synthesize() bare for
# Kokoro (voxniac_zero.py's default engine).
def synthesize(text: str):
    return synthesize_kokoro(text)
