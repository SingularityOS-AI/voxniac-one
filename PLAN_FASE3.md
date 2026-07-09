# VOXNIAC ONE — Phase 3 Spec (SaaS experience: interviewer + call-a-prospect UI)

Implements Layer 1 of `../docs/VOXNIAC_SPEC.md` §4 per `../docs/CLAUDE_CODE_PROMPT_FASE3.md`,
adapted to the voxniac-zero-ONE codebase (the truth base IS `agent_profile.json`).
Everything in English. Do not break the working call path (Phase 1+2).

## A. Server-managed tunnel + "Call a prospect" (visible in the UI)

1. `server.py` lifespan startup: try to start the cloudflared tunnel by reusing
   `call_launcher.start_tunnel(port)` (import it; do not duplicate logic). Store
   `PUBLIC_WSS = wss://<host>/ws/twilio` in app state. If cloudflared fails, log
   loudly and continue — browser calls still work, phone calls report the error.
   Env override `VOXNIAC_PUBLIC_HOST` skips cloudflared (for cloud deploys).
2. `POST /call` with JSON `{"to": "+1..."}`: validates E.164-ish format, calls
   `call_launcher.trigger_call(to, PUBLIC_WSS)`, returns `{"sid", "to", "status"}`
   or a fail-loud error JSON (`{"error": {...}}`, HTTP 502/400). Never log secrets.
3. `GET /call/status` → `{"tunnel_up": bool, "public_host": str|null}`.
4. UI (`static/`): new "Call a prospect" card — phone input (default empty,
   placeholder `+1 307 555 0100`), button **Call prospect**, tunnel status dot
   (from /call/status), and result line (call SID / error). Disable button while
   a call is being placed. Keep the browser-call section as-is.

## B. Interviewer — Layer 1 state machine (the "promptizer")

1. New module `interviewer.py`:
   - States: `INTERVIEWING -> REVIEWING_PLAN -> APPROVED` (+ `back` command from
     REVIEWING_PLAN to INTERVIEWING preserving answers).
   - State + collected fields persist to `onboarding_state.json` next to the code
     (bulletproof load like agent_profile; closing/reopening the browser resumes
     where it left off). Backend owns state, client renders it.
   - LLM: `accounts/fireworks/models/gpt-oss-120b` with `reasoning_effort:"high"`
     via the existing `vz_llm.stream_chat` (pass model/effort explicitly; do NOT
     add it to the voice-call selector MODELOS_LLM).
   - Interview coverage (minimum truth-base fields, guided one question at a time,
     user answers free-form): services + prices; dream outcome the business sells;
     ICP + its acute pain points; opening line preferences; at least 3 common
     objections with the founder's preferred responses; escalation rule (when to
     hand to a human). Inspired by the Offer/Acquisition builders (money + pain +
     outcome, not deliverables).
   - REVIEWING_PLAN: the 120B model drafts the full profile and presents it for
     approval: a short opening line (max 2 spoken sentences) and a system_prompt
     built with this exact structure (ElevenLabs-derived, keep under ~1200 tokens):
     `# Personality`, `# Goal` (numbered steps: qualify -> objections -> close for
     appointment/email), `# Guardrails` (never re-introduce yourself — opening
     already played; 1-2 short sentences per reply under 30 words; anchor facts
     only — never invent prices; never transfer the call; escalation rule),
     `# Tone` (<=3 lines, spoken-natural). Repeat the two most critical rules in
     both Goal and Guardrails.
   - APPROVED (only on explicit user approval): writes `agent_profile.json`
     (system_prompt, agent_opening, voice, llm_model preserved from current file
     unless changed) + the raw structured fields under a `truth_base` key, then
     hot-reloads the profile used by new calls (make `vz_config` expose a
     `reload_agent_profile()` that re-reads the file and updates the dict
     in place so `cascade.py`/`vz_llm.py` pick it up for the NEXT call; document
     that in-flight calls keep the old prompt).
2. `server.py`: `WS /ws/interview` (or POST endpoints if simpler): JSON chat —
   client sends `{"type":"user_msg","text"}` / `{"type":"approve"}` /
   `{"type":"back"}` / `{"type":"reset"}`; server streams
   `{"type":"interviewer_token"}` / `{"type":"interviewer_done"}` /
   `{"type":"state","state","fields"}` / `{"type":"profile_written"}` / errors.
3. UI: new "Agent Setup" card (collapsible or second column): chat panel with the
   interviewer, state badge (INTERVIEWING / REVIEWING_PLAN / APPROVED), buttons
   Approve / Adjust (back) / Reset. After approval, refresh the agent-opening
   preview shown in the call cards.

## C. Constraints

- Do not modify `cascade.py`, `transports.py`, `vz_stt_live.py`, `vz_tts_live.py`
  beyond what §B.1 requires for profile hot-reload (prefer zero changes there).
- `MODELOS_LLM` voice table unchanged. Kimi stays the call default.
- Fail loud; a provider failure never crashes the server; all English.
- Verify: py_compile + import server + run server and exercise: GET /call/status,
  a full simulated interview via the WS/endpoints (scripted answers), approval
  writes agent_profile.json with the required fields, and the browser call UI
  still loads (GET / 200, /config 200).
