"""Tests for server.py's Phase 4 Etapa B endpoints: GET/POST /profile — the
Agent Setup "Profile Editor"'s backend (manual edit of agent_opening/
prompt_blocks.*/truth_base/voice/llm_model, independent of the interviewer's
own chat-driven Approve flow). No network: agent_profile.json I/O is
redirected to tmp_path (server.AGENT_PROFILE_PATH monkeypatched, same pattern
test_interviewer.py uses for interviewer.AGENT_PROFILE_PATH), and the real
vz_config.AGENT_PROFILE module singleton (mutated in place by
reload_agent_profile(), same object server.py's own AGENT_PROFILE name
refers to) is snapshotted/restored around every test that calls POST
/profile, so a save in this test file can never leak into another test's
expectations elsewhere in the suite.
"""

import copy
import json

import pytest
from fastapi.testclient import TestClient

import server
import vz_config


@pytest.fixture(autouse=True)
def _skip_real_cloudflared_tunnel(monkeypatch):
    """Same override as test_server_twilio.py: forces the VOXNIAC_PUBLIC_HOST
    override path so TestClient(app)'s lifespan startup never spawns a real
    cloudflared process for these HTTP-only tests."""
    monkeypatch.setattr(server, "PUBLIC_HOST_OVERRIDE", "test.invalid")


@pytest.fixture(autouse=True)
def _restore_agent_profile():
    """vz_config.AGENT_PROFILE is a module-level singleton dict that
    reload_agent_profile() mutates IN PLACE (clear + update) — the exact
    same object server.py's own `AGENT_PROFILE` name refers to. A real POST
    /profile call in a test therefore mutates shared state that would
    otherwise bleed into every other test in the session. Snapshot before,
    restore after."""
    snapshot = copy.deepcopy(vz_config.AGENT_PROFILE)
    yield
    vz_config.AGENT_PROFILE.clear()
    vz_config.AGENT_PROFILE.update(snapshot)


def _valid_payload():
    return {
        "agent_opening": "Hi, quick question for you.",
        "voice": "aura-2-thalia-en",
        "llm_model": "accounts/fireworks/models/kimi-k2p6",
        "prompt_blocks": {
            "personality": "Test personality.",
            "environment": "Test environment.",
            "tone": "Test tone.",
            "goal": "Test goal.",
            "guardrails": "Test guardrails.",
        },
        "truth_base": json.dumps({"business_name": "Acme Test Co"}),
    }


# ---------------------------------------------------------------------------
# GET /profile
# ---------------------------------------------------------------------------
def test_get_profile_returns_expected_shape(monkeypatch):
    fake_profile = {
        "agent_opening": "Hello there.",
        "voice": "aura-2-thalia-en",
        "llm_model": "accounts/fireworks/models/kimi-k2p6",
        "prompt_blocks": {
            "personality": "P.",
            "environment": "E.",
            "tone": "T.",
            "goal": "G.",
            "guardrails": "GR.",
        },
        "truth_base": {"business_name": "Acme"},
    }
    monkeypatch.setattr(server, "AGENT_PROFILE", fake_profile)

    with TestClient(server.app) as client:
        resp = client.get("/profile")

    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_opening"] == "Hello there."
    assert data["voice"] == "aura-2-thalia-en"
    assert data["llm_model"] == "accounts/fireworks/models/kimi-k2p6"
    assert data["prompt_blocks"] == {
        "personality": "P.", "environment": "E.", "tone": "T.", "goal": "G.", "guardrails": "GR.",
    }
    assert data["truth_base"] == {"business_name": "Acme"}


def test_get_profile_never_raises_on_missing_prompt_blocks_or_truth_base(monkeypatch):
    """A legacy flat-string profile (no prompt_blocks/truth_base keys at all)
    must still render blank textareas, never a 500."""
    monkeypatch.setattr(server, "AGENT_PROFILE", {"agent_opening": "Legacy.", "voice": "v", "llm_model": "m"})

    with TestClient(server.app) as client:
        resp = client.get("/profile")

    assert resp.status_code == 200
    data = resp.json()
    assert data["prompt_blocks"] == {
        "personality": "", "environment": "", "tone": "", "goal": "", "guardrails": "",
    }
    assert data["truth_base"] == {}


# ---------------------------------------------------------------------------
# POST /profile — roundtrip + hot-reload
# ---------------------------------------------------------------------------
def test_post_profile_roundtrip_writes_file_and_hot_reloads(tmp_path, monkeypatch):
    profile_path = tmp_path / "agent_profile.json"
    monkeypatch.setattr(server, "AGENT_PROFILE_PATH", profile_path)

    with TestClient(server.app) as client:
        resp = client.post("/profile", json=_valid_payload())

    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_opening"] == "Hi, quick question for you."
    assert data["prompt_blocks"]["personality"] == "Test personality."
    assert data["truth_base"] == {"business_name": "Acme Test Co"}
    assert data["voice"] == "aura-2-thalia-en"
    assert data["llm_model"] == "accounts/fireworks/models/kimi-k2p6"

    # Written to the (redirected) file on disk.
    on_disk = json.loads(profile_path.read_text(encoding="utf-8"))
    assert on_disk["agent_opening"] == "Hi, quick question for you."
    assert on_disk["truth_base"] == {"business_name": "Acme Test Co"}

    # Hot-reloaded: the shared AGENT_PROFILE singleton (same object GET
    # /profile reads) reflects the new content without a restart.
    assert vz_config.AGENT_PROFILE["agent_opening"] == "Hi, quick question for you."

    with TestClient(server.app) as client:
        get_resp = client.get("/profile")
    assert get_resp.json()["agent_opening"] == "Hi, quick question for you."


