"""Tests for Phase 4 Etapa C (Campaigns): leads.py's store/obfuscation/LLM
helpers, and server.py's /leads* endpoints + the /ws/twilio lead-call
override + post-call classification wiring. No network anywhere: every
Fireworks call (leads.stream_chat), every Twilio call (call_launcher.
trigger_call), and CascadeSession itself are monkeypatched/faked.
"""

import copy
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import leads
import server
import vz_config

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
APOLLO_CSV_PATH = FIXTURES_DIR / "apollo_demo.csv"


@pytest.fixture(autouse=True)
def _isolated_leads_db(tmp_path, monkeypatch):
    """Every test in this file gets its own leads.db (tmp_path) — never the
    real one in the repo root, and never shared across tests."""
    monkeypatch.setattr(leads, "LEADS_DB_PATH", tmp_path / "leads.db")


@pytest.fixture(autouse=True)
def _skip_real_cloudflared_tunnel(monkeypatch):
    """Same override every other server.py test file uses: forces the
    VOXNIAC_PUBLIC_HOST override path so TestClient(app)'s lifespan startup
    never spawns a real cloudflared process."""
    monkeypatch.setattr(server, "PUBLIC_HOST_OVERRIDE", "test.invalid")


@pytest.fixture(autouse=True)
def _restore_agent_profile():
    """Same snapshot/restore pattern test_profile_endpoint.py uses — nothing
    in this file should ever leave vz_config.AGENT_PROFILE mutated for
    another test, and Etapa C's whole "never touch agent_profile.json"
    guarantee is exactly what several tests below assert against."""
    snapshot = copy.deepcopy(vz_config.AGENT_PROFILE)
    yield
    vz_config.AGENT_PROFILE.clear()
    vz_config.AGENT_PROFILE.update(snapshot)


# ---------------------------------------------------------------------------
# Obfuscation (Gate 2)
# ---------------------------------------------------------------------------
def test_mask_phone_apollo_shape():
    assert leads.mask_phone("+13055551234") == "+1305•••1234"


def test_mask_phone_handles_short_garbage_input_gracefully():
    assert leads.mask_phone("55") == "•••"
    assert leads.mask_phone("") == ""
    assert leads.mask_phone(None) == ""


def test_mask_email_domain_only():
    assert leads.mask_email("jane.doe@acme.com") == "acme.com"


def test_mask_email_handles_malformed_input():
    assert leads.mask_email("not-an-email") == ""
    assert leads.mask_email(None) == ""


# ---------------------------------------------------------------------------
# CSV import + obfuscation (leads.import_csv)
# ---------------------------------------------------------------------------
def test_import_csv_counts_and_imports_the_demo_fixture():
    csv_bytes = APOLLO_CSV_PATH.read_bytes()
    result = leads.import_csv(csv_bytes)
    assert result == {"imported": 7, "skipped": 0}

    all_leads = leads.list_leads()
    assert len(all_leads) == 7
    assert all(row["status"] == "COLD" for row in all_leads)
    assert all(row["isBallena"] is False for row in all_leads)
    assert all(row["painPoints"] == [] for row in all_leads)


def test_import_csv_never_persists_the_real_phone_or_email(tmp_path):
    """The exact acceptance criterion from PLAN_FASE4_CAMPAIGNS.md: the real
    phone number must NOT appear anywhere in leads.db, not even as a
    substring — only mask_phone()'s output does."""
    csv_bytes = APOLLO_CSV_PATH.read_bytes()
    leads.import_csv(csv_bytes)

    db_bytes = leads.LEADS_DB_PATH.read_bytes()
    # Every raw phone number from the fixture CSV (unmasked).
    for raw_phone in ("+15551230001", "+15551230002", "+15551230007"):
        assert raw_phone.encode() not in db_bytes
    # Every raw local-part of an email from the fixture CSV.
    for raw_email in (b"avance@aetherdynamic.example", b"sjenkins@cloudscale.example"):
        assert raw_email not in db_bytes

    all_leads = leads.list_leads()
    phones = {row["phone"] for row in all_leads}
    assert "+1555•••0001" in phones
    emails = {row["email"] for row in all_leads}
    assert "aetherdynamic.example" in emails
    assert all("@" not in e for e in emails)  # domain only, never the local part


