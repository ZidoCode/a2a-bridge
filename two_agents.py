#!/usr/bin/env python3
"""
a2a-bridge dogfood example: two agents on "different machines" (here: two
processes) solve a real task together over the bridge.

  - hermes (orchestrator) sends a TASK to zido: "compute something"
  - zido (worker) long-polls, runs the task locally, replies RESULT
  - hermes receives the result and reports the combined answer

Run:
    python3 examples/two_agents.py            # starts its own relay
    A2A_RELAY=... python3 examples/two_agents.py   # or use an existing relay

Stdlib only.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "clients" / "python"))
import a2a_client as C  # noqa: E402


def free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def start_relay(port: int, data: str) -> subprocess.Popen:
    env = {**os.environ, "A2A_SECRET": "example-secret", "A2A_DATA": data,
           "A2A_PORT": str(port), "A2A_HOST": "127.0.0.1"}
    return subprocess.Popen([sys.executable, str(ROOT / "relay" / "a2a_relay.py")],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> None:
    external = os.environ.get("A2A_RELAY", "").strip()
    proc = None
    if external:
        relay = external.rstrip("/")
        os.environ["A2A_SECRET"] = os.environ.get("A2A_SECRET", "example-secret")
        print(f"[setup] using external relay {relay}")
    else:
        port = free_port()
        data = tempfile.mkdtemp(prefix="a2a-example-")
        relay = f"http://127.0.0.1:{port}"
        os.environ["A2A_RELAY"] = relay
        os.environ["A2A_SECRET"] = "example-secret"
        proc = start_relay(port, data)
        time.sleep(1.0)
        print(f"[setup] started local relay at {relay}")

    try:
        # register both
        C._req("POST", f"{relay}/register", {"agent": "hermes"}, os.environ["A2A_SECRET"])
        C._req("POST", f"{relay}/register", {"agent": "zido"}, os.environ["A2A_SECRET"])

        # --- hermes: send a task ---
        os.environ["A2A_AGENT"] = "hermes"
        task_payload = {"op": "fib", "n": 20}
        sent = C.send("zido", task_payload, "task")
        print(f"[hermes] sent task {sent.get('stored')} to zido")

        # --- zido: poll, do the work, reply ---
        os.environ["A2A_AGENT"] = "zido"
        deadline = time.time() + 20
        task_msg = None
        while time.time() < deadline:
            for m in C.poll(5):
                if m["type"] == "task" and task_msg is None:
                    task_msg = m
            if task_msg:
                break
        if not task_msg:
            print("[zido] no task received (timeout)"); return
        print(f"[zido] received task {task_msg['id']}: {task_msg['content']}")

        # actually do the work
        def fib(n): 
            a, b = 0, 1
            for _ in range(n): a, b = b, a + b
            return a
        n = task_msg["content"].get("n", 10)
        answer = fib(n)
        print(f"[zido] computed fib({n}) = {answer}")
        C.ack(task_msg["id"])
        res = C.send("", f"fib({n})={answer}", "result", reply_to=task_msg["id"])
        print(f"[zido] sent result {res.get('stored')} back to hermes")

        # --- hermes: poll for the result ---
        os.environ["A2A_AGENT"] = "hermes"
        deadline = time.time() + 20
        got = None
        while time.time() < deadline:
            for m in C.poll(5):
                if m["type"] == "result" and got is None:
                    got = m
            if got:
                break
        if not got:
            print("[hermes] no result received (timeout)"); return
        print(f"[hermes] received result: {got['content']}  (reply_to {got['reply_to'][:8]}…)")
        C.ack(got["id"])

        print("\n✔ two agents exchanged task + result over the bridge")
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()


if __name__ == "__main__":
    main()
