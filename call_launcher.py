"""
call_launcher.py — Phase 2 of Voxniac ONE: trigger a real outbound phone call
via Twilio that connects to the already-running /ws/twilio Media Streams
endpoint (see server.py).

Flow (CLI):
  1. Health-check the local server (GET http://127.0.0.1:<port>/config).
  2. Spawn a cloudflared quick tunnel exposing http://127.0.0.1:<port>.
  3. Build the wss:// URL for /ws/twilio from the tunnel hostname.
  4. Wait a few seconds for DNS propagation, then trigger the outbound call
     via Twilio (unless --dry-run, which stops right before calls.create()).
  5. Keep the tunnel process alive until Ctrl+C so the call has somewhere to
     stream audio to/from.

Usage:
  python call_launcher.py [<to_number>] [--port 8080] [--dry-run]

If <to_number> is omitted, it's read from the CALL_ME_NUMBER environment
variable (set it in the ..\\.env file) — never hardcode a real phone number
in source, this repo is shared for the AMD hackathon.

Example:
  python call_launcher.py --dry-run           (uses CALL_ME_NUMBER from .env)
  python call_launcher.py +15555550100 --dry-run
"""

import argparse
import os
import queue
import re
import subprocess
import sys
import threading
import time

import requests
from twilio.rest import Client

from vz_config import TWILIO_PHONE_NUMBER, TWILIO_SID, TWILIO_TOKEN

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_CLOUDFLARED_PATH = (
    r"C:\Users\gabri\Desktop\SINGULARITYOS\DESTKTOP PYTHON\Neural Sales"
    r"\monitoreo vapi\cloudflared.exe"
)
TUNNEL_URL_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
TUNNEL_TIMEOUT_S = 60
DNS_PROPAGATION_WAIT_S = 3
HEALTH_CHECK_TIMEOUT_S = 5


# ---------------------------------------------------------------------------
# trigger_call
# ---------------------------------------------------------------------------
def _escape_xml_attr(value: str) -> str:
    """Minimal XML attribute escaping for TwiML <Parameter value="...">
    (Twilio's own TwiML is XML) — avoids pulling in a full XML library for
    two characters. Not a general-purpose escaper; only used for values this
    codebase itself controls (phone numbers, lead ids, uuid4 hex keys)."""
    return (value or "").replace("&", "&amp;").replace('"', "&quot;")