def test_post_profile_accepts_truth_base_as_dict_not_string(tmp_path, monkeypatch):
    profile_path = tmp_path / "agent_profile.json"
    monkeypatch.setattr(server, "AGENT_PROFILE_PATH", profile_path)

    payload = _valid_payload()
    payload["truth_base"] = {"business_name": "Dict Co"}  # dict, not a JSON string

    with TestClient(server.app) as client:
        resp = client.post("/profile", json=payload)

    assert resp.status_code == 200
    assert resp.json()["truth_base"] == {"business_name": "Dict Co"}


def test_post_profile_empty_truth_base_string_becomes_empty_object(tmp_path, monkeypatch):
    profile_path = tmp_path / "agent_profile.json"
    monkeypatch.setattr(server, "AGENT_PROFILE_PATH", profile_path)

    payload = _valid_payload()
    payload["truth_base"] = "   "

    with TestClient(server.app) as client:
        resp = client.post("/profile", json=payload)

    assert resp.status_code == 200
    assert resp.json()["truth_base"] == {}


# ---------------------------------------------------------------------------
# POST /profile — validation failures (fail loud, HTTP 400)
# ---------------------------------------------------------------------------
def test_post_profile_invalid_truth_base_json_string_returns_400(tmp_path, monkeypatch):
    profile_path = tmp_path / "agent_profile.json"
    monkeypatch.setattr(server, "AGENT_PROFILE_PATH", profile_path)

    payload = _valid_payload()
    payload["truth_base"] = "{not valid json"

    with TestClient(server.app) as client:
        resp = client.post("/profile", json=payload)

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["stage"] == "profile"
    assert "truth_base" in body["error"]["detail"]
    assert not profile_path.exists()  # never wrote a half-valid payload


def test_post_profile_truth_base_json_array_returns_400(tmp_path, monkeypatch):
    profile_path = tmp_path / "agent_profile.json"
    monkeypatch.setattr(server, "AGENT_PROFILE_PATH", profile_path)

    payload = _valid_payload()
    payload["truth_base"] = json.dumps([1, 2, 3])  # valid JSON, but not an object

    with TestClient(server.app) as client:
        resp = client.post("/profile", json=payload)

    assert resp.status_code == 400
    assert resp.json()["error"]["stage"] == "profile"


def test_post_profile_non_string_prompt_block_returns_400(tmp_path, monkeypatch):
    profile_path = tmp_path / "agent_profile.json"
    monkeypatch.setattr(server, "AGENT_PROFILE_PATH", profile_path)

    payload = _valid_payload()
    payload["prompt_blocks"]["tone"] = 12345  # must be a string

    with TestClient(server.app) as client:
        resp = client.post("/profile", json=payload)

    assert resp.status_code == 400
    assert "prompt_blocks.tone" in resp.json()["error"]["detail"]


def test_post_profile_non_string_agent_opening_returns_400(tmp_path, monkeypatch):
    profile_path = tmp_path / "agent_profile.json"
    monkeypatch.setattr(server, "AGENT_PROFILE_PATH", profile_path)

    payload = _valid_payload()
    payload["agent_opening"] = ["not", "a", "string"]

    with TestClient(server.app) as client:
        resp = client.post("/profile", json=payload)

    assert resp.status_code == 400
    assert "agent_opening" in resp.json()["error"]["detail"]


def test_post_profile_prompt_blocks_wrong_type_returns_400(tmp_path, monkeypatch):
    profile_path = tmp_path / "agent_profile.json"
    monkeypatch.setattr(server, "AGENT_PROFILE_PATH", profile_path)

    payload = _valid_payload()
    payload["prompt_blocks"] = "not a dict"

    with TestClient(server.app) as client:
        resp = client.post("/profile", json=payload)

    assert resp.status_code == 400
    assert "prompt_blocks" in resp.json()["error"]["detail"]


def test_post_profile_top_level_json_array_returns_400(tmp_path, monkeypatch):
    """A syntactically valid JSON body that isn't an object at all (e.g. a
    bare array) must fail loud with 400, not a 500 or an AttributeError."""
    profile_path = tmp_path / "agent_profile.json"
    monkeypatch.setattr(server, "AGENT_PROFILE_PATH", profile_path)

    with TestClient(server.app) as client:
        resp = client.post(
            "/profile", content=b"[1, 2, 3]", headers={"Content-Type": "application/json"},
        )

    assert resp.status_code == 400
    assert resp.json()["error"]["stage"] == "profile"


def test_post_profile_malformed_json_body_returns_400(tmp_path, monkeypatch):
    profile_path = tmp_path / "agent_profile.json"
    monkeypatch.setattr(server, "AGENT_PROFILE_PATH", profile_path)

    with TestClient(server.app) as client:
        resp = client.post(
            "/profile", content=b"{not json at all", headers={"Content-Type": "application/json"},
        )

    assert resp.status_code == 400
    assert resp.json()["error"]["stage"] == "profile"
