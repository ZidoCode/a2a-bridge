#!/usr/bin/env python3
"""
a2a-bridge relay — lightweight message relay for independent agents.

Stdlib only. One file. Run:

    A2A_SECRET=change-me A2A_PORT=8765 python3 a2a_relay.py

Endpoints:
    POST /send          {"agent": "zido", "message": {...}}  -> 202 {"stored": id}
    GET  /inbox?agent=&wait=  long-poll (default wait 30s) -> {"messages": [...]}
    DELETE /inbox/:id?agent=  ack a delivered message (removes it from queue)
    GET  /health        -> {"ok": true, "agents": [...]}
    POST /register      {"agent": "zido"}  -> registers agent id (no-op if exists)
    GET  /resolve/:id   -> {"sender": ..., "recipient": ...}

Auth: every request must carry header  X-A2A-Secret: <secret>
Message integrity: HMAC-SHA256 per message-format.md v1.1.

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
from datetime import datetime
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
# Access logging is OFF by default. Every request used to write a line to stderr;
# when stderr is a pipe nobody drains (systemd without a journal reader, a
# subprocess.PIPE, Docker without log rotation) the 64 KiB pipe buffer fills and
# the write blocks INSIDE the request handler — the whole relay wedges after a few
# hundred requests. Startup lines and errors still go to stderr.
ACCESS_LOG = os.environ.get("A2A_ACCESS_LOG", "0").strip().lower() in ("1", "true", "yes")
DEDUP_KEEP = 20000
# state.json is rewritten at most this often (seconds); a crash can lose at most
# this window of dedup ids, which at-least-once delivery tolerates.
SAVE_INTERVAL = 1.0

# condition that wakes long-pollers when a message lands
_new = threading.Condition()
# in-memory dedup + agent registry + send-index (persisted to state.json)
_state: dict = {"agents": {}, "seen": [], "sent": {}}
_lock = threading.Lock()
_last_save = 0.0

# ----------------------------------------------------------------- paths
DATA.mkdir(parents=True, exist_ok=True)
for sub in ("outbox", "dead-letter"):
    (DATA / sub).mkdir(exist_ok=True)
STATE_FILE = DATA / "state.json"


# ----------------------------------------------------------------- state
def _load_state() -> None:
    if STATE_FILE.exists():
        try:
            loaded = json.loads(STATE_FILE.read_text())
            if isinstance(loaded, dict):
                _state.update(loaded)
        except Exception as e:  # corrupt state — start clean, keep file for ops
            print(f"[a2a-relay] WARNING: state.json unreadable ({e}); starting fresh", flush=True)
    if not isinstance(_state.get("agents"), dict):
        _state["agents"] = {}
    if not isinstance(_state.get("seen"), list):
        _state["seen"] = []
    if not isinstance(_state.get("sent"), dict):
        _state["sent"] = {}


def _save_state(force: bool = False) -> None:
    """Persist state. Caller must hold _lock.

    Writes are debounced: with 20k dedup ids the payload is megabytes, so
    rewriting it on every single send would turn delivery into O(state). Losing
    the last second of dedup ids only risks a duplicate delivery, which the
    at-least-once contract already allows.
    """
    global _last_save
    now = time.time()
    if not force and (now - _last_save) < SAVE_INTERVAL:
        return
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_state))
    tmp.replace(STATE_FILE)
    _last_save = now


def _remember_id(mid: str) -> None:
    if mid not in _state["seen"][-500:]:
        _state["seen"].append(mid)
        _state["seen"] = _state["seen"][-DEDUP_KEEP:]


def _agent_dir(name: str) -> Path:
    return DATA / "outbox" / name


# ----------------------------------------------------------------- hmac
def canonical(m: dict) -> bytes:
    """Canonical bytes covered by the HMAC (message-format.md v1.1).

    reply_to is included: it drives result routing, so leaving it unsigned let a
    tampered copy redirect a reply without invalidating the signature.
    """
    return (
        f"{m['id']}{m['from']}{m['to']}{m['type']}{m['timestamp']}"
        f"{json.dumps(m.get('content', ''), sort_keys=True, separators=(',', ':'))}"
        f"{json.dumps(m.get('reply_to'), sort_keys=True, separators=(',', ':'))}"
    ).encode()


def sign(secret: bytes, m: dict) -> str:
    return _hmac.new(secret, canonical(m), hashlib.sha256).hexdigest()


# ----------------------------------------------------------------- validate
AGENT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _iso_ok(ts) -> bool:
    if not isinstance(ts, str) or "T" not in ts:
        return False
    try:
        datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return True
    except Exception:
        return False


def validate(m: dict) -> str | None:
    """Return None if valid, else a reason string. Never raises."""
    if not isinstance(m, dict):
        return "message is not a JSON object"
    for f in ("id", "from", "to", "type", "content", "timestamp", "hmac"):
        if f not in m:
            return f"missing field '{f}'"
    if not isinstance(m["id"], str) or not UUID_RE.match(m["id"]):
        return "id must be a uuid-v4 string"
    for f in ("from", "to"):
        if not isinstance(m[f], str) or not AGENT_RE.match(m[f]):
            return "from/to must match ^[a-z0-9][a-z0-9_-]{2,63}$"
    if not isinstance(m["type"], str) or m["type"] not in ("message", "task", "result", "error"):
        return "type must be message|task|result|error"
    if m["type"] in ("result", "error"):
        rt = m.get("reply_to")
        if not isinstance(rt, str) or not UUID_RE.match(rt):
            return f"type '{m['type']}' requires reply_to (uuid of the original message)"
    elif m.get("reply_to") is not None and not (
        isinstance(m["reply_to"], str) and UUID_RE.match(m["reply_to"])
    ):
        return "reply_to must be a uuid string when present"
    if m["from"] == m["to"]:
        return "from and to must differ"
    if not _iso_ok(m["timestamp"]):
        return "timestamp must be ISO-8601"
    if not isinstance(m["hmac"], str) or len(m["hmac"]) != 64:
        return "hmac must be a sha256 hex digest"
    try:
        if len(json.dumps(m).encode()) > MAX_BYTES:
            return f"message exceeds {MAX_BYTES} bytes"
    except Exception:
        return "message is not JSON-serialisable"
    return None


# ----------------------------------------------------------------- handler
class H(BaseHTTPRequestHandler):
    server_version = "a2a-bridge/1.1"

    def log_message(self, fmt, *args):  # access log, opt-in only
        if not ACCESS_LOG:
            return
        try:
            sys.stderr.write("[a2a-relay] %s %s\n" % (self.address_string(), fmt % args))
        except Exception:
            pass

    # ---- helpers
    def _check_secret(self) -> bool:
        got = self.headers.get("X-A2A-Secret", "").encode()
        if not SECRET or not _hmac.compare_digest(got, SECRET):
            self._json(403, {"error": "bad secret"})
            return False
        return True

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # peer hung up mid-response (curl killed, client timeout). Nothing to
            # report and nothing to fix — swallow it so the thread does not spew a
            # traceback into the operator's log.
            self.close_connection = True

    def _body(self, limit: int):
        """Read the request body. Returns bytes, or None when the body is too large."""
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
        except (TypeError, ValueError):
            return None
        if n < 0 or n > limit:
            return None
        if n == 0:
            return b""
        return self.rfile.read(n)

    def handle_one_request(self):
        """Never let an unexpected exception kill the connection silently."""
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as e:  # pragma: no cover - defensive
            sys.stderr.write(f"[a2a-relay] ERROR {type(e).__name__}: {e}\n")
            try:
                self._json(500, {"error": "internal error"})
            except Exception:
                pass

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
            q = parse_qs(u.query)
            agent = q.get("agent", [""])[0]
            wait = _clamp_wait(q.get("wait", [""])[0])
            self._inbox(agent, wait)
            return
        m = re.match(r"^/resolve/([0-9a-f-]{36})$", u.path)
        if m:
            self._resolve(m.group(1))
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._check_secret():
            return
        u = urlparse(self.path)
        if u.path == "/register":
            body = self._body(4096)
            if body is None:
                self._json(413, {"error": "body too large"})
                return
            self._register(body)
            return
        if u.path == "/send":
            body = self._body(MAX_BYTES + 4096)
            if body is None:
                self._json(413, {"error": f"body exceeds {MAX_BYTES + 4096} bytes"})
                return
            self._send(body)
            return
        self._json(404, {"error": "not found"})

    def do_DELETE(self):
        if not self._check_secret():
            return
        u = urlparse(self.path)
        m = re.match(r"^/inbox/([0-9a-f-]{36})$", u.path)
        if not m:
            self._json(404, {"error": "not found"})
            return
        agent = parse_qs(u.query).get("agent", [""])[0]
        if not agent:
            self._json(400, {"error": "agent query param required"})
            return
        if not AGENT_RE.match(agent):
            self._json(400, {"error": "agent id must match ^[a-z0-9][a-z0-9_-]{2,63}$"})
            return
        self._ack(agent, m.group(1))

    # ---- actions
    def _register(self, body: bytes) -> None:
        try:
            req = json.loads(body or b"{}")
        except Exception:
            self._json(400, {"error": "invalid JSON"})
            return
        if not isinstance(req, dict):
            self._json(400, {"error": "body must be a JSON object"})
            return
        name = req.get("agent")
        if not isinstance(name, str) or not AGENT_RE.match(name):
            self._json(400, {"error": "agent id must match ^[a-z0-9][a-z0-9_-]{2,63}$"})
            return
        with _lock:
            _state["agents"].setdefault(name, {"registered_at": time.time()})
            _agent_dir(name).mkdir(parents=True, exist_ok=True)
            _save_state(force=True)
        self._json(200, {"ok": True, "agent": name})

    def _send(self, body: bytes) -> None:
        try:
            req = json.loads(body or b"{}")
        except Exception:
            self._json(400, {"error": "invalid JSON"})
            return
        if not isinstance(req, dict):
            self._json(400, {"error": "body must be {agent?, message: {...}}"})
            return
        msg = req.get("message")
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
        if not _hmac.compare_digest(expect.encode(), msg["hmac"].lower().encode()):
            self._dead_letter(msg, "bad hmac")
            self._json(403, {"error": "bad hmac"})
            return
        to = msg["to"]
        with _lock:
            if msg["id"] in _state["seen"]:
                self._json(200, {"ok": True, "id": msg["id"], "duplicate": True})
                return
        # Write the message FIRST, then remember it. Doing it the other way round
        # means a failed write (disk full, permissions) leaves the id in the dedup
        # index and every retry is answered "duplicate" — the message is lost.
        fname = f"msg-{msg['id']}.json"
        f = _agent_dir(to) / fname
        try:
            _agent_dir(to).mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(msg))
            tmp.replace(f)
        except OSError as e:
            sys.stderr.write(f"[a2a-relay] ERROR could not store {msg['id']}: {e}\n")
            self._json(500, {"error": "could not store message, retry"})
            return
        with _lock:
            _remember_id(msg["id"])
            _state["agents"].setdefault(to, {"registered_at": time.time()})
            # remember sender/recipient for /resolve after ack
            _state["sent"][msg["id"]] = {"from": msg["from"], "to": to, "ts": time.time()}
            if len(_state["sent"]) > DEDUP_KEEP:
                for k in sorted(_state["sent"], key=lambda x: _state["sent"][x]["ts"])[:1000]:
                    _state["sent"].pop(k, None)
            _save_state()
        with _new:
            _new.notify_all()
        self._json(202, {"stored": msg["id"], "to": to})

    def _inbox(self, agent: str, wait: int) -> None:
        if not agent:
            self._json(400, {"error": "agent query param required"})
            return
        if not AGENT_RE.match(agent):
            self._json(400, {"error": "agent id must match ^[a-z0-9][a-z0-9_-]{2,63}$"})
            return
        d = _agent_dir(agent)
        if not d.exists():
            # long-poll even if no dir yet (agent may register mid-wait)
            _wait_for_new(d, wait)
            if not d.exists():
                self._json(200, {"messages": []})
                return
        self._inbox_return(agent, wait)

    def _inbox_return(self, agent: str, wait: int) -> None:
        d = _agent_dir(agent)
        files = _pending(d)
        if not files and wait > 0:
            # nothing yet — wait up to `wait` seconds for something to land
            _wait_for_new(d, wait)
            files = _pending(d)
        # filter by HMAC (relay re-verifies on delivery — defense in depth)
        out = []
        for p in files:
            try:
                m = json.loads(p.read_text())
                if _hmac.compare_digest(sign(SECRET, m).encode(), str(m.get("hmac", "")).lower().encode()):
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
        """Ack is idempotent: two clients may legitimately ack the same message.

        Checking exists() then unlink() is a race — the other thread can remove the
        file in between and unlink() raises FileNotFoundError, which used to surface
        as a 500. unlink(missing_ok=True) closes that window.
        """
        f = _agent_dir(agent) / f"msg-{mid}.json"
        try:
            f.unlink()
        except FileNotFoundError:
            self._json(404, {"error": "not found or already acked"})
            return
        except OSError:
            self._json(404, {"error": "not found or already acked"})
            return
        self._json(200, {"acked": mid})

    def _dead_letter(self, msg, reason: str) -> None:
        d = DATA / "dead-letter"
        mid = uuid.uuid4().hex
        if isinstance(msg, dict):
            cand = msg.get("id")
            if isinstance(cand, str) and UUID_RE.match(cand):
                mid = cand
        p = d / f"dl-{mid}.json"
        try:
            p.write_text(json.dumps(msg, default=str))
        except Exception:
            p.write_text(json.dumps({"unserialisable": type(msg).__name__}))
        (d / f"dl-{mid}.reason.txt").write_text(reason)


# ----------------------------------------------------------------- helpers
def _clamp_wait(raw: str) -> int:
    """`wait` query param in seconds, clamped to [0, LONG_POLL_S]."""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return LONG_POLL_S
    return max(0, min(v, LONG_POLL_S))


def _pending(d: Path) -> list:
    """Pending message files, oldest arrival first (file mtime, then name)."""
    if not d.exists():
        return []
    files = []
    for p in d.glob("msg-*.json"):
        try:
            files.append((p.stat().st_mtime_ns, p.name, p))
        except OSError:
            continue
    files.sort()
    return [p for _, _, p in files]


def _wait_for_new(d: Path, wait: int) -> None:
    if wait <= 0:
        return
    deadline = time.time() + wait
    with _new:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            if _pending(d):
                return
            _new.wait(timeout=remaining)


class RelayServer(ThreadingHTTPServer):
    # the stdlib default backlog is 5: a burst of parallel clients overflows the
    # SYN queue and connections just time out. 128 is the usual Linux default.
    request_queue_size = 128
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    if not SECRET:
        print("[a2a-relay] FATAL: A2A_SECRET env var is empty — refusing to start unauthenticated", file=sys.stderr)
        sys.exit(1)
    _load_state()
    with _lock:
        _save_state(force=True)
    srv = RelayServer((HOST, PORT), H)
    print(f"[a2a-relay] listening on {HOST}:{PORT}, data={DATA}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with _lock:
            _save_state(force=True)
        srv.server_close()


if __name__ == "__main__":
    main()
