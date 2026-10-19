#!/usr/bin/env python3
"""
a2a-bridge dogfood example: two agents on "different machines" (here: two
processes) solve a real task together over the bridge.

  - hermes (orchestrator) sends a TASK to zido: "compute something"
  - zido (worker) long-polls, runs the task locally, replies RESULT
  - hermes receives the result and reports the combined answer

Run:
    python3 examples/two_agents.py                     # starts its own relay
    A2A_RELAY=http://host:8765 python3 examples/two_agents.py   # use an existing relay

Stdlib only. Exits non-zero if either hop times out, so CI catches a broken bridge.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "clients" / "python"))
import a2a_client as C  # noqa: E402

TIMEOUT_S = 20


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_relay(port: int, data: str) -> subprocess.Popen:
    env = {**os.environ, "A2A_SECRET": "example-secret", "A2A_DATA": data,
           "A2A_PORT": str(port), "A2A_HOST": "127.0.0.1"}
    return subprocess.Popen([sys.executable, str(ROOT / "relay" / "a2a_relay.py")],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for_relay(relay: str, secret: str, proc=None) -> None:
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            raise SystemExit("[setup] relay exited before becoming ready")
        try:
            C._req("GET", f"{relay}/health", secret=secret, timeout=2)
            return
        except Exception:
            time.sleep(0.15)
    raise SystemExit(f"[setup] relay at {relay} did not become ready in 15s")


def wait_for_message(mtype: str, agent: str, wait: int = 5):
    """Poll until a message of `mtype` arrives, or the deadline passes."""
    os.environ["A2A_AGENT"] = agent
    deadline = time.time() + TIMEOUT_S
    while time.time() < deadline:
        for m in C.poll(wait):
            if m.get("type") == mtype:
                return m
            C.ack(m["id"])  # not ours: drop it so it does not wake the next poll
    return None


def main() -> int:
    external = os.environ.get("A2A_RELAY", "").strip()
    proc = None
    if external:
        relay = external.rstrip("/")
        os.environ.setdefault("A2A_SECRET", "example-secret")
        print(f"[setup] using external relay {relay}")
    else:
        port = free_port()
        data = tempfile.mkdtemp(prefix="a2a-example-")
        relay = f"http://127.0.0.1:{port}"
        os.environ["A2A_RELAY"] = relay
        os.environ["A2A_SECRET"] = "example-secret"
        proc = start_relay(port, data)
        print(f"[setup] started local relay at {relay}")

    secret = os.environ["A2A_SECRET"]
    try:
        wait_for_relay(relay, secret, proc)

        for a in ("hermes", "zido"):
            C._req("POST", f"{relay}/register", {"agent": a}, secret)

        # --- hermes: send a task ---
        os.environ["A2A_AGENT"] = "hermes"
        task_payload = {"op": "fib", "n": 20}
        sent = C.send("zido", task_payload, "task")
        if "stored" not in sent:
            print(f"[hermes] send failed: {sent}")
            return 1
        print(f"[hermes] sent task {sent['stored']} to zido")

        # --- zido: poll, do the work, reply ---
        task_msg = wait_for_message("task", "zido")
        if not task_msg:
            print("[zido] no task received (timeout)")
            return 1
        print(f"[zido] received task {task_msg['id']}: {task_msg['content']}")

        def fib(n):
            a, b = 0, 1
            for _ in range(n):
                a, b = b, a + b
            return a

        n = task_msg["content"].get("n", 10) if isinstance(task_msg["content"], dict) else 10
        answer = fib(int(n))
        print(f"[zido] computed fib({n}) = {answer}")
        C.ack(task_msg["id"])
        res = C.send("", f"fib({n})={answer}", "result", reply_to=task_msg["id"])
        if "stored" not in res:
            print(f"[zido] result send failed: {res}")
            return 1
        print(f"[zido] sent result {res['stored']} back to hermes")

        # --- hermes: poll for the result ---
        got = wait_for_message("result", "hermes")
        if not got:
            print("[hermes] no result received (timeout)")
            return 1
        print(f"[hermes] received result: {got['content']}  (reply_to {got['reply_to'][:8]}…)")
        C.ack(got["id"])

        print("\n✔ two agents exchanged task + result over the bridge")
        return 0
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
