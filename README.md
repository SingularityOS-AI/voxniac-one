# VOXNIAC ONE

**The streaming voice core of Voxniac** — a transport-agnostic, fully streaming
ASR → LLM → TTS cascade with live partial transcripts, sentence-level speech
synthesis, and instant barge-in. This engine is the heart that will power
Voxniac's outbound sales calls (Twilio) and the live demo embedded in
[voxniac.com](https://voxniac.com).

Built by SingularityOS AI LLC (Wyoming, USA) for the AMD Developer Hackathon —
ACT II, Track 3. 100% open-weight models served by Fireworks AI; speech I/O by
Deepgram; zero multimodal S2S black boxes — every stage of the cascade produces
inspectable text, a hard compliance requirement for enterprise B2B (health,
legal).

---

## Architecture

```
Transport A: Browser microphone ── PCM16 16 kHz ──┐
Transport B: Twilio Media Streams ── mulaw 8 kHz ─┤
                                                  ▼
              ┌──────────────── cascade.py (asyncio) ────────────────┐
              │ Task A  audio in ──▶ Deepgram nova-3 live WebSocket  │
              │         (interim results · endpointing 300 ms ·     │
              │          VAD events)                                │
              │ Task B  final transcript ──▶ Fireworks LLM (SSE,    │
              │         Kimi K2.6 default) ──▶ sentence splitter    │
              │ Task C  sentences ──▶ Deepgram Aura-2 live WS ──▶   │
              │         audio chunks ──▶ transport out              │
              │                                                     │
              │ BARGE-IN: SpeechStarted while the agent is speaking │
              │ ⇒ cancel LLM turn + TTS Clear + flush playback +    │
              │   {"type":"barge_in"} — the agent shuts up NOW.     │
              └─────────────────────────────────────────────────────┘
```

Every sentence the LLM finishes is synthesized immediately — the agent starts
talking while the model is still writing. Nothing waits for anything it doesn't
have to.

### Stack

| Stage | Engine | Why |
|---|---|---|
| STT | Deepgram **nova-3** live WebSocket | True streaming + interim results + server-side endpointing (no push-to-talk, no batch upload) |
| LLM | Fireworks **Kimi K2.6** (`kimi-k2p6`, `reasoning_effort:"none"`) | Fastest of the 4 benchmarked open-weight models (0% downstream WER); selector also offers DeepSeek-V4-Flash, MiniMax M2.7, GPT-OSS-20B |
| TTS | Deepgram **Aura-2** live WebSocket (`aura-2-thalia-en`) | First audio chunk in ~100-400 ms, `Clear` message = instant barge-in |
| Fallbacks | Groq whisper-large-v3-turbo (batch) / Aura-2 REST | Engaged only if a live socket fails to connect or drops — loudly logged, never silent |

### Agent persona — not hardcoded

The persona (system prompt), the opening line, the voice, and the default model
live in **`agent_profile.json`** — not in code. Today it ships with the
SingularityOS appointment-setter profile ("Sharon"); in the Voxniac SaaS,
Stage 1 (the business interviewer) will *generate* this file per customer.
Loading is bulletproof: a missing or corrupt profile falls back to embedded
defaults without crashing.

---

## Quickstart

```bat
:: 1. Keys — resolved from ..\.env (GROQ_API_KEY, FIREWORKS_API_KEY,
::    DEEPGRAM_API_KEY, KOKORO_API_KEY)
:: 2. Install
pip install -r requirements.txt
:: 3. Run
RUN_VOXNIAC_ONE.bat        :: → http://127.0.0.1:8080
```

Open the page, pick an LLM, press **Start Call**. The agent speaks first.
Interrupt it mid-sentence — it stops. That's the product.

---

## WebSocket protocol (`/ws/call`)

**Client → server**
| Frame | Meaning |
|---|---|
| binary | PCM16 mono 16 kHz mic audio (continuous while in call) |
| `{"type":"start_call","llm":"<model_id>"}` | begin a call |
| `{"type":"end_call"}` | end it (socket stays open for the next call) |

**Server → client**
| Frame | Meaning |
|---|---|
| `{"type":"call_started","opening":…}` | agent opening text (audio follows) |
| `{"type":"stt_partial"/"stt_final","text":…}` | live user transcript |
| `{"type":"agent_token","token":…}` / `{"type":"agent_done",…}` | streaming reply text |
| binary | PCM16 mono 24 kHz agent audio — play immediately, gapless |
| `{"type":"barge_in"}` | stop playback instantly |
| `{"type":"metrics","stt_final_s","ttft_s","ttfa_s","e2e_s"}` | per-turn latency |
| `{"type":"error","stage","kind","detail"}` | fail-loud provider error (call survives) |

`/ws/twilio` speaks Twilio Media Streams natively: `connected/start/media/stop`
in, base64 mulaw `media` frames out, `clear` on barge-in. Same cascade, zero
transcoding — Deepgram accepts and emits mulaw 8 kHz directly.

## Metrics (per turn, logged to `voxniac_one_log.jsonl`)

- **stt_final_s** — user speech start → final transcript (utterance duration + endpointing)
- **ttft_s** — final transcript → first LLM token
- **ttfa_s** — final transcript → first TTS audio chunk ready
- **e2e_s** — final transcript → first audio byte sent to the transport (the perceived response latency)

### Measured (2026-07-08, automated full-turn test, real APIs)

| Metric | Value |
|---|---|
| LLM TTFT (Kimi K2.6) | **1.07 s** |
| E2E (speech end → first agent audio) | **1.46 s** |
| Previous turn-based harness (Voxniac Zero) | 6–10 s |

The <800 ms target is not yet met — TTFT dominates. The optimization path, in
order of expected impact: trim the system prompt (~450 tokens today), A/B
DeepSeek-V4-Flash TTFT, `endpointing=150`, and re-measure from a US-region
deployment (local tests add transatlantic RTT to every stage). Barge-in,
streaming and transport overhead are already at target.

## Engineering principles (non-negotiable)

1. **Fail loud** — every provider failure carries stage + auth/network/unknown classification.
2. **A failed turn never kills the call** — the socket and session survive everything short of a hangup.
3. **Bulletproof config** — clamp or default, never crash.
4. **Every stage inspectable** — transcripts, replies and latencies logged per turn.
5. **English everywhere** — code, comments, logs, UI.

## Repository map

```
cascade.py        # the heart: 3-task streaming pipeline + barge-in + metrics
transports.py     # BrowserTransport / TwilioTransport adapters
vz_stt_live.py    # Deepgram nova-3 live WS client
vz_tts_live.py    # Deepgram Aura-2 live WS client
vz_llm.py         # Fireworks SSE client (persistent session, 4 models)
vz_asr.py         # batch STT fallback (Groq whisper)
vz_tts.py         # REST TTS fallback (Aura-2 / Kokoro / Orpheus)
vz_config.py      # keys, agent_profile.json, clamped VAD config
vz_logger.py      # per-turn JSONL + WAV recordings
server.py         # FastAPI: /, /config, /ws/call, /ws/twilio
agent_profile.json# persona: system prompt, opening, voice, default model
static/           # live call UI (warm cream / terracotta Voxniac palette)
PLAN_ONE.md       # the refactor spec this build implements
```

## Roadmap

1. **Twilio live** — point a TwiML `<Stream>` at `/ws/twilio`, dial out with the
   corporate number (outbound dialer inherited from Neural Sales
   `src/ai/outbound_caller.py` / `twilio_voice.py`).
2. **Latency to <800 ms** — prompt diet, TTFT A/B, US-region deploy.
3. **Stage 1 interviewer** — an ultra-reasoning model interviews the founder and
   writes `agent_profile.json` (kills the last hardcoded artifact).
4. **voxniac.com embed** — the browser transport becomes the website's live demo.
5. **State machine** — `SCHEDULED / ESCALATED / REJECTED` outcomes per
   `VOXNIAC_SPEC.md` §4 (appointment setter, not closer).

---

*SingularityOS AI LLC — Sheridan, Wyoming. Zero latency. Absolute results.*
