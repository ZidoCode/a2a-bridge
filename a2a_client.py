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
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from a2a_hmac import sign


def _env() -> tuple[str, str, str]:
    relay = os.environ.get("A2A_RELAY", "http://127.0.0.1:8765").rstrip("/")
    secret = os.environ.get("A2A_SECRET", "")
    agent = os.environ.get("A2A_AGENT", "")
    if not secret or not agent:
        sys.exit("set A2A_RELAY, A2A_SECRET, A2A_AGENT")
    return relay, secret, agent


def _req(method: str, path: str, payload=None, secret: str = "") -> dict:
    headers = {"X-A2A-Secret": secret}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=120) as resp:
        return json.loads(resp.read())


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
        r = _req("GET", f"{relay}/resolve/{reply_to}", secret=secret)
        if "error" in r:
            return r
        to = r.get("sender", to)
    m = _msg(secret, agent, to, mtype, content, reply_to)
    try:
        return _req("POST", relay + "/send", {"message": m}, secret)
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()[:500], "status": e.code}


def poll(wait_s: int = 30) -> list:
    relay, secret, agent = _env()
    return _req("GET", f"{relay}/inbox?agent={agent}", secret=secret).get("messages", [])


def ack(mid: str) -> dict:
    relay, secret, agent = _env()
    try:
        return _req("DELETE", f"{relay}/inbox/{mid}?agent={agent}", secret=secret)
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()[:500], "status": e.code}


def main() -> None:
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    cmd = a[0]
    arg1 = a[1] if len(a) > 1 else ""
    arg2 = a[2] if len(a) > 2 else ""
    if cmd == "send":
        print(json.dumps(send(arg1, arg2), indent=2))
    elif cmd == "task":
        content = json.loads(arg2) if arg2 and arg2.startswith(("{", "[")) else arg2
        print(json.dumps(send(arg1, content, "task"), indent=2))
    elif cmd == "result":
        print(json.dumps(send("", arg2, "result", reply_to=arg1), indent=2))
    elif cmd == "error":
        print(json.dumps(send("", arg2, "error", reply_to=arg1), indent=2))
    elif cmd == "poll":
        msgs = poll(int(arg1) if arg1 and arg1.isdigit() else 30)
        print(json.dumps(msgs, indent=2))
        for m in msgs:
            ack(m["id"])
    elif cmd == "ack":
        print(json.dumps(ack(arg1), indent=2))
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
