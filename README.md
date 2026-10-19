<p align="center">
  <img src="assets/bridge-animated.svg" alt="Two agents exchanging task and result messages through an a2a-bridge relay" width="760"/>
</p>

<p align="center">
  <strong>Google built A2A for enterprises. We built the bridge for everyone else.</strong>
</p>

<p align="center">
  <img alt="CI" src="https://github.com/ZidoCode/a2a-bridge/actions/workflows/ci.yml/badge.svg"/>
  <img alt="Python" src="https://img.shields.io/badge/python-3.9%2B-3776ab?logo=python&logoColor=white"/>
  <img alt="Dependencies" src="https://img.shields.io/badge/dependencies-stdlib%20only-lightgrey"/>
  <img alt="License" src="https://img.shields.io/badge/license-MIT-8b5cf6"/>
</p>

**`a2a-bridge`** is a lightweight, self-hostable message relay for independent AI agents on *different machines*.
No Agent Cards. No protocol negotiation. No shared runtime. **One secret, one port, five minutes.**

Two agents that can't share a filesystem or a process can still send messages, delegate tasks, and get results
back — over plain HTTP, with HMAC integrity and at-least-once delivery.

---

## Why this exists

Indie agent builders run real agents on separate boxes. Talking them to each other today means reinventing one of:

| Solution | Gap |
|----------|-----|
| **Google A2A** | Enterprise spec (Agent Cards, task lifecycle, SSE, push). A compliant agent takes *days*, not minutes. |
| **MCP** | Agent→tool, not agent→agent. No concept of two autonomous agents negotiating. |
| **Multi-agent frameworks** (CrewAI, AutoGen) | Assume a shared runtime. All agents in one process. |
| **DIY** (webhooks, Telegram groups, GitHub issues) | No standard format, no delivery guarantees, no auth story. |

`a2a-bridge` is the missing middle: batteries-included, 5-minute setup, transport over HTTP, secure by default.

---

## What's in the box

```
a2a-bridge/
├── relay/
│   ├── a2a_relay.py     # the server — ONE FILE, stdlib only
│   └── test_a2a.py      # 23-check suite (spawns its own relay)
├── clients/
│   ├── python/
│   │   ├── a2a_client.py  # send / task / result / poll / ack
│   │   └── a2a_hmac.py    # shared HMAC/canonical (stdlib)
│   └── shell/
│       └── a2a.sh         # curl + python3, zero pip installs
├── examples/
│   └── two_agents.py      # dogfood: two agents solve a task together
├── assets/
│   └── bridge-animated.svg
└── message-format.md      # the standard (v1, <50 lines)
```

No `pip install` for the relay. No `npm`. Python 3.9+ stdlib, bash + curl for the shell client.

---

## 5-minute setup

**1. Run the relay** (any box with Python 3.9+):

```bash
A2A_SECRET="$(openssl rand -hex 32)" A2A_PORT=8765 A2A_DATA=./a2a-data \
  python3 relay/a2a_relay.py
```

**2. Connect agent A** (machine 1):

```bash
export A2A_RELAY=http://RELAY-IP:8765 A2A_SECRET=... A2A_AGENT=hermes
python3 clients/python/a2a_client.py send zido "hello"
```

**3. Connect agent B** (machine 2):

```bash
export A2A_RELAY=http://RELAY-IP:8765 A2A_SECRET=... A2A_AGENT=zido
python3 clients/python/a2a_client.py poll   # long-polls, prints, acks
```

That's it. They're talking.

> The relay refuses to start without `A2A_SECRET` — there is no unauthenticated mode.

---

## API

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/register` | Register an agent id (idempotent) |
| `POST` | `/send` | Deliver a signed message to an agent |
| `GET`  | `/inbox?agent=X` | Long-poll for messages addressed to `X` |
| `DELETE` | `/inbox/:id?agent=X` | Ack a delivered message (removes it) |
| `GET`  | `/resolve/:id` | Who sent a message (route results back) |
| `GET`  | `/health` | Liveness + registered agents |

Every request carries `X-A2A-Secret: <secret>`. Every message carries an **HMAC-SHA256** over its canonical
fields. Bad HMAC → `403`, dead-lettered, never delivered.

**Delivery model:** at-least-once. The relay holds a message until the recipient acks it. Recipients treat
`id` as an idempotency key and skip what they've seen. Invalid or unauthenticated messages go to
`dead-letter/` with a reason file — never silently dropped, never delivered.

**Task semantics:** a `task` is a request; the responder answers with a `result` (or `error`) carrying
`reply_to`. The relay's `/resolve` lets the responder route back to the right agent even after the original
was acked.

---

## Security

- **No open relay.** Shared-secret auth on every endpoint; the relay won't boot without a secret.
- **Message integrity.** HMAC-SHA256 per message; verified on *receive* and re-verified on *delivery*
  (defense in depth).
- **No SSRF surface.** The relay only stores and serves JSON to authenticated agents — it never calls back to
  client-supplied URLs, so there's no webhook SSRF vector.
- **Bounded state.** Dedup index + send-index are capped (LRU) so a long-lived relay can't grow unbounded.

---

## What it is NOT

- Not a replacement for Google A2A (that's for enterprise compliance — Agent Cards, SSE, push).
- Not a multi-agent orchestration framework (agents stay independent; nothing here drives their loops).
- Not a shared-memory system.

It's the **"just let my two agents talk"** tool.

---

## Run the tests

```bash
python3 relay/test_a2a.py
# 23 checks passed
```

The suite spins up its own relay on an ephemeral port, so it never touches a running instance.

## Roadmap (post-MVP)

- TypeScript client (`npm i a2a-bridge`)
- WebSocket `/stream` for real-time (polling stays as the zero-dep default)
- Per-agent secrets (today: one relay-wide secret)
- Signed receipts / optional end-to-end keys beyond the relay secret
- Docker image + one-line deploy

---

<p align="center">
  <em>Part of the ZidoCode A2A family — built by auditing the impractical ones first
  (a2a-doctor, a2a-webhook-audit, a2a-stream-tax, and the rest).</em>
</p>
