#!/usr/bin/env python3
"""
a2a-bridge Python client. Stdlib only.

Usage:
    export A2A_RELAY=http://127.0.0.1:8765
    export A2A_SECRET=change-me
    export A2A_AGENT=hermes

    python3 a2a_client.py send <to> "<text>"
    python3 a2a_client.py task <to> "<json-or-text>"
    python3 a2a_client.py result <reply_to_id> "<content>"
    python3 a2a_client.py error <reply_to_id> "<content>"
    python3 a2a_client.py poll [wait_s]    # long-poll once, print, ack
    python3 a2a_client.py ack <id>
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from a2a_hmac import sign

EXIT_USAGE = 2
EXIT_NET = 1


class RelayError(Exception):
    """Transport-level failure (unreachable relay, bad URL, timeout)."""


def _env() -> tuple[str, str, str]:
    relay = os.environ.get("A2A_RELAY", "http://127.0.0.1:8765").rstrip("/")
    secret = os.environ.get("A2A_SECRET", "")
    agent = os.environ.get("A2A_AGENT", "")
    if not secret:
        raise RelayError("set A2A_SECRET (the relay secret)")
    if not agent:
        raise RelayError("set A2A_AGENT (this agent's id)")
    if "://" not in relay:
        raise RelayError(f"A2A_RELAY must be a URL, got {relay!r}")
    return relay, secret, agent


def _req(method: str, path: str, payload=None, secret: str = "", timeout: int = 120) -> dict:
    headers = {"X-A2A-Secret": secret}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        # the relay answers errors as JSON; surface it instead of a stack trace
        try:
            return {"error": json.loads(e.read()).get("error", "http error"), "status": e.code}
        except Exception:
            return {"error": f"http {e.code}", "status": e.code}
    except urllib.error.URLError as e:
        raise RelayError(f"cannot reach relay at {path}: {e.reason}") from e
    except (TimeoutError, OSError) as e:
        raise RelayError(f"cannot reach relay at {path}: {e}") from e
    try:
        return json.loads(raw)
    except Exception:
        raise RelayError(f"relay returned non-JSON: {raw[:200]!r}")


def _msg(secret: str, frm: str, to: str, mtype: str, content, reply_to=None) -> dict:
    m = {
        "id": str(uuid.uuid4()),
        "from": frm,
        "to": to,
        "type": mtype,
        "content": content,
        "reply_to": reply_to,
        "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    m["hmac"] = sign(secret.encode(), m)
    return m


def send(to: str, content, mtype: str = "message", reply_to=None) -> dict:
    relay, secret, agent = _env()
    if mtype in ("result", "error"):
        if not reply_to:
            return {"error": "result/error require reply_to"}
        r = _req("GET", f"{relay}/resolve/{urllib.parse.quote(reply_to)}", secret=secret)
        if "error" in r:
            return r
        to = r.get("sender", to)
        if not to:
            return {"error": "could not resolve sender"}
    m = _msg(secret, agent, to, mtype, content, reply_to)
    return _req("POST", relay + "/send", {"message": m}, secret)


def poll(wait_s: int = 30) -> list:
    relay, secret, agent = _env()
    q = urllib.parse.urlencode({"agent": agent, "wait": int(wait_s)})
    return _req("GET", f"{relay}/inbox?{q}", secret=secret, timeout=int(wait_s) + 30).get("messages", [])


def ack(mid: str) -> dict:
    relay, secret, agent = _env()
    q = urllib.parse.urlencode({"agent": agent})
    return _req("DELETE", f"{relay}/inbox/{urllib.parse.quote(mid)}?{q}", secret=secret)


def _parse_content(raw: str):
    """JSON when it looks like JSON, else the raw string. Bad JSON is an error, not a crash."""
    s = raw.strip()
    if s[:1] in ("{", "["):
        try:
            return json.loads(s)
        except json.JSONDecodeError as e:
            raise RelayError(f"argument is not valid JSON: {e}") from e
    return raw


def main() -> int:
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = a[0]
    arg1 = a[1] if len(a) > 1 else ""
    arg2 = a[2] if len(a) > 2 else ""
    try:
        if cmd == "send":
            print(json.dumps(send(arg1, arg2), indent=2))
        elif cmd == "task":
            print(json.dumps(send(arg1, _parse_content(arg2), "task"), indent=2))
        elif cmd == "result":
            print(json.dumps(send("", arg2, "result", reply_to=arg1), indent=2))
        elif cmd == "error":
            print(json.dumps(send("", arg2, "error", reply_to=arg1), indent=2))
        elif cmd == "poll":
            try:
                wait = int(arg1) if arg1 else 30
            except ValueError:
                print(f"wait_s must be an integer, got {arg1!r}", file=sys.stderr)
                return EXIT_USAGE
            msgs = poll(max(0, wait))
            print(json.dumps(msgs, indent=2))
            for m in msgs:
                ack(m["id"])
        elif cmd == "ack":
            print(json.dumps(ack(arg1), indent=2))
        else:
            print(f"unknown command {cmd!r}\n", file=sys.stderr)
            print(__doc__)
            return EXIT_USAGE
    except RelayError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_NET
    return 0


if __name__ == "__main__":
    sys.exit(main())
