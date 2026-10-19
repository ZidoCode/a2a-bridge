# a2a-bridge Message Format — v1

One JSON object = one message. Transport: HTTP (relay) or any tunnel.
No envelopes, no framing, no negotiation. If it doesn't fit in 50 lines you're doing it wrong.

## Fields

| Field      | Type              | Required | Notes                                    |
|------------|-------------------|----------|------------------------------------------|
| `id`       | string (uuid-v4)  | yes      | Idempotency key. Relay dedupes on this.  |
| `from`     | string            | yes      | Agent id (creator-supplied, 3-64 chars). |
| `to`       | string            | yes      | Agent id the message is addressed to.    |
| `type`     | enum              | yes      | `message` \| `task` \| `result` \| `error` |
| `content`  | string \| object  | yes      | Free-form. `task`/`result` use `reply_to`. |
| `reply_to` | string \| null    | no       | `id` of the message being answered.      |
| `timestamp`| string (ISO-8601) | yes      | Sender time, with offset.                |
| `hmac`     | string            | yes      | HMAC-SHA256 hex, see below.              |

## Example

```json
{
  "id": "7c9e6679-9422-4046-ad62-7e753614afba",
  "from": "hermes",
  "to": "zido",
  "type": "task",
  "content": {"exec": "df -h /"},
  "reply_to": null,
  "timestamp": "2026-10-19T13:30:00+07:00",
  "hmac": "5d41402abc4b2a76b9719d911017c592"
}
```

## HMAC

```
hmac = HMAC_SHA256(secret, canonical)
canonical = id + from + to + type + timestamp + json(content, sort_keys, separators=(",",":"))
```

- `secret` is the shared relay secret (`A2A_SECRET`).
- Hex-encoded, lowercase.
- Relay verifies on `POST /send`. Inbound agents verify on `GET /inbox`.
- A message with a bad HMAC is rejected (403) and NOT stored.

## Semantics

- **Delivery:** at-least-once. The relay stores until the recipient acks it
  (`DELETE /inbox/:id`). Recipients must treat `id` as unique and skip
  already-processed ids (idempotency).
- **Dead-letter:** messages whose `to` agent has never registered a secret, or
  that fail validation for 24h, move to `dead-letter/` (operator-visible,
  never delivered).
- **Ordering:** per-`from`, FIFO by insertion. No cross-agent ordering claim.
- **Size:** max 256 KiB per message (relay limit, configurable).
- **Type rules:**
  - `message` — fire-and-forget chat.
  - `task` — request; sender expects a `result` with `reply_to` set.
  - `result` — MUST carry `reply_to` pointing at the task's `id`.
  - `error` — MUST carry `reply_to`; used when a task fails instead of `result`.

## Versioning

`v1` is pinned by this file. Breaking changes bump to `v2` and require a new
relay. Clients MAY include `"format": "v1"`; relays default to v1.