def test_import_csv_skips_blank_rows():
    csv_text = (
        "First Name,Last Name,Company,Email,Work Direct Phone,Industry,# Employees\n"
        "Jane,Doe,Acme Inc,jane@acme.example,+15551111111,Tech,50\n"
        ",,,,,,\n"  # entirely blank row -> skipped
        "John,Smith,Beta LLC,john@beta.example,+15552222222,Finance,20\n"
    )
    result = leads.import_csv(csv_text.encode("utf-8"))
    assert result == {"imported": 2, "skipped": 1}


def test_import_csv_tolerates_missing_columns():
    """A CSV missing the phone/industry/# Employees columns entirely must
    still import (with blank defaults), never raise."""
    csv_text = "First Name,Last Name,Company,Email\nJane,Doe,Acme Inc,jane@acme.example\n"
    result = leads.import_csv(csv_text.encode("utf-8"))
    assert result == {"imported": 1, "skipped": 0}
    lead = leads.list_leads()[0]
    assert lead["phone"] == ""
    assert lead["industry"] == ""
    assert lead["companySize"] == ""


# ---------------------------------------------------------------------------
# CRUD (leads.py)
# ---------------------------------------------------------------------------
def _import_one():
    leads.import_csv(APOLLO_CSV_PATH.read_bytes())
    return leads.list_leads()[0]


def test_update_lead_only_touches_whitelisted_fields():
    lead = _import_one()
    original_phone = lead["phone"]
    original_email = lead["email"]

    updated = leads.update_lead(lead["id"], {
        "status": "HOT",
        "isBallena": True,
        "painPoints": ["slow onboarding", "manual data entry"],
        "phone": "+19998887777",  # NOT whitelisted -> must be ignored
        "email": "hacked@evil.example",  # NOT whitelisted -> must be ignored
    })

    assert updated["status"] == "HOT"
    assert updated["isBallena"] is True
    assert updated["painPoints"] == ["slow onboarding", "manual data entry"]
    assert updated["phone"] == original_phone  # unchanged
    assert updated["email"] == original_email  # unchanged


def test_update_lead_unknown_id_returns_none():
    assert leads.update_lead("does-not-exist", {"status": "HOT"}) is None


def test_update_lead_call_sets_last_call_fields():
    lead = _import_one()
    updated = leads.update_lead_call(lead["id"], "20260710_120000_0100", "2026-07-10T12:00:00+00:00")
    assert updated["lastCallId"] == "20260710_120000_0100"
    assert updated["lastCallDate"] == "2026-07-10T12:00:00+00:00"


def test_update_lead_classification_sets_status_and_reasoning():
    lead = _import_one()
    updated = leads.update_lead_classification(lead["id"], "HOT", "Confirmed pain point, gave email.")
    assert updated["status"] == "HOT"
    assert updated["classificationReasoning"] == "Confirmed pain point, gave email."


def test_update_lead_classification_rejects_invalid_status():
    lead = _import_one()
    with pytest.raises(leads.LeadsError):
        leads.update_lead_classification(lead["id"], "LUKEWARM", "nope")


# ---------------------------------------------------------------------------
# LLM: generate_lead_prompt / classify_lead_call (Fireworks mocked)
# ---------------------------------------------------------------------------
def test_generate_lead_prompt_happy_path(monkeypatch):
    lead = {
        "contactName": "Alexander Vance", "companyName": "Aether Dynamic",
        "industry": "Robotics", "companySize": "250", "seniority": "Director",
        "painPoints": ["high latency in outreach"],
    }

    def fake_stream_chat(model_id, reasoning_effort, history, on_token=None, **kwargs):
        return json.dumps({
            "first_message": "Hi Alexander, quick question about Aether Dynamic's outreach.",
            "system_prompt": "You are calling Alexander at Aether Dynamic about latency issues.",
        }), 0.1, 0.2

    monkeypatch.setattr(leads, "stream_chat", fake_stream_chat)
    result = leads.generate_lead_prompt(lead, vz_config.AGENT_PROFILE)
    assert "Alexander" in result["first_message"]
    assert "Aether Dynamic" in result["system_prompt"]


