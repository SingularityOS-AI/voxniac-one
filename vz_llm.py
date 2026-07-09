"""
vz_llm.py — Fireworks LLM streaming client: 4 selectable models, SSE, TTFT measurement.

Model ids and reasoning_effort values are EXACT (empirically validated) — do not
change them. The ORDER of MODELOS_LLM matters: the first available entry is the
selector's default. Kimi K2.6 goes first for being the fastest (0% downstream WER,
TTFT ~1.3s) — decision recorded in VOXNIAC_SPEC.md (v11). GPT-OSS-20B is last for
being 2-4x slower.

The system prompt and the agent's opening line are loaded from agent_profile.json
via vz_config (bulletproof: missing/corrupt profile -> embedded safe defaults).
Conversation history is capped at the last MAX_HISTORY_MESSAGES messages.

SYSTEM_PROMPT/AGENT_OPENING are NOT frozen snapshots: `AGENT_PROFILE` is a dict
owned by vz_config and mutated in place by `vz_config.reload_agent_profile()`
(Phase 3 hot-reload, used after the onboarding interviewer writes a new
agent_profile.json). `ConversationHistory.as_messages_with_system()` reads
`AGENT_PROFILE["system_prompt"]` fresh on every call so voice calls pick up a
reloaded profile on their next turn. `vz_llm.SYSTEM_PROMPT` / `AGENT_OPENING`
stay importable for any external caller (module-level `__getattr__`, PEP 562)
but resolve dynamically for the same reason — never cache them locally.
"""

import json
import threading
import time

import requests

from vz_config import AGENT_PROFILE, FIREWORKS_API_KEY, get_effective_system_prompt

TIMEOUT_S = 8
MAX_HISTORY_MESSAGES = 12
DEFAULT_MAX_TOKENS = 150


def __getattr__(name):
    # PEP 562 dynamic module attributes: `vz_llm.SYSTEM_PROMPT` / `AGENT_OPENING`
    # keep working for any external caller, but always resolve live against the
    # (possibly hot-reloaded) AGENT_PROFILE dict instead of a stale snapshot.
    if name == "SYSTEM_PROMPT":
        return get_effective_system_prompt(AGENT_PROFILE)
    if name == "AGENT_OPENING":
        return AGENT_PROFILE.get("agent_opening", "")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# (model_id, display_label, reasoning_effort) — EXACT values, do not change.
# Order = default priority (the first available model auto-selects).
MODELOS_LLM = {
    "1": ("accounts/fireworks/models/kimi-k2p6", "Kimi K2.6 [NONE]", "none"),
    "2": ("accounts/fireworks/models/deepseek-v4-flash", "DeepSeek-V4-Flash [NONE]", "none"),
    "3": ("accounts/fireworks/models/minimax-m2p7", "MiniMax M2.7 [LOW]", "low"),
    "4": ("accounts/fireworks/models/gpt-oss-20b", "GPT-OSS-20B [LOW]", "low"),
}

# Persistent HTTP session: reuses the TCP/TLS connection across turns and calls
# to shave milliseconds off TTFT versus opening a fresh connection every time.
_SESSION = requests.Session()


class ConversationHistory:
    """User/assistant message history, capped at MAX_HISTORY_MESSAGES (system prompt excluded)."""

    def __init__(self):
        self.messages = []

    def add_user(self, text: str):
        self.messages.append({"role": "user", "content": text})
        self._cap()

    def add_assistant(self, text: str):
        self.messages.append({"role": "assistant", "content": text})
        self._cap()

    def _cap(self):
        if len(self.messages) > MAX_HISTORY_MESSAGES:
            self.messages = self.messages[-MAX_HISTORY_MESSAGES:]

    def as_messages_with_system(self, system_prompt: "str | None" = None):
        """Builds the [system, *history] message list for a Fireworks call.

        system_prompt=None (default, used by every voice-call caller) calls
        vz_config.get_effective_system_prompt(AGENT_PROFILE) fresh at call
        time — never a cached value — so a hot-reloaded profile is picked up
        on the next turn. This is the single accessor point (Phase 3.5 P3):
        it composes profile["prompt_blocks"] into the final prompt when
        present, or falls back to the flat profile["system_prompt"] string
        unchanged for older profiles (see vz_config.build_system_prompt).
        Passing an explicit system_prompt (e.g. the onboarding interviewer's
        own persona) overrides that for this call only.
        """
        prompt = system_prompt if system_prompt is not None else get_effective_system_prompt(AGENT_PROFILE)
        return [{"role": "system", "content": prompt}] + self.messages


