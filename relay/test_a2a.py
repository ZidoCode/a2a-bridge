#!/usr/bin/env python3
"""a2a-bridge test suite — stdlib only. Run: python3 relay/test_a2a.py

Spawns a relay subprocess on an ephemeral port, exercises every endpoint,
asserts delivery guarantees / HMAC / dedup / dead-letter, and covers the
adversarial cases (path traversal, type confusion, replay, poisoning).

Exits 0 on pass, 1 on the first failed assertion.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "clients" / "python"))
from a2a_hmac import sign  # noqa: E402

RELAY = str(ROOT / "relay" / "a2a_relay.py")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def req(method: str, url: str, body=None, secret="", timeout=20) -> tuple:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, headers={"X-A2A-Secret": secret}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}
    except Exception as e:  # connection level failure is a test failure, not a crash
        return "EXC", {"error": f"{type(e).__name__}: {e}"}


def mk(secret: str, frm: str, to: str, mtype: str, content, reply_to=None, mid=None, ts=None) -> dict:
    m = {"id": mid or str(uuid.uuid4()), "from": frm, "to": to, "type": mtype,
         "content": content, "reply_to": reply_to,
         "timestamp": ts or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")}
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
        for _ in range(80):
            c, _ = req("GET", base + "/health", secret=secret, timeout=2)
            if c == 200:
                break
            time.sleep(0.1)
        else:
            out = proc.stdout.read().decode() if proc.stdout else ""
            print("FATAL: relay did not start\n" + out[:2000])
            return 1

        def ok(name, cond, detail=""):
            nonlocal passed
            assert cond, f"FAILED: {name} {detail}"
            passed += 1
            print(f"  ok  {name}")

        # ---------------------------------------------------------- auth
        ok("no secret -> 403", req("GET", base + "/health")[0] == 403)
        ok("wrong secret -> 403", req("GET", base + "/health", secret="wrong")[0] == 403)
        c, h = req("GET", base + "/health", secret=secret)
        ok("good secret -> 200", c == 200 and h.get("ok") is True)

        # ------------------------------------------------------ register
        for a in ("hermes", "zido"):
            ok(f"register {a}", req("POST", base + "/register", {"agent": a}, secret)[0] == 200)
        ok("bad agent id rejected", req("POST", base + "/register", {"agent": "X"}, secret)[0] == 400)
        # regression: non-string agent must be a 400, not a dropped connection
        for bad in (123, ["a"], {"x": 1}, None, True):
            c, _ = req("POST", base + "/register", {"agent": bad}, secret)
            ok(f"register agent={type(bad).__name__} -> 400", c == 400, f"got {c}")
        ok("register non-object body -> 400", req("POST", base + "/register", [1, 2], secret)[0] == 400)

        # ------------------------------------------------------ delivery
        m = mk(secret, "hermes", "zido", "message", "hi")
        c, r = req("POST", base + "/send", {"message": m}, secret)
        ok("send -> 202", c == 202 and r.get("stored") == m["id"])
        c, r = req("GET", f"{base}/inbox?agent=zido", secret=secret)
        ok("zido receives 1", c == 200 and len(r["messages"]) == 1)
        ok("ack -> 200", req("DELETE", f"{base}/inbox/{m['id']}?agent=zido", secret=secret)[0] == 200)
        ok("inbox empty after ack",
           len(req("GET", f"{base}/inbox?agent=zido", secret=secret)[1]["messages"]) == 0)

        # ------------------------------------------- task + resolve
        t = mk(secret, "hermes", "zido", "task", {"exec": "ls"})
        req("POST", base + "/send", {"message": t}, secret)
        r = req("GET", f"{base}/inbox?agent=zido", secret=secret)[1]
        ok("zido sees task", any(x["id"] == t["id"] for x in r["messages"]))
        req("DELETE", f"{base}/inbox/{t['id']}?agent=zido", secret=secret)
        c, r = req("GET", f"{base}/resolve/{t['id']}", secret=secret)
        ok("resolve finds sender post-ack", c == 200 and r.get("sender") == "hermes")
        res = mk(secret, "zido", "hermes", "result", "done", reply_to=t["id"])
        ok("result accepted", req("POST", base + "/send", {"message": res}, secret)[0] == 202)
        r = req("GET", f"{base}/inbox?agent=hermes", secret=secret)[1]
        ok("hermes receives result", any(x["id"] == res["id"] for x in r["messages"]))

        # ------------------------------------- validation rejects
        bad = mk(secret, "zido", "hermes", "result", "nope")
        bad["reply_to"] = None
        bad["hmac"] = sign(secret.encode(), bad)
        ok("result w/o reply_to -> 400", req("POST", base + "/send", {"message": bad}, secret)[0] == 400)

        # regression: reply_to is covered by the HMAC
        signed = mk(secret, "zido", "hermes", "result", "ok", reply_to=t["id"])
        tampered = dict(signed)
        tampered["reply_to"] = str(uuid.uuid4())  # redirect the answer, keep old hmac
        c, _ = req("POST", base + "/send", {"message": tampered}, secret)
        ok("tampered reply_to -> 403", c == 403, f"got {c}")

        # regression: type confusion in from/to -> 400, not a dropped connection
        for field, val in (("from", 123), ("to", None), ("from", ["a"])):
            mm = mk(secret, "hermes", "zido", "message", "x")
            mm[field] = val
            mm["hmac"] = sign(secret.encode(), mm)
            c, _ = req("POST", base + "/send", {"message": mm}, secret)
            ok(f"send {field}={type(val).__name__} -> 400", c == 400, f"got {c}")

        # regression: non-uuid id must not reach the filesystem
        for evil in ("../" * 8 + "etc", "a" * 36, "../../../../tmp/a2a-escape"):
            mm = mk(secret, "hermes", "zido", "message", "x", mid=evil)
            c, _ = req("POST", base + "/send", {"message": mm}, secret)
            ok(f"non-uuid id ({evil[:12]}…) -> 400", c == 400, f"got {c}")

        # regression: bad timestamp
        mm = mk(secret, "hermes", "zido", "message", "x", ts="yesterday")
        c, _ = req("POST", base + "/send", {"message": mm}, secret)
        ok("bad timestamp -> 400", c == 400, f"got {c}")

        # ------------------------------------------------------ hmac
        tam = mk("WRONG", "zido", "hermes", "message", "tampered")
        ok("bad hmac -> 403", req("POST", base + "/send", {"message": tam}, secret)[0] == 403)
        ok("dead-letter written", any((data / "dead-letter").glob("dl-*.reason.txt")))

        # ------------------------------------------------------ dedup
        d = mk(secret, "hermes", "zido", "message", "dup")
        ok("dup first -> 202", req("POST", base + "/send", {"message": d}, secret)[0] == 202)
        c, r2 = req("POST", base + "/send", {"message": d}, secret)
        ok("dup second -> duplicate flag", c == 200 and r2.get("duplicate") is True)
        r = req("GET", f"{base}/inbox?agent=zido", secret=secret)[1]
        ok("inbox has exactly 1 dup", sum(1 for x in r["messages"] if x["id"] == d["id"]) == 1)

        ok("self-send rejected",
           req("POST", base + "/send", {"message": mk(secret, "hermes", "hermes", "message", "s")},
               secret)[0] == 400)

        # ------------------------------------------- security: traversal
        # regression: /inbox?agent= must not read outside A2A_DATA/outbox
        leak_dir = data.parent / f"LEAK-{uuid.uuid4().hex}" / "outbox" / "evil"
        leak_dir.mkdir(parents=True)
        leak_id = str(uuid.uuid4())
        leak_msg = mk(secret, "hermes", "zido", "message", "TOP-SECRET")
        leak_msg["id"] = leak_id
        leak_msg["hmac"] = sign(secret.encode(), leak_msg)
        (leak_dir / f"msg-{leak_id}.json").write_text(json.dumps(leak_msg))
        rel = "../../" + leak_dir.parent.parent.name + "/outbox/evil"
        c, r = req("GET", base + "/inbox?agent=" + urllib.parse.quote(rel, safe=""), secret=secret)
        ok("inbox path traversal blocked", c == 400, f"got {c} {r}")
        shutil.rmtree(leak_dir.parent.parent, ignore_errors=True)

        # regression: DELETE with a traversing agent id
        c, _ = req("DELETE", base + f"/inbox/{leak_id}?agent=../../etc", secret=secret)
        ok("delete path traversal blocked", c == 400, f"got {c}")

        # ------------------------------------------- security: poison file
        # an undecodable file in the outbox must not wedge delivery
        zd = data / "outbox" / "zido"
        poison = zd / f"msg-{uuid.uuid4()}.json"
        poison.write_bytes(b"\xff\xfe not json at all")
        alive = mk(secret, "hermes", "zido", "message", "delivered-despite-poison")
        req("POST", base + "/send", {"message": alive}, secret)
        r = req("GET", f"{base}/inbox?agent=zido", secret=secret)[1]
        ok("delivery works with a poison file present",
           any(x["id"] == alive["id"] for x in r["messages"]))
        poison.unlink()

        # ------------------------------------------------- long-poll
        r = req("GET", f"{base}/inbox?agent=hermes", secret=secret)[1]
        for x in r.get("messages", []):
            req("DELETE", f"{base}/inbox/{x['id']}?agent=hermes", secret=secret)

        got = {}

        def _poll():
            got["c"], got["r"] = req("GET", f"{base}/inbox?agent=hermes", secret=secret)

        th = threading.Thread(target=_poll)
        t0 = time.time()
        th.start()
        time.sleep(0.5)
        wake = mk(secret, "zido", "hermes", "message", "wakeup")
        req("POST", base + "/send", {"message": wake}, secret)
        deadline = time.time() + 10
        while th.is_alive() and time.time() < deadline:
            time.sleep(0.05)
        elapsed = time.time() - t0
        ok("long-poll woke fast", elapsed < 2 and not th.is_alive(), f"{elapsed:.2f}s")
        msgs = (got.get("r") or {}).get("messages", [])
        ok("long-poll got the message", any(x.get("id") == wake["id"] for x in msgs))
        req("DELETE", f"{base}/inbox/{wake['id']}?agent=hermes", secret=secret)

        # regression: ?wait= is honoured instead of always blocking LONG_POLL_S
        t0 = time.time()
        c, _ = req("GET", f"{base}/inbox?agent=nobody&wait=1", secret=secret)
        el = time.time() - t0
        ok("wait=0 returns immediately",
           req("GET", f"{base}/inbox?agent=nobody2&wait=0", secret=secret)[0] == 200)
        ok("wait=1 honoured", c == 200 and el < 1.8, f"{el:.2f}s")
        t0 = time.time()
        req("GET", f"{base}/inbox?agent=nobody3&wait=99", secret=secret)
        ok("wait is clamped to LONG_POLL_S", time.time() - t0 < 4.0)

        # regression: oversized body -> 413, connection stays usable
        big = {"id": str(uuid.uuid4()), "from": "hermes", "to": "zido", "type": "message",
               "content": "x" * (400 * 1024), "reply_to": None,
               "timestamp": datetime.now(timezone.utc).isoformat()}
        c, _ = req("POST", base + "/send", {"message": big}, secret)
        ok("oversized body -> 413", c == 413, f"got {c}")
        ok("relay alive after oversized body", req("GET", base + "/health", secret=secret)[0] == 200)

        # regression: malformed request lines never kill the relay
        for raw in (b"POST /send HTTP/1.1\r\nHost: x\r\nContent-Length: nope\r\n\r\n",
                    b"POST /send HTTP/1.1\r\nHost: x\r\nContent-Length: -5\r\n\r\n"):
            try:
                s = socket.create_connection(("127.0.0.1", port), timeout=5)
                s.sendall(b"X-A2A-Secret: " + secret.encode() + b"\r\n" + raw)
                s.recv(200)
                s.close()
            except Exception:
                pass
        ok("relay alive after malformed requests",
           req("GET", base + "/health", secret=secret)[0] == 200)

        # ------------------------------------------- state durability
        # state.json must exist and hold the agents we registered
        st = json.loads((data / "state.json").read_text())
        ok("state.json persisted agents", "zido" in st.get("agents", {}))

        # ------------------------------------------- log backpressure
        # Regression: with access logging on and stderr a pipe nobody drains, the
        # 64 KiB pipe buffer fills and every handler blocks on write — the server
        # wedges. The fix is that logging is OFF by default, so this spawns a relay
        # with the DEFAULT config and stderr wired to a PIPE that is never read:
        # it must keep answering. (A build that logs every request unconditionally
        # stalls at ~1280 requests, which is what this asserts against.)
        port2 = free_port()
        data2 = Path(tempfile.mkdtemp(prefix="a2a-logtest-"))
        env2 = {**os.environ, "A2A_SECRET": secret, "A2A_DATA": str(data2),
                "A2A_PORT": str(port2), "A2A_HOST": "127.0.0.1",
                "A2A_LONGPOLL_S": "1"}
        env2.pop("A2A_ACCESS_LOG", None)
        proc2 = subprocess.Popen([sys.executable, RELAY], env=env2,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        base2 = f"http://127.0.0.1:{port2}"
        try:
            for _ in range(80):
                if req("GET", base2 + "/health", secret=secret, timeout=2)[0] == 200:
                    break
                time.sleep(0.1)
            slow = None
            for i in range(2500):
                t0 = time.time()
                c, _ = req("GET", base2 + "/health", secret=secret, timeout=5)
                if c != 200 or (time.time() - t0) > 3:
                    slow = (i, c, round(time.time() - t0, 1))
                    break
        finally:
            proc2.terminate()
            try:
                proc2.wait(timeout=5)
            except Exception:
                proc2.kill()
            shutil.rmtree(data2, ignore_errors=True)
        ok("relay does not block on an unread log pipe", slow is None,
           f"stalled at request {slow}")

        print(f"\n{passed} checks passed")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        shutil.rmtree(data, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