def test_generate_lead_prompt_strips_markdown_fences(monkeypatch):
    lead = {"contactName": "Jane", "companyName": "Acme", "industry": "", "companySize": "", "seniority": "", "painPoints": []}
    payload = {"first_message": "Hi Jane.", "system_prompt": "Call Jane at Acme."}

    def fake_stream_chat(model_id, reasoning_effort, history, on_token=None, **kwargs):
        return "```json\n" + json.dumps(payload) + "\n```", 0.1, 0.2

    monkeypatch.setattr(leads, "stream_chat", fake_stream_chat)
    result = leads.generate_lead_prompt(lead, vz_config.AGENT_PROFILE)
    assert result == payload


def test_generate_lead_prompt_raises_on_unparseable_response(monkeypatch):
    lead = {"contactName": "Jane", "companyName": "Acme", "industry": "", "companySize": "", "seniority": "", "painPoints": []}

    def fake_stream_chat(model_id, reasoning_effort, history, on_token=None, **kwargs):
        return "not json at all", 0.1, 0.2

    monkeypatch.setattr(leads, "stream_chat", fake_stream_chat)
    with pytest.raises(leads.LeadsError):
        leads.generate_lead_prompt(lead, vz_config.AGENT_PROFILE)


def test_generate_lead_prompt_raises_on_llm_failure(monkeypatch):
    lead = {"contactName": "Jane", "companyName": "Acme", "industry": "", "companySize": "", "seniority": "", "painPoints": []}

    def fake_stream_chat(*a, **k):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(leads, "stream_chat", fake_stream_chat)
    with pytest.raises(leads.LeadsError):
        leads.generate_lead_prompt(lead, vz_config.AGENT_PROFILE)


def test_classify_lead_call_happy_path(monkeypatch):
    lead = {"contactName": "Jane", "companyName": "Acme"}

    def fake_stream_chat(model_id, reasoning_effort, history, on_token=None, **kwargs):
        return json.dumps({"status": "hot", "reasoning": "Gave email and confirmed pain point."}), 0.1, 0.2

    monkeypatch.setattr(leads, "stream_chat", fake_stream_chat)
    result = leads.classify_lead_call(lead, "User: sure, my email is jane@acme.com")
    assert result["status"] == "HOT"  # uppercased
    assert "email" in result["reasoning"]


def test_classify_lead_call_raises_on_invalid_status(monkeypatch):
    lead = {"contactName": "Jane", "companyName": "Acme"}

    def fake_stream_chat(model_id, reasoning_effort, history, on_token=None, **kwargs):
        return json.dumps({"status": "MAYBE", "reasoning": "unsure"}), 0.1, 0.2

    monkeypatch.setattr(leads, "stream_chat", fake_stream_chat)
    with pytest.raises(leads.LeadsError):
        leads.classify_lead_call(lead, "some transcript")


# ---------------------------------------------------------------------------
# server.py: POST /leads/import, GET /leads, PATCH /leads/{id}
# ---------------------------------------------------------------------------
def test_endpoint_import_leads_multipart():
    with TestClient(server.app) as client:
        with open(APOLLO_CSV_PATH, "rb") as f:
            resp = client.post(
                "/leads/import", files={"file": ("apollo_demo.csv", f, "text/csv")}
            )
    assert resp.status_code == 200
    assert resp.json() == {"imported": 7, "skipped": 0}


