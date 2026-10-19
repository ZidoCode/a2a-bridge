#!/usr/bin/env python3
"""a2a-bridge test suite — stdlib only. Run: python3 test_a2a.py

Spawns a relay subprocess on an ephemeral port, exercises every endpoint,
asserts delivery guarantees / HMAC / dedup / dead-letter. Exits 0 on pass."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "clients" / "python"))
from a2a_hmac import sign

RELAY = str(Path(__file__).resolve().parent.parent / "relay" / "a2a_relay.py")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def req(method: str, url: str, body=None, secret="") -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, headers={"X-A2A-Secret": secret}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def mk(secret: str, frm: str, to: str, mtype: str, content, reply_to=None, mid=None) -> dict:
    m = {"id": mid or str(uuid.uuid4()), "from": frm, "to": to, "type": mtype,
         "content": content, "reply_to": reply_to,
         "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")}
    m["hmac"] = sign(secret.encode(), m)
    return m


def main() -> int:
    port = free_port()
    secret = "unit-test-secret"
    data = Path(tempfile.mkdtemp(prefix="a2a-test-"))
    env = {**os.environ, "A2A_SECRET": secret, "A2A_DATA": str(data), "A2A_PORT": str(port),
           "A2A_HOST": "127.0.0.1", "A2A_LONGPOLL_S": "2"}
    proc = subprocess.Popen([sys.executable, RELAY], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    passed = 0
    try:
        # wait for boot
        for _ in range(50):
            try:
                c, _ = req("GET", base + "/health", secret=secret)
                if c == 200:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        else:
            print("FATAL: relay did not start"); return 1

        def ok(name, cond):
            nonlocal passed
            assert cond, f"FAILED: {name}"
            passed += 1
            print(f"  ok  {name}")

        # 1. auth
        c, _ = req("GET", base + "/health")
        ok("no secret -> 403", c == 403)
        c, _ = req("GET", base + "/health", secret="wrong")
        ok("wrong secret -> 403", c == 403)
        c, h = req("GET", base + "/health", secret=secret)
        ok("good secret -> 200", c == 200 and h.get("ok") is True)

        # 2. register
        for a in ("hermes", "zido"):
            c, _ = req("POST", base + "/register", {"agent": a}, secret)
            ok(f"register {a}", c == 200)
        c, _ = req("POST", base + "/register", {"agent": "X"}, secret)
        ok("bad agent id rejected", c == 400)

        # 3. message delivery
        m = mk(secret, "hermes", "zido", "message", "hi")
        c, r = req("POST", base + "/send", {"message": m}, secret)
        ok("send -> 202", c == 202 and r.get("stored") == m["id"])
        c, r = req("GET", f"{base}/inbox?agent=zido", secret=secret)
        ok("zido receives 1", c == 200 and len(r["messages"]) == 1)
        c, _ = req("DELETE", f"{base}/inbox/{m['id']}?agent=zido", secret=secret)
        ok("ack -> 200", c == 200)
        c, r = req("GET", f"{base}/inbox?agent=zido", secret=secret)
        ok("inbox empty after ack", len(r["messages"]) == 0)

        # 4. task + result routing via resolve
        t = mk(secret, "hermes", "zido", "task", {"exec": "ls"})
        req("POST", base + "/send", {"message": t}, secret)
        c, r = req("GET", f"{base}/inbox?agent=zido", secret=secret)
        zido_sees_task = any(x["id"] == t["id"] for x in r["messages"])
        ok("zido sees task", zido_sees_task)
        req("DELETE", f"{base}/inbox/{t['id']}?agent=zido", secret=secret)  # ack
        c, r = req("GET", f"{base}/resolve/{t['id']}", secret=secret)
        ok("resolve finds sender post-ack", c == 200 and r.get("sender") == "hermes")
        res = mk(secret, "zido", "hermes", "result", "done", reply_to=t["id"])
        c, r = req("POST", base + "/send", {"message": res}, secret)
        ok("result accepted", c == 202)
        c, r = req("GET", f"{base}/inbox?agent=hermes", secret=secret)
        ok("hermes receives result", any(x["id"] == res["id"] for x in r["messages"]))

        # 5. result without reply_to -> rejected
        bad = mk(secret, "zido", "hermes", "result", "nope")
        bad["reply_to"] = None
        bad["hmac"] = sign(secret.encode(), bad)
        c, r = req("POST", base + "/send", {"message": bad}, secret)
        ok("result w/o reply_to -> 400", c == 400)

        # 6. tampered HMAC -> 403 + dead-letter
        tam = mk("WRONG", "zido", "hermes", "message", "tampered")
        c, r = req("POST", base + "/send", {"message": tam}, secret)
        ok("bad hmac -> 403", c == 403)
        ok("dead-letter file written", (data / "dead-letter").glob("dl-*.reason.txt") is not None
             and any((data / "dead-letter").glob("dl-*.reason.txt")))

        # 7. dedup
        d = mk(secret, "hermes", "zido", "message", "dup")
        c1, _ = req("POST", base + "/send", {"message": d}, secret)
        c2, r2 = req("POST", base + "/send", {"message": d}, secret)
        ok("dup first -> 202", c1 == 202)
        ok("dup second -> duplicate flag", c2 == 200 and r2.get("duplicate") is True)
        c, r = req("GET", f"{base}/inbox?agent=zido", secret=secret)
        ok("inbox has exactly 1 dup", sum(1 for x in r["messages"] if x["id"] == d["id"]) == 1)

        # 8. from==to rejected
        s = mk(secret, "hermes", "hermes", "message", "self")
        c, _ = req("POST", base + "/send", {"message": s}, secret)
        ok("self-send rejected", c == 400)

        # 9. long-poll wakes fast when a message lands mid-wait.
        # First drain any backlog (step 4 left a result for hermes) so the
        # poller actually blocks.
        c, r = req("GET", f"{base}/inbox?agent=hermes", secret=secret)
        for x in r.get("messages", []):
            req("DELETE", f"{base}/inbox/{x['id']}?agent=hermes", secret=secret)
        import threading
        got = {}
        def _poll():
            c, r = req("GET", f"{base}/inbox?agent=hermes", secret=secret)
            got["c"], got["r"] = c, r
        th = threading.Thread(target=_poll)
        t0 = time.time()
        th.start()
        time.sleep(0.5)  # let the poller block
        wake = mk(secret, "zido", "hermes", "message", "wakeup")
        req("POST", base + "/send", {"message": wake}, secret)
        # wait for the thread to finish AND populate got (small grace for write ordering)
        deadline = time.time() + 10
        while th.is_alive() and time.time() < deadline:
            time.sleep(0.05)
        elapsed = time.time() - t0
        ok("long-poll woke fast", elapsed < 2 and not th.is_alive())
        msgs = (got.get("r") or {}).get("messages", [])
        ok("long-poll got the message", any(x.get("id") == wake["id"] for x in msgs))
        req("DELETE", f"{base}/inbox/{wake['id']}?agent=hermes", secret=secret)

        print(f"\n{passed} checks passed")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
