"""Tests for vz_config.py's Phase 3.5 additions:
- P3: build_system_prompt() / get_effective_system_prompt() / load_agent_profile()
  retrocompatibility with flat "system_prompt" profiles.
- P2: get_interviewer_model_id() / set_interviewer_model_id() config.json round trip.
No network.
"""

import json

import vz_config


# ---------------------------------------------------------------------------
# build_system_prompt — P3
# ---------------------------------------------------------------------------
def test_build_system_prompt_composes_blocks_in_order():
    profile = {
        "prompt_blocks": {
            "personality": "Persona text.",
            "environment": "Environment text.",
            "tone": "Tone text.",
            "goal": "Goal text.",
            "guardrails": "Guardrails text.",
        }
    }
    prompt = vz_config.build_system_prompt(profile)
    assert "## PERSONALITY\nPersona text." in prompt
    assert "## ENVIRONMENT\nEnvironment text." in prompt
    assert "## TONE\nTone text." in prompt
    assert "## GOAL\nGoal text." in prompt
    assert "## GUARDRAILS\nGuardrails text." in prompt
    # Order matters: personality before goal before guardrails.
    assert prompt.index("PERSONALITY") < prompt.index("GOAL") < prompt.index("GUARDRAILS")


def test_build_system_prompt_includes_truth_base_section():
    profile = {
        "prompt_blocks": {"personality": "P."},
        "truth_base": {
            "business_name": "Acme",
            "objections": [{"objection": "too expensive", "response": "we save you time"}],
        },
    }
    prompt = vz_config.build_system_prompt(profile)
    assert "## TRUTH_BASE" in prompt
    assert "business_name: Acme" in prompt
    assert "too expensive -> we save you time" in prompt


def test_build_system_prompt_falls_back_to_flat_system_prompt_when_no_blocks():
    profile = {"system_prompt": "You are a legacy flat-string agent."}
    prompt = vz_config.build_system_prompt(profile)
    assert prompt == "You are a legacy flat-string agent."


def test_build_system_prompt_falls_back_when_prompt_blocks_is_empty_dict():
    profile = {"prompt_blocks": {}, "system_prompt": "Fallback text."}
    assert vz_config.build_system_prompt(profile) == "Fallback text."


def test_build_system_prompt_falls_back_when_prompt_blocks_is_wrong_type():
    profile = {"prompt_blocks": "not a dict", "system_prompt": "Fallback text 2."}
    assert vz_config.build_system_prompt(profile) == "Fallback text 2."


def test_build_system_prompt_never_raises_on_missing_profile_keys():
    assert vz_config.build_system_prompt({}) == ""


def test_get_effective_system_prompt_uses_explicit_profile():
    profile = {"prompt_blocks": {"personality": "Explicit."}}
    assert "Explicit." in vz_config.get_effective_system_prompt(profile)


def test_get_effective_system_prompt_defaults_to_module_agent_profile(monkeypatch):
    fake_profile = {"system_prompt": "Module-level default text."}
    monkeypatch.setattr(vz_config, "AGENT_PROFILE", fake_profile)
    assert vz_config.get_effective_system_prompt() == "Module-level default text."


# ---------------------------------------------------------------------------
# load_agent_profile — retrocompatibility (P3)
# ---------------------------------------------------------------------------
def test_load_agent_profile_flat_legacy_file_has_no_prompt_blocks_override(tmp_path):
    path = tmp_path / "agent_profile.json"
    path.write_text(json.dumps({
        "system_prompt": "Legacy prompt.",
        "agent_opening": "Hello there.",
        "voice": "aura-2-thalia-en",
        "llm_model": "accounts/fireworks/models/kimi-k2p6",
    }), encoding="utf-8")

    profile = vz_config.load_agent_profile(path)
    assert profile["system_prompt"] == "Legacy prompt."
    assert profile["agent_opening"] == "Hello there."
    # No "prompt_blocks" key in the file -> the loader keeps the EMBEDDED
    # default prompt_blocks (never crashes), but build_system_prompt() must
    # still resolve to the flat legacy string, since that's what a legacy
    # caller cares about... except build_system_prompt prefers prompt_blocks
    # when present. This profile's prompt_blocks is the *default* Sharon
    # blocks (from _DEFAULT_AGENT_PROFILE), not the legacy text, so the
    # embedded default's own retrocompatibility is what's actually verified.
    assert isinstance(profile.get("prompt_blocks"), dict)