def test_endpoint_import_leads_empty_file_rejected():
    with TestClient(server.app) as client:
        resp = client.post(
            "/leads/import", files={"file": ("empty.csv", io.BytesIO(b""), "text/csv")}
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["stage"] == "leads"


def test_endpoint_get_leads_and_filter_by_status():
    leads.import_csv(APOLLO_CSV_PATH.read_bytes())
    first_id = leads.list_leads()[0]["id"]
    leads.update_lead(first_id, {"status": "HOT"})

    with TestClient(server.app) as client:
        resp_all = client.get("/leads")
        resp_hot = client.get("/leads?status=HOT")
        resp_bad = client.get("/leads?status=LUKEWARM")

    assert resp_all.status_code == 200
    assert len(resp_all.json()) == 7
    assert resp_hot.status_code == 200
    assert len(resp_hot.json()) == 1
    assert resp_bad.status_code == 400


def test_endpoint_patch_lead_roundtrip_and_not_found():
    lead = _import_one()
    with TestClient(server.app) as client:
        resp = client.patch(f"/leads/{lead['id']}", json={"status": "WARM", "isBallena": True})
        resp_missing = client.patch("/leads/does-not-exist", json={"status": "HOT"})

    assert resp.status_code == 200
    assert resp.json()["status"] == "WARM"
    assert resp.json()["isBallena"] is True
    assert resp_missing.status_code == 404


def test_endpoint_patch_lead_rejects_invalid_status():
    lead = _import_one()
    with TestClient(server.app) as client:
        resp = client.patch(f"/leads/{lead['id']}", json={"status": "LUKEWARM"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# server.py: POST /leads/{id}/generate_prompt (LLM mocked)
# ---------------------------------------------------------------------------
def test_endpoint_generate_prompt_saves_onto_the_lead(monkeypatch):
    lead = _import_one()

    def fake_generate_lead_prompt(lead_dict, base_profile):
        return {"first_message": "Hi there.", "system_prompt": "Custom system prompt."}

    monkeypatch.setattr(leads, "generate_lead_prompt", fake_generate_lead_prompt)

    with TestClient(server.app) as client:
        resp = client.post(f"/leads/{lead['id']}/generate_prompt")

    assert resp.status_code == 200
    data = resp.json()
    assert data["customFirstMessage"] == "Hi there."
    assert data["customSystemPrompt"] == "Custom system prompt."

    # Persisted, not just returned.
    assert leads.get_lead(lead["id"])["customFirstMessage"] == "Hi there."


def test_endpoint_generate_prompt_not_found():
    with TestClient(server.app) as client:
        resp = client.post("/leads/does-not-exist/generate_prompt")
    assert resp.status_code == 404


def test_endpoint_generate_prompt_llm_failure_is_502(monkeypatch):
    lead = _import_one()

    def fake_generate_lead_prompt(lead_dict, base_profile):
        raise leads.LeadsError("Fireworks unreachable")

    monkeypatch.setattr(leads, "generate_lead_prompt", fake_generate_lead_prompt)

    with TestClient(server.app) as client:
        resp = client.post(f"/leads/{lead['id']}/generate_prompt")

    assert resp.status_code == 502
    assert resp.json()["error"]["stage"] == "leads"


# ---------------------------------------------------------------------------
# server.py: POST /leads/{id}/call — DEMO_SAFE_MODE, override registration
# ---------------------------------------------------------------------------
def test_endpoint_call_lead_always_dials_call_me_number(monkeypatch):
    lead = _import_one()
    real_lead_phone = lead["phone"]  # masked already — never a dialable number anyway

    captured = {}

    def fake_trigger_call(to, wss_url, extra_params=None):
        captured["to"] = to
        captured["extra_params"] = extra_params
        return "CA_FAKE_SID"

    monkeypatch.setattr(server, "trigger_call", fake_trigger_call)
    monkeypatch.setenv("CALL_ME_NUMBER", "+15555550100")
    monkeypatch.delenv("DEMO_SAFE_MODE", raising=False)  # default true

    with TestClient(server.app) as client:
        resp = client.post(f"/leads/{lead['id']}/call", json={
            "first_message": "Hi from override.", "system_prompt": "Custom prompt.",
        })

    assert resp.status_code == 200
    data = resp.json()
    assert data["to"] == "+15555550100"
    assert data["to"] != real_lead_phone
    assert data["demo_safe_mode"] is True
    assert captured["to"] == "+15555550100"
    assert captured["extra_params"]["lead_id"] == lead["id"]
    assert "override_key" in captured["extra_params"]


def test_endpoint_call_lead_still_dials_call_me_number_when_demo_safe_mode_off(monkeypatch):
    """No lead ever carries a real, dialable number (masked at import) — so
    even with DEMO_SAFE_MODE explicitly off, CALL_ME_NUMBER is still what's
    dialed. The response's demo_safe_mode field reports the flag honestly."""
    lead = _import_one()

    def fake_trigger_call(to, wss_url, extra_params=None):
        return "CA_FAKE_SID"

    monkeypatch.setattr(server, "trigger_call", fake_trigger_call)
    monkeypatch.setenv("CALL_ME_NUMBER", "+15555550100")
    monkeypatch.setenv("DEMO_SAFE_MODE", "false")

    with TestClient(server.app) as client:
        resp = client.post(f"/leads/{lead['id']}/call", json={})

    assert resp.status_code == 200
    data = resp.json()
    assert data["to"] == "+15555550100"
    assert data["demo_safe_mode"] is False


def test_endpoint_call_lead_fails_loud_without_call_me_number(monkeypatch):
    lead = _import_one()
    monkeypatch.delenv("CALL_ME_NUMBER", raising=False)

    with TestClient(server.app) as client:
        resp = client.post(f"/leads/{lead['id']}/call", json={})

    assert resp.status_code == 502
    assert resp.json()["error"]["stage"] == "call"


def test_endpoint_call_lead_not_found(monkeypatch):
    monkeypatch.setenv("CALL_ME_NUMBER", "+15555550100")
    with TestClient(server.app) as client:
        resp = client.post("/leads/does-not-exist/call", json={})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /ws/twilio: the lead override actually reaches CascadeSession, and
# agent_profile.json is never touched by any of this.
# ---------------------------------------------------------------------------
class _FakeCascadeSessionCapturingOverride:
    """Same duck-type as test_server_twilio.py's FakeCascadeSession, plus it
    captures `profile` and `system_prompt_override` so this test can assert
    the override actually reached CascadeSession's constructor."""

    instances = []

    def __init__(
        self, transport, stt_cfg, llm_cfg, tts_cfg, profile,
        call_id=None, channel="unknown", system_prompt_override=None,
    ):
        self.transport = transport
        self.profile = profile
        self.call_id = call_id
        self.channel = channel
        self.system_prompt_override = system_prompt_override
        self.started = False
        self.stopped = False
        _FakeCascadeSessionCapturingOverride.instances.append(self)

    async def start(self):
        self.started = True

    async def feed_audio(self, chunk):
        pass

    async def stop(self):
        self.stopped = True


def test_ws_twilio_applies_lead_override_without_touching_agent_profile_file(monkeypatch, tmp_path):
    _FakeCascadeSessionCapturingOverride.instances.clear()
    monkeypatch.setattr(server, "CascadeSession", _FakeCascadeSessionCapturingOverride)
    monkeypatch.setattr(server.vz_logger, "write_call_transcript", lambda *a, **k: None)
    monkeypatch.setattr(server.event_bus, "publish", lambda *a, **k: None)

    # Redirect agent_profile.json writes (there should be NONE in this test,
    # but redirecting the path is the same defensive belt-and-suspenders
    # pattern test_profile_endpoint.py uses) and snapshot the original
    # opening line to prove the global was never mutated.
    fake_profile_path = tmp_path / "agent_profile.json"
    monkeypatch.setattr(server, "AGENT_PROFILE_PATH", fake_profile_path)
    original_opening = vz_config.AGENT_PROFILE.get("agent_opening")

    lead_id = "lead-abc-123"
    override_key = "override-key-xyz"
    server._LEAD_CALL_OVERRIDES[override_key] = {
        "lead_id": lead_id,
        "first_message": "Hi, this is a personalized opening for you.",
        "system_prompt": "Custom per-lead system prompt.",
    }

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws/twilio") as ws:
            ws.send_json({
                "event": "start",
                "start": {
                    "streamSid": "MZ1",
                    "customParameters": {
                        "to": "+15555550100", "lead_id": lead_id, "override_key": override_key,
                    },
                },
            })
            ws.send_json({"event": "stop"})

    assert len(_FakeCascadeSessionCapturingOverride.instances) == 1
    session = _FakeCascadeSessionCapturingOverride.instances[0]
    assert session.profile["agent_opening"] == "Hi, this is a personalized opening for you."
    assert session.system_prompt_override == "Custom per-lead system prompt."
    assert session.started is True
    assert session.stopped is True

    # The override was consumed exactly once (no leak into a future call).
    assert override_key not in server._LEAD_CALL_OVERRIDES

    # agent_profile.json was never written, and the real in-memory global
    # profile's opening line is untouched.
    assert not fake_profile_path.exists()
    assert vz_config.AGENT_PROFILE.get("agent_opening") == original_opening


def test_ws_twilio_without_override_key_behaves_exactly_like_before(monkeypatch):
    """A regular POST /call prospect call (no lead_id/override_key at all)
    must still work with the plain, un-overridden global AGENT_PROFILE —
    this is the "77 previous tests stay green" regression guard for the
    new conditional kwarg logic in ws_twilio, expressed as its own test."""
    _FakeCascadeSessionCapturingOverride.instances.clear()
    monkeypatch.setattr(server, "CascadeSession", _FakeCascadeSessionCapturingOverride)
    monkeypatch.setattr(server.vz_logger, "write_call_transcript", lambda *a, **k: None)
    monkeypatch.setattr(server.event_bus, "publish", lambda *a, **k: None)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws/twilio") as ws:
            ws.send_json({
                "event": "start",
                "start": {"streamSid": "MZ2", "customParameters": {"to": "+15555550199"}},
            })
            ws.send_json({"event": "stop"})

    session = _FakeCascadeSessionCapturingOverride.instances[-1]
    assert session.profile is vz_config.AGENT_PROFILE  # the real global, unchanged
    assert session.system_prompt_override is None


def test_ws_twilio_with_lead_id_updates_last_call_fields_synchronously(monkeypatch):
    """The lastCallId/lastCallDate write happens synchronously in the
    `finally` block (unlike classification, which is fire-and-forget) — see
    server.py's ws_twilio — so it's observable immediately after the `with`
    block exits, with no timing flakiness."""
    _FakeCascadeSessionCapturingOverride.instances.clear()
    monkeypatch.setattr(server, "CascadeSession", _FakeCascadeSessionCapturingOverride)
    monkeypatch.setattr(server.vz_logger, "write_call_transcript", lambda *a, **k: None)
    monkeypatch.setattr(server.event_bus, "publish", lambda *a, **k: None)
    # Prevent the real (fire-and-forget) classification task from running a
    # real Fireworks call in the background during/after this test.
    # asyncio.create_task() requires an actual coroutine object, so this
    # replacement must itself be `async def`, not a lambda returning an
    # awaitable-duck-type.
    async def _noop_classify(*a, **k):
        return None

    monkeypatch.setattr(server, "_classify_lead_after_call", _noop_classify)

    lead = _import_one()

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws/twilio") as ws:
            ws.send_json({
                "event": "start",
                "start": {
                    "streamSid": "MZ3",
                    "customParameters": {"to": "+15555550100", "lead_id": lead["id"]},
                },
            })
            ws.send_json({"event": "stop"})

    updated = leads.get_lead(lead["id"])
    assert updated["lastCallId"] is not None
    assert updated["lastCallDate"] is not None


# ---------------------------------------------------------------------------
# server._classify_lead_after_call — unit test (avoids the fire-and-forget
# task's timing nondeterminism inside a TestClient websocket test above).
# ---------------------------------------------------------------------------
async def test_classify_lead_after_call_updates_status_and_reasoning(monkeypatch):
    lead = _import_one()

    monkeypatch.setattr(server.vz_logger, "get_call_transcript_text", lambda call_id: "User: yes, email is x@y.com")

    def fake_classify(lead_dict, transcript_text):
        assert transcript_text == "User: yes, email is x@y.com"
        return {"status": "HOT", "reasoning": "Gave email, confirmed pain point."}

    monkeypatch.setattr(leads, "classify_lead_call", fake_classify)

    await server._classify_lead_after_call(lead["id"], "20260710_120000_0100")

    updated = leads.get_lead(lead["id"])
    assert updated["status"] == "HOT"
    assert updated["classificationReasoning"] == "Gave email, confirmed pain point."


async def test_classify_lead_after_call_never_raises_on_llm_failure(monkeypatch):
    lead = _import_one()
    monkeypatch.setattr(server.vz_logger, "get_call_transcript_text", lambda call_id: "")

    def fake_classify(lead_dict, transcript_text):
        raise leads.LeadsError("Fireworks unreachable")

    monkeypatch.setattr(leads, "classify_lead_call", fake_classify)

    # Must not raise — status stays exactly as it was (COLD, the import default).
    await server._classify_lead_after_call(lead["id"], "20260710_120000_0100")
    assert leads.get_lead(lead["id"])["status"] == "COLD"


async def test_classify_lead_after_call_unknown_lead_is_a_noop(monkeypatch):
    calls = {"classify": 0}
    monkeypatch.setattr(server.vz_logger, "get_call_transcript_text", lambda call_id: "")

    def fake_classify(lead_dict, transcript_text):
        calls["classify"] += 1
        return {"status": "HOT", "reasoning": "x"}

    monkeypatch.setattr(leads, "classify_lead_call", fake_classify)
    await server._classify_lead_after_call("does-not-exist", "some-call-id")
    assert calls["classify"] == 0