def trigger_call(to_number: str, wss_url: str, extra_params: "dict | None" = None) -> str:
    """
    Fires a real outbound call via the Twilio REST API and connects it to
    wss_url using a <Connect><Stream> TwiML verb. Returns the call SID.

    Fails loud (raises RuntimeError) if TWILIO_SID / TWILIO_TOKEN /
    TWILIO_PHONE_NUMBER are missing from the environment/.env — this must
    never silently no-op.

    extra_params: Phase 4 Etapa C — optional extra Twilio Media Streams
    custom Stream Parameters (e.g. {"lead_id": "...", "override_key": "..."})
    appended alongside the existing "to" parameter, so /ws/twilio's "start"
    event can look up a per-call prompt override (see server.py's
    _LEAD_CALL_OVERRIDES) without touching agent_profile.json. Default None
    preserves the exact previous TwiML for every existing caller.
    """
    missing = [
        name
        for name, value in (
            ("TWILIO_SID", TWILIO_SID),
            ("TWILIO_TOKEN", TWILIO_TOKEN),
            ("TWILIO_PHONE_NUMBER", TWILIO_PHONE_NUMBER),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"Missing Twilio credentials: {', '.join(missing)}. "
            r"Set them in C:\Users\gabri\Desktop\voxniac\.env before calling."
        )

    # Phase 3.5 P1: pass the destination number through as a Twilio Media
    # Streams custom Stream Parameter — Twilio's "start" event otherwise
    # carries no phone number at all, and /ws/twilio needs it (last 4 digits)
    # to build the call's call_id (see transports.parse_twilio_event).
    extra_tags = ""
    for key, value in (extra_params or {}).items():
        extra_tags += f'<Parameter name="{_escape_xml_attr(key)}" value="{_escape_xml_attr(str(value))}"/>'

    twiml = (
        f'<Response><Connect><Stream url="{wss_url}">'
        f'<Parameter name="to" value="{to_number}"/>'
        f"{extra_tags}"
        f"</Stream></Connect></Response>"
    )
    client = Client(TWILIO_SID, TWILIO_TOKEN)
    call = client.calls.create(twiml=twiml, to=to_number, from_=TWILIO_PHONE_NUMBER)
    return call.sid


# ---------------------------------------------------------------------------
# start_tunnel
# ---------------------------------------------------------------------------
def _cloudflared_path() -> str:
    return os.environ.get("CLOUDFLARED_PATH", DEFAULT_CLOUDFLARED_PATH)


def start_tunnel(port: int = 8080):
    """
    Spawns a cloudflared quick tunnel pointing at http://127.0.0.1:<port> and
    parses its output (stderr merged into stdout, where cloudflared prints
    the tunnel URL) for a https://*.trycloudflare.com URL. Waits up to
    TUNNEL_TIMEOUT_S seconds for it to appear.

    Returns (process: subprocess.Popen, host: str) where host is the bare
    "xxxx.trycloudflare.com" hostname (no scheme).

    Fail-loud: raises RuntimeError if cloudflared.exe is missing, or if no
    tunnel URL appears within the timeout (the spawned process is killed
    first in that case).
    """
    cloudflared_path = _cloudflared_path()
    if not os.path.isfile(cloudflared_path):
        raise RuntimeError(
            f"cloudflared.exe not found at '{cloudflared_path}'. "
            "Set the CLOUDFLARED_PATH environment variable to override the "
            "default location."
        )

    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    process = subprocess.Popen(
        [cloudflared_path, "tunnel", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="ignore",
        creationflags=flags,
    )

    # Read cloudflared's output on a background thread so we can enforce a
    # hard wall-clock timeout even if the process stalls without producing
    # any more output (a blocking `for line in process.stdout` loop would not
    # respect a timeout on its own).
    line_queue: "queue.Queue[str]" = queue.Queue()

    def _pump():
        try:
            for line in process.stdout:
                line_queue.put(line)
        except Exception:
            pass

    reader = threading.Thread(target=_pump, daemon=True)
    reader.start()

    host = None
    deadline = time.monotonic() + TUNNEL_TIMEOUT_S
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            line = line_queue.get(timeout=max(0.1, min(1.0, remaining)))
        except queue.Empty:
            if time.monotonic() < deadline:
                continue
            break
        
        match = TUNNEL_URL_RE.search(line)
        if match:
            host = match.group(0).replace("https://", "")
            break

    if not host:
        process.kill()
        raise RuntimeError(
            f"cloudflared did not print a trycloudflare.com URL within "
            f"{TUNNEL_TIMEOUT_S}s. Check that cloudflared.exe works "
            "standalone and that the internet connection is up."
        )

    return process, host


def _stop_tunnel(process: subprocess.Popen):
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


# ---------------------------------------------------------------------------
# Local server health check
# ---------------------------------------------------------------------------
def _health_check(port: int):
    url = f"http://127.0.0.1:{port}/config"
    try:
        resp = requests.get(url, timeout=HEALTH_CHECK_TIMEOUT_S)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Voxniac ONE server is not reachable at {url} ({exc}). "
            f"Start it first, e.g.: RUN_VOXNIAC_ONE.bat, or run "
            f"`uvicorn server:app --host 127.0.0.1 --port {port}` from "
            "voxniac-zero-ONE\\."
        ) from exc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description=(
            "Trigger a real outbound Twilio call into Voxniac ONE's "
            "/ws/twilio Media Streams endpoint via a cloudflared tunnel."
        )
    )
    parser.add_argument(
        "to_number",
        nargs="?",
        default=None,
        help=(
            "Destination phone number in E.164 format, e.g. +15555550100. "
            "If omitted, reads the CALL_ME_NUMBER environment variable "
            "(set it in ..\\.env) — never hardcode a real number here."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Local port where server.py (uvicorn) is running (default: 8080)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Do everything except calls.create(): still opens the tunnel, "
            "prints the TwiML and wss URL that would be used, then closes "
            "the tunnel and exits."
        ),
    )
    args = parser.parse_args()

    to_number = args.to_number or os.environ.get("CALL_ME_NUMBER")
    if not to_number:
        print(
            "[ERROR] No destination number given and CALL_ME_NUMBER is not set.\n"
            "        Either pass one as an argument (python call_launcher.py +1...) "
            "or set CALL_ME_NUMBER=+1... in C:\\Users\\gabri\\Desktop\\voxniac\\.env"
        )
        sys.exit(1)

    print(f"[1/4] Health-checking Voxniac ONE server on port {args.port}...")
    _health_check(args.port)
    print("      OK: server is up.")

    print("[2/4] Starting cloudflared tunnel...")
    process, host = start_tunnel(args.port)
    print(f"      OK: tunnel is up at https://{host}")

    wss_url = f"wss://{host}/ws/twilio"
    twiml = (
        f'<Response><Connect><Stream url="{wss_url}">'
        f'<Parameter name="to" value="{to_number}"/>'
        f"</Stream></Connect></Response>"
    )

    print(f"[3/4] Waiting {DNS_PROPAGATION_WAIT_S}s for DNS propagation...")
    time.sleep(DNS_PROPAGATION_WAIT_S)

    if args.dry_run:
        print("[4/4] --dry-run: NOT calling Twilio. Would use:")
        print(f"      wss URL: {wss_url}")
        print(f"      TwiML:   {twiml}")
        print("Shutting down tunnel (dry run complete).")
        _stop_tunnel(process)
        return

    print(f"[4/4] Triggering outbound call to {to_number}...")
    call_sid = trigger_call(to_number, wss_url)
    print(f"      OK: call triggered. SID: {call_sid}")
    print("Tunnel stays open for the call. Press Ctrl+C to stop and close it.")

    try:
        process.wait()
    except KeyboardInterrupt:
        print("\nCtrl+C received. Closing tunnel...")
        _stop_tunnel(process)


if __name__ == "__main__":
    main()