def stream_chat(
    model_id: str,
    reasoning_effort: str,
    history: ConversationHistory,
    on_token=None,
    stop_event: "threading.Event | None" = None,
    system_prompt: "str | None" = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: float = TIMEOUT_S,
):
    """
    Calls Fireworks in streaming mode. Returns (full_response, ttft_seconds, total_seconds).

    on_token(delta_str) is invoked for every content fragment received, so callers
    can forward tokens live (e.g. into a sentence splitter feeding TTS).

    stop_event, if provided, is polled between SSE lines; when set (barge-in), the
    HTTP response is closed immediately and the partial response accumulated so far
    is returned instead of blocking until the model finishes.

    system_prompt: optional override of the system message. Default (None)
    preserves existing behavior exactly — the live agent_profile.json system
    prompt is used (read fresh at call time, see as_messages_with_system).
    Callers that need a different persona (e.g. the onboarding interviewer,
    which is a distinct chat entity from the phone-call agent) pass their own
    string here; cascade.py/CascadeSession never needs to and doesn't.

    max_tokens: defaults to 150 (the existing voice-call budget, unchanged for
    every current caller). The onboarding interviewer passes a higher budget
    (700) since REVIEWING_PLAN needs to draft a full structured profile in one
    response.

    timeout: defaults to TIMEOUT_S=8s (unchanged voice-call behavior — that
    budget is tuned for low-latency phone turns on `reasoning_effort:"none"`
    models). The onboarding interviewer runs `gpt-oss-120b` at
    `reasoning_effort:"high"`, which can legitimately take longer to produce
    its first token; it passes a longer timeout explicitly. This is a
    per-call HTTP timeout (requests applies it as connect+per-read timeout on
    a streaming response), not a total-request budget.

    Any network/HTTP exception propagates as-is (fail loud) so the caller can
    classify it and decide whether to retry.
    """
    t0 = time.time()
    r = _SESSION.post(
        "https://api.fireworks.ai/inference/v1/chat/completions",
        headers={"Authorization": f"Bearer {FIREWORKS_API_KEY}"},
        json={
            "model": model_id,
            "max_tokens": max_tokens,
            "top_k": 40,
            "presence_penalty": 0,
            "frequency_penalty": 0,
            "stream": True,
            "reasoning_effort": reasoning_effort,
            "messages": history.as_messages_with_system(system_prompt),
        },
        stream=True,
        timeout=timeout,
    )
    if not r.ok:
        raise RuntimeError(f"Fireworks LLM HTTP {r.status_code}: {r.text[:200]}")

    ttft = None
    full_response = ""

    for line in r.iter_lines():
        if stop_event is not None and stop_event.is_set():
            r.close()
            break
        if not line:
            continue
        decoded = line.decode("utf-8")
        if not decoded.startswith("data: "):
            continue
        if decoded == "data: [DONE]":
            break
        try:
            chunk = json.loads(decoded[6:])
            delta = chunk["choices"][0]["delta"].get("content", "")
        except (json.JSONDecodeError, KeyError, IndexError):
            continue
        if delta:
            if ttft is None:
                ttft = time.time() - t0
            full_response += delta
            if on_token:
                on_token(delta)

    total = time.time() - t0
    if ttft is None:
        # No content tokens arrived (empty response) — avoid propagating None downstream.
        ttft = total
    return full_response, ttft, total
