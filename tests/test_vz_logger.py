"""Tests for vz_logger.py's Phase 3.5 P1 additions: make_call_id() and
write_call_transcript(). No network."""

import re

import vz_logger


# ---------------------------------------------------------------------------
# make_call_id
# ---------------------------------------------------------------------------
def test_make_call_id_twilio_uses_last_4_digits_of_phone():
    call_id = vz_logger.make_call_id(phone="+13075550100")
    assert re.fullmatch(r"\d{8}_\d{6}_0100", call_id)


def test_make_call_id_twilio_falls_back_to_0000_without_phone():
    call_id = vz_logger.make_call_id(phone=None)
    assert call_id.endswith("_0000")


def test_make_call_id_twilio_falls_back_to_0000_for_short_phone():
    call_id = vz_logger.make_call_id(phone="12")
    assert call_id.endswith("_0000")


def test_make_call_id_browser_uses_prefix_and_no_phone():
    call_id = vz_logger.make_call_id(prefix="browser")
    assert call_id.startswith("browser_")
    assert re.fullmatch(r"browser_\d{8}_\d{6}", call_id)


def test_make_call_id_never_raises_on_weird_phone():
    # Non-digit garbage should just yield the 0000 fallback, not raise.
    call_id = vz_logger.make_call_id(phone="not-a-phone-number")
    assert call_id.endswith("_0000")


# ---------------------------------------------------------------------------
# write_call_transcript
# ---------------------------------------------------------------------------
def test_write_call_transcript_renders_turns_from_the_jsonl_log(tmp_path, monkeypatch):
    log_path = tmp_path / "log.jsonl"
    transcripts_dir = tmp_path / "transcripts"
    monkeypatch.setattr(vz_logger, "LOG_PATH", log_path)
    monkeypatch.setattr(vz_logger, "TRANSCRIPTS_DIR", transcripts_dir)

    call_id = "20260709_143207_0100"
    vz_logger.log_turn({
        "call_id": call_id,
        "channel": "twilio",
        "llm_model": "accounts/fireworks/models/kimi-k2p6",
        "user_text": "Hi, who is this?",
        "agent_text": "This is Sharon with SingularityOS.",
        "stt_final_s": 0.5,
        "ttft_s": 1.1,
        "ttfa_s": 1.3,
        "e2e_s": 1.4,
    })
    # A turn from a DIFFERENT call must not leak into this transcript.
    vz_logger.log_turn({
        "call_id": "some_other_call",
        "channel": "twilio",
        "llm_model": "m",
        "user_text": "unrelated",
        "agent_text": "unrelated reply",
    })

    path = vz_logger.write_call_transcript(
        call_id, "+13075550100", "2026-07-09T14:32:07+00:00", "2026-07-09T14:33:00+00:00",
        "accounts/fireworks/models/kimi-k2p6",
    )

    assert path is not None
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert call_id in content
    assert "+…0100" in content  # masked phone, not the full number
    assert "13075550100" not in content  # full number never appears
    assert "Hi, who is this?" in content
    assert "This is Sharon with SingularityOS." in content
    assert "unrelated" not in content  # the other call's turn is excluded
    assert "ttft_s=1.1s" in content


def test_write_call_transcript_never_raises_even_after_an_exception(tmp_path, monkeypatch):
    """Mirrors server.py's ws_twilio `finally` block contract: the transcript
    must still be written even if something went wrong earlier in the call."""
    log_path = tmp_path / "log.jsonl"
    transcripts_dir = tmp_path / "transcripts"
    monkeypatch.setattr(vz_logger, "LOG_PATH", log_path)
    monkeypatch.setattr(vz_logger, "TRANSCRIPTS_DIR", transcripts_dir)

    call_id = "callDropped"
    vz_logger.log_turn({
        "call_id": call_id, "channel": "twilio", "llm_model": "m",
        "user_text": "hello", "agent_text": "hi there",
    })

    written_path = None
    try:
        try:
            raise RuntimeError("simulated call drop mid-turn")
        finally:
            written_path = vz_logger.write_call_transcript(call_id, "+13075550100", None, None, "m")
    except RuntimeError:
        pass

    assert written_path is not None
    assert written_path.exists()
    assert "hi there" in written_path.read_text(encoding="utf-8")


def test_write_call_transcript_handles_missing_log_gracefully(tmp_path, monkeypatch):
    monkeypatch.setattr(vz_logger, "LOG_PATH", tmp_path / "does_not_exist.jsonl")
    monkeypatch.setattr(vz_logger, "TRANSCRIPTS_DIR", tmp_path / "transcripts")

    path = vz_logger.write_call_transcript("callNoTurns", None, None, None, None)
    assert path is not None
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "No turns were logged" in content


def test_write_call_transcript_never_raises_on_unwritable_dir(monkeypatch):
    # Point TRANSCRIPTS_DIR at something that can't be created as a directory
    # (a path whose parent is a FILE, not a dir) to force an OSError, and
    # confirm write_call_transcript swallows it instead of propagating.
    bogus_parent = vz_logger.THIS_DIR / "voxniac_one_log.jsonl"  # this is a file, not a dir
    monkeypatch.setattr(vz_logger, "TRANSCRIPTS_DIR", bogus_parent / "transcripts")

    result = vz_logger.write_call_transcript("callX", None, None, None, None)
    assert result is None
