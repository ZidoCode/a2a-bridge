#!/usr/bin/env python3
"""
a2a-bridge relay — lightweight message relay for independent agents.

Stdlib only. One file. Run:

    A2A_SECRET=change-me A2A_PORT=8765 python3 a2a_relay.py

Endpoints:
    POST /send          {"agent": "zido", "message": {...}}  -> 202 {"stored": id}
    GET  /inbox?agent=  long-poll (default wait 30s) -> [msg, ...]
    DELETE /inbox/:id?agent=  ack a delivered message (removes it from queue)
    GET  /health        -> {"ok": true, "agents": [...]}
    POST /register      {"agent": "zido"}  -> registers agent id (no-op if exists)

Auth: every request must carry header  X-A2A-Secret: <secret>
Message integrity: HMAC-SHA256 per message-format.md v1.

Storage: $A2A_DATA (default ./a2a-data)
    state.json        agent registry + dedup id set
    outbox/<agent>/   pending messages addressed to <agent>
    dead-letter/      undeliverable/invalid messages + reason file
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ----------------------------------------------------------------- config
SECRET = os.environ.get("A2A_SECRET", "").encode()
PORT = int(os.environ.get("A2A_PORT", "8765"))
HOST = os.environ.get("A2A_HOST", "0.0.0.0")
DATA = Path(os.environ.get("A2A_DATA", "./a2a-data"))
MAX_BYTES = int(os.environ.get("A2A_MAX_BYTES", str(256 * 1024)))
LONG_POLL_S = int(os.environ.get("A2A_LONGPOLL_S", "30"))
DEDUP_KEEP = 20000

# condition that wakes long-pollers when a message lands
_new = threading.Condition()
# in-memory dedup + agent registry + send-index (persisted to state.json)
_state: dict = {"agents": {}, "seen": [], "sent": {}}
_lock = threading.Lock()

# ----------------------------------------------------------------- paths
DATA.mkdir(parents=True, exist_ok=True)
for sub in ("outbox", "dead-letter"):
    (DATA / sub).mkdir(exist_ok=True)
STATE_FILE = DATA / "state.json"


# ----------------------------------------------------------------- state
def _load_state() -> None:
    global _state
    if STATE_FILE.exists():
        try:
            _state.update(json.loads(STATE_FILE.read_text()))
        except Exception as e:  # corrupt state — start clean, keep file for ops
            print(f"[a2a-relay] WARNING: state.json unreadable ({e}); starting fresh", flush=True)
    _state.setdefault("agents", {})
    _state.setdefault("seen", [])
    _state.setdefault("sent", {})


def _save_state() -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_state))
    tmp.replace(STATE_FILE)


def _remember_id(mid: str) -> None:
    if mid not in _state["seen"][-500:]:
        _state["seen"].append(mid)
        _state["seen"] = _state["seen"][-DEDUP_KEEP:]


def _agent_dir(name: str) -> Path:
    return DATA / "outbox" / name


# ----------------------------------------------------------------- hmac
def canonical(m: dict) -> bytes:
    return (
        f"{m['id']}{m['from']}{m['to']}{m['type']}{m['timestamp']}"
        f"{json.dumps(m.get('content', ''), sort_keys=True, separators=(',', ':'))}"
    ).encode()


def sign(secret: bytes, m: dict) -> str:
    return _hmac.new(secret, canonical(m), hashlib.sha256).hexdigest()


# ----------------------------------------------------------------- validate
AGENT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")


def validate(m: dict) -> str | None:
    """Return None if valid, else a reason string."""
    if not isinstance(m, dict):
        return "message is not a JSON object"
    for f in ("id", "from", "to", "type", "content", "timestamp", "hmac"):
        if f not in m:
            return f"missing field '{f}'"
    if not isinstance(m["id"], str) or len(m["id"]) != 36 or m["id"].count("-") != 4:
        return "id must be a uuid-v4 string"
    if not AGENT_RE.match(m["from"]) or not AGENT_RE.match(m["to"]):
        return "from/to must match ^[a-z0-9][a-z0-9_-]{2,63}$"
    if m["type"] not in ("message", "task", "result", "error"):
        return f"type '{m['type']}' not in message|task|result|error"
    if m["type"] in ("result", "error"):
        rt = m.get("reply_to")
        if not rt or not isinstance(rt, str) or len(rt) != 36:
            return f"type '{m['type']}' requires reply_to (uuid of the original message)"
    if m["from"] == m["to"]:
        return "from and to must differ"
    if not isinstance(m["timestamp"], str) or "T" not in m["timestamp"]:
        return "timestamp must be ISO-8601"
    if len(json.dumps(m).encode()) > MAX_BYTES:
        return f"message exceeds {MAX_BYTES} bytes"
    return None


# ----------------------------------------------------------------- handler
class H(BaseHTTPRequestHandler):
    server_version = "a2a-bridge/1.0"

    def log_message(self, fmt, *args):  # quiet access log
        sys.stderr.write("[a2a-relay] %s %s\n" % (self.address_string(), fmt % args))

    # ---- helpers
    def _check_secret(self) -> bool:
        got = self.headers.get("X-A2A-Secret", "").encode()
        if not SECRET or not _hmac.compare_digest(got, SECRET):
            self._json(403, {"error": "bad secret"})
            return False
        return True

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self, limit: int) -> bytes:
        n = int(self.headers.get("Content-Length", 0))
        if n <= 0 or n > limit:
            return b""
        return self.rfile.read(n)

    # ---- routes
    def do_GET(self):
        if not self._check_secret():
            return
        u = urlparse(self.path)
        if u.path == "/health":
            with _lock:
                agents = list(_state["agents"].keys())
            self._json(200, {"ok": True, "agents": agents, "ts": time.time()})
            return
        if u.path == "/inbox":
            self._inbox(parse_qs(u.query).get("agent", [""])[0])
            return
        m = re.match(r"^/resolve/([0-9a-f-]+)$", u.path)
        if m:
            self._resolve(m.group(1))
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._check_secret():
            return
        u = urlparse(self.path)
        if u.path == "/register":
            self._register(self._body(4096))
            return
        if u.path == "/send":
            self._send(self._body(MAX_BYTES + 4096))
            return
        self._json(404, {"error": "not found"})

    def do_DELETE(self):
        if not self._check_secret():
            return
        m = re.match(r"^/inbox/([0-9a-f-]+)$", urlparse(self.path).path)
        if not m:
            self._json(404, {"error": "not found"})
            return
        agent = parse_qs(urlparse(self.path).query).get("agent", [""])[0]
        if not agent:
            self._json(400, {"error": "agent query param required"})
            return
        self._ack(agent, m.group(1))

    # ---- actions
    def _register(self, body: bytes) -> None:
        try:
            req = json.loads(body or b"{}")
            name = req.get("agent", "")
        except Exception:
            self._json(400, {"error": "invalid JSON"})
            return
        if not AGENT_RE.match(name):
            self._json(400, {"error": "agent id must match ^[a-z0-9][a-z0-9_-]{2,63}$"})
            return
        with _lock:
            _state["agents"].setdefault(name, {"registered_at": time.time()})
            _agent_dir(name).mkdir(parents=True, exist_ok=True)
            _save_state()
        self._json(200, {"ok": True, "agent": name})

    def _send(self, body: bytes) -> None:
        try:
            req = json.loads(body or b"{}")
            msg = req.get("message")
            agent_hint = req.get("agent")  # optional duplicate of msg["to"]
        except Exception:
            self._json(400, {"error": "invalid JSON"})
            return
        if not isinstance(msg, dict):
            self._json(400, {"error": "body must be {agent?, message: {...}}"})
            return
        err = validate(msg)
        if err:
            self._dead_letter(msg, "invalid: " + err)
            self._json(400, {"error": err})
            return
        # verify HMAC
        expect = sign(SECRET, msg)
        if not _hmac.compare_digest(expect.encode(), str(msg.get("hmac", "")).encode()):
            self._dead_letter(msg, "bad hmac")
            self._json(403, {"error": "bad hmac"})
            return
        to = msg["to"]
        with _lock:
            if msg["id"] in _state["seen"]:
                self._json(200, {"ok": True, "id": msg["id"], "duplicate": True})
                return
            _remember_id(msg["id"])
            _state["agents"].setdefault(to, {"registered_at": time.time()})
            _agent_dir(to).mkdir(parents=True, exist_ok=True)
            # remember sender/recipient for /resolve after ack
            _state["sent"][msg["id"]] = {"from": msg["from"], "to": to, "ts": time.time()}
            if len(_state["sent"]) > DEDUP_KEEP:
                for k in sorted(_state["sent"], key=lambda x: _state["sent"][x]["ts"])[:1000]:
                    _state["sent"].pop(k, None)
            _save_state()
        fname = f"msg-{msg['id']}.json"
        f = _agent_dir(to) / fname
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(msg))
        tmp.replace(f)
        with _new:
            _new.notify_all()
        self._json(202, {"stored": msg["id"], "to": to})

    def _inbox(self, agent: str) -> None:
        if not agent:
            self._json(400, {"error": "agent query param required"})
            return
        d = _agent_dir(agent)
        if not d.exists():
            # long-poll even if no dir yet (agent may register mid-wait)
            deadline = time.time() + LONG_POLL_S
            with _new:
                _new.wait(timeout=max(0, deadline - time.time()))
            d = _agent_dir(agent)
            if not d.exists():
                self._json(200, {"messages": []})
                return
        self._inbox_return(agent)

    def _inbox_return(self, agent: str) -> None:
        d = _agent_dir(agent)
        files = sorted(d.glob("msg-*.json"))
        if not files:
            # nothing yet — wait up to LONG_POLL_S for something to land
            deadline = time.time() + LONG_POLL_S
            with _new:
                _new.wait(timeout=max(0, deadline - time.time()))
            files = sorted(_agent_dir(agent).glob("msg-*.json"))
        # filter by HMAC (relay re-verifies on delivery — defense in depth)
        out = []
        for p in files:
            try:
                m = json.loads(p.read_text())
                if _hmac.compare_digest(sign(SECRET, m).encode(), str(m.get("hmac", "")).encode()):
                    out.append(m)
            except Exception:
                pass
        self._json(200, {"messages": out})

    def _resolve(self, mid: str) -> None:
        """Return the original sender+recipient of a message id.

        Lets a responder route a result/error back to the right agent without
        knowing it in advance. Checks the persistent send-index first (survives
        ack), then outbox dirs, then dead-letter.
        """
        with _lock:
            rec = _state["sent"].get(mid)
        if rec:
            self._json(200, {"id": mid, "sender": rec["from"], "recipient": rec["to"]})
            return
        for d in (DATA / "outbox").glob("*"):
            if not d.is_dir():
                continue
            f = d / f"msg-{mid}.json"
            if f.exists():
                try:
                    m = json.loads(f.read_text())
                    self._json(200, {"id": mid, "sender": m.get("from"), "recipient": m.get("to")})
                    return
                except Exception:
                    pass
        f = DATA / "dead-letter" / f"dl-{mid}.json"
        if f.exists():
            try:
                m = json.loads(f.read_text())
                self._json(200, {"id": mid, "sender": m.get("from"), "recipient": m.get("to"), "dead_letter": True})
                return
            except Exception:
                pass
        self._json(404, {"error": "id not found in relay"})

    def _ack(self, agent: str, mid: str) -> None:
        f = _agent_dir(agent) / f"msg-{mid}.json"
        if f.exists():
            f.unlink()
            self._json(200, {"acked": mid})
        else:
            self._json(404, {"error": "not found or already acked"})

    def _dead_letter(self, msg, reason: str) -> None:
        d = DATA / "dead-letter"
        try:
            mid = msg.get("id", str(uuid.uuid4())) if isinstance(msg, dict) else str(uuid.uuid4())
        except Exception:
            mid = str(uuid.uuid4())
        p = d / f"dl-{mid}.json"
        p.write_text(json.dumps(msg, default=str)[:4096])
        (d / f"dl-{mid}.reason.txt").write_text(reason)


def main() -> None:
    if not SECRET:
        print("[a2a-relay] FATAL: A2A_SECRET env var is empty — refusing to start unauthenticated", file=sys.stderr)
        sys.exit(1)
    _load_state()
    _save_state()
    srv = ThreadingHTTPServer((HOST, PORT), H)
    print(f"[a2a-relay] listening on {HOST}:{PORT}, data={DATA}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