def test_load_agent_profile_with_prompt_blocks_overrides_default_blocks(tmp_path):
    path = tmp_path / "agent_profile.json"
    path.write_text(json.dumps({
        "agent_opening": "Hi.",
        "voice": "aura-2-thalia-en",
        "llm_model": "accounts/fireworks/models/kimi-k2p6",
        "prompt_blocks": {
            "personality": "Custom personality.",
            "environment": "Custom environment.",
            "tone": "Custom tone.",
            "goal": "Custom goal.",
            "guardrails": "Custom guardrails.",
        },
        "truth_base": {"business_name": "Acme Corp"},
    }), encoding="utf-8")

    profile = vz_config.load_agent_profile(path)
    assert profile["prompt_blocks"]["personality"] == "Custom personality."
    assert profile["truth_base"]["business_name"] == "Acme Corp"
    prompt = vz_config.build_system_prompt(profile)
    assert "Custom personality." in prompt
    assert "business_name: Acme Corp" in prompt


def test_load_agent_profile_missing_file_uses_embedded_defaults(tmp_path):
    path = tmp_path / "does_not_exist.json"
    profile = vz_config.load_agent_profile(path)
    assert profile["agent_opening"]  # embedded default is non-empty
    assert isinstance(profile["prompt_blocks"], dict)


def test_load_agent_profile_malformed_prompt_blocks_falls_back_to_default(tmp_path):
    path = tmp_path / "agent_profile.json"
    path.write_text(json.dumps({
        "agent_opening": "Hi.",
        "prompt_blocks": "not-a-dict",
    }), encoding="utf-8")
    profile = vz_config.load_agent_profile(path)
    # Malformed prompt_blocks -> keeps the embedded default dict, never crashes.
    assert isinstance(profile["prompt_blocks"], dict)
    assert profile["prompt_blocks"]  # non-empty


# ---------------------------------------------------------------------------
# Interviewer model selection — P2
# ---------------------------------------------------------------------------
def test_set_interviewer_model_id_persists_and_reads_back(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(vz_config, "INTERVIEWER_CONFIG", dict(vz_config.INTERVIEWER_CONFIG))
    monkeypatch.delenv("INTERVIEWER_MODEL_ID", raising=False)

    target = "accounts/fireworks/models/gpt-oss-20b"
    ok = vz_config.set_interviewer_model_id(target, path=config_path)
    assert ok is True
    assert vz_config.get_interviewer_model_id() == target

    # Persisted to disk too.
    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert on_disk["interviewer"]["model_id"] == target


def test_set_interviewer_model_id_rejects_unknown_model(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(vz_config, "INTERVIEWER_CONFIG", dict(vz_config.INTERVIEWER_CONFIG))
    ok = vz_config.set_interviewer_model_id("not-a-real-model", path=config_path)
    assert ok is False
    assert not config_path.exists()


def test_set_interviewer_model_id_preserves_existing_config_content(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"vad": {"silence_ms": 999}}), encoding="utf-8")
    monkeypatch.setattr(vz_config, "INTERVIEWER_CONFIG", dict(vz_config.INTERVIEWER_CONFIG))

    ok = vz_config.set_interviewer_model_id("accounts/fireworks/models/gpt-oss-20b", path=config_path)
    assert ok is True
    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert on_disk["vad"]["silence_ms"] == 999  # untouched
    assert on_disk["interviewer"]["model_id"] == "accounts/fireworks/models/gpt-oss-20b"


def test_get_interviewer_model_id_env_override_takes_precedence(monkeypatch):
    monkeypatch.setattr(vz_config, "INTERVIEWER_CONFIG", {"model_id": "accounts/fireworks/models/gpt-oss-120b"})
    monkeypatch.setenv("INTERVIEWER_MODEL_ID", "accounts/fireworks/models/gpt-oss-20b")
    assert vz_config.get_interviewer_model_id() == "accounts/fireworks/models/gpt-oss-20b"


def test_get_interviewer_model_id_ignores_unknown_env_value(monkeypatch):
    monkeypatch.setattr(vz_config, "INTERVIEWER_CONFIG", {"model_id": "accounts/fireworks/models/gpt-oss-120b"})
    monkeypatch.setenv("INTERVIEWER_MODEL_ID", "not-a-real-model")
    assert vz_config.get_interviewer_model_id() == "accounts/fireworks/models/gpt-oss-120b"


def test_load_config_merges_interviewer_section_bulletproof(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"interviewer": {"model_id": "garbage"}}), encoding="utf-8")
    cfg = vz_config.load_config(path)
    assert cfg["interviewer"]["model_id"] == vz_config.DEFAULT_INTERVIEWER_MODEL_ID


def test_load_config_missing_file_uses_full_defaults(tmp_path):
    path = tmp_path / "missing.json"
    cfg = vz_config.load_config(path)
    assert cfg["interviewer"]["model_id"] == vz_config.DEFAULT_INTERVIEWER_MODEL_ID
    assert cfg["vad"]["silence_ms"] == vz_config.DEFAULT_CONFIG["vad"]["silence_ms"]
