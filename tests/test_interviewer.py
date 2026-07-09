"""Tests for interviewer.py's Phase 3.5 P3 plan-parsing changes
(_parse_plan_json's new prompt_blocks shape) and P2's model resolution
(get_interviewer_model_id used instead of a hardcoded constant). No network."""

import json

import interviewer
import vz_config


def _valid_blocks():
    return {
        "personality": "Sharon, ops director.",
        "environment": "Live outbound phone call.",
        "tone": "Short spoken sentences.",
        "goal": "Qualify then book an appointment.",
        "guardrails": "Never invent prices.",
    }


def _valid_truth_base():
    return {
        "business_name": "Acme",
        "objections": [
            {"objection": "too expensive", "response": "roi in 30 days"},
            {"objection": "not now", "response": "let's book a 15 min call"},
            {"objection": "who are you", "response": "Wyoming corp, fully licensed"},
        ],
        "escalation_rule": "hand off to the founder on request",
    }


# ---------------------------------------------------------------------------
# _parse_plan_json — Phase 3.5 P3 shape
# ---------------------------------------------------------------------------
def test_parse_plan_json_accepts_well_formed_prompt_blocks():
    payload = {
        "agent_opening": "Hi, quick question.",
        "prompt_blocks": _valid_blocks(),
        "truth_base": _valid_truth_base(),
    }
    parsed = interviewer._parse_plan_json(json.dumps(payload))
    assert parsed is not None
    assert parsed["prompt_blocks"]["personality"] == "Sharon, ops director."


def test_parse_plan_json_strips_markdown_fences():
    payload = {
        "agent_opening": "Hi.",
        "prompt_blocks": _valid_blocks(),
        "truth_base": _valid_truth_base(),
    }
    fenced = "```json\n" + json.dumps(payload) + "\n```"
    parsed = interviewer._parse_plan_json(fenced)
    assert parsed is not None


def test_parse_plan_json_rejects_missing_prompt_blocks_key():
    payload = {"agent_opening": "Hi.", "truth_base": _valid_truth_base()}
    assert interviewer._parse_plan_json(json.dumps(payload)) is None


def test_parse_plan_json_rejects_incomplete_prompt_blocks():
    blocks = _valid_blocks()
    del blocks["guardrails"]  # missing a required block
    payload = {"agent_opening": "Hi.", "prompt_blocks": blocks, "truth_base": _valid_truth_base()}
    assert interviewer._parse_plan_json(json.dumps(payload)) is None


def test_parse_plan_json_rejects_empty_string_block():
    blocks = _valid_blocks()
    blocks["tone"] = "   "  # blank
    payload = {"agent_opening": "Hi.", "prompt_blocks": blocks, "truth_base": _valid_truth_base()}
    assert interviewer._parse_plan_json(json.dumps(payload)) is None


def test_parse_plan_json_rejects_missing_agent_opening():
    payload = {"prompt_blocks": _valid_blocks(), "truth_base": _valid_truth_base()}
    assert interviewer._parse_plan_json(json.dumps(payload)) is None


def test_parse_plan_json_rejects_missing_truth_base():
    payload = {"agent_opening": "Hi.", "prompt_blocks": _valid_blocks()}
    assert interviewer._parse_plan_json(json.dumps(payload)) is None


def test_parse_plan_json_rejects_garbage_text():
    assert interviewer._parse_plan_json("not json at all") is None
    assert interviewer._parse_plan_json("") is None
    assert interviewer._parse_plan_json(None) is None


def test_parse_plan_json_recovers_json_with_stray_prose_around_it():
    payload = {
        "agent_opening": "Hi.",
        "prompt_blocks": _valid_blocks(),
        "truth_base": _valid_truth_base(),
    }
    text = "Here you go:\n" + json.dumps(payload) + "\nHope that helps!"
    parsed = interviewer._parse_plan_json(text)
    assert parsed is not None


# ---------------------------------------------------------------------------
# approve() — writes prompt_blocks-shaped agent_profile.json
# ---------------------------------------------------------------------------
def test_approve_writes_prompt_blocks_profile_and_hot_reloads(tmp_path, monkeypatch):
    profile_path = tmp_path / "agent_profile.json"
    monkeypatch.setattr(interviewer, "AGENT_PROFILE_PATH", profile_path)

    session = interviewer.InterviewSession.__new__(interviewer.InterviewSession)
    session.state = {
        "state": "REVIEWING_PLAN",
        "messages": [],
        "draft": {
            "agent_opening": "Hi there.",
            "prompt_blocks": _valid_blocks(),
            "truth_base": _valid_truth_base(),
        },
    }
    monkeypatch.setattr(session, "_save", lambda: None)
    monkeypatch.setattr(interviewer, "reload_agent_profile", lambda *a, **k: None)

    profile = session.approve()
    assert profile["prompt_blocks"]["personality"] == "Sharon, ops director."
    assert session.state["state"] == "APPROVED"

    on_disk = json.loads(profile_path.read_text(encoding="utf-8"))
    assert "prompt_blocks" in on_disk
    assert "system_prompt" not in on_disk  # new structure only, per P3


def test_approve_falls_back_to_legacy_system_prompt_without_raising(tmp_path, monkeypatch):
    """A draft left over from before Phase 3.5 (flat system_prompt, no
    prompt_blocks) must not crash approve() — see the docstring on approve()."""
    profile_path = tmp_path / "agent_profile.json"
    monkeypatch.setattr(interviewer, "AGENT_PROFILE_PATH", profile_path)

    session = interviewer.InterviewSession.__new__(interviewer.InterviewSession)
    session.state = {
        "state": "REVIEWING_PLAN",
        "messages": [],
        "draft": {
            "agent_opening": "Hi there.",
            "system_prompt": "Legacy flat prompt.",
            "truth_base": _valid_truth_base(),
        },
    }
    monkeypatch.setattr(session, "_save", lambda: None)
    monkeypatch.setattr(interviewer, "reload_agent_profile", lambda *a, **k: None)

    profile = session.approve()
    assert profile["system_prompt"] == "Legacy flat prompt."


# ---------------------------------------------------------------------------
# P2: model id resolution goes through vz_config, not a hardcoded constant
# ---------------------------------------------------------------------------
def test_interviewer_has_no_hardcoded_model_id_constant():
    assert not hasattr(interviewer, "INTERVIEWER_MODEL_ID")


def test_interviewer_model_id_resolves_via_vz_config(monkeypatch):
    monkeypatch.setattr(vz_config, "INTERVIEWER_CONFIG", {"model_id": "accounts/fireworks/models/gpt-oss-20b"})
    monkeypatch.delenv("INTERVIEWER_MODEL_ID", raising=False)
    assert interviewer.get_interviewer_model_id() == "accounts/fireworks/models/gpt-oss-20b"
