# a2a-bridge Message Format — v1.1

One JSON object = one message. Transport: HTTP (relay) or any tunnel.
No envelopes, no framing, no negotiation. If it doesn't fit in 50 lines you're doing it wrong.

## Fields

| Field      | Type              | Required | Notes                                    |
|------------|-------------------|----------|------------------------------------------|
| `id`       | string (uuid)     | yes      | Idempotency key. Relay dedupes on this.  |
| `from`     | string            | yes      | Agent id: `^[a-z0-9][a-z0-9_-]{2,63}$`   |
| `to`       | string            | yes      | Agent id the message is addressed to.    |
| `type`     | enum              | yes      | `message` \| `task` \| `result` \| `error` |
| `content`  | string \| object  | yes      | Free-form payload.                       |
| `reply_to` | string \| null    | no       | `id` of the message being answered. Required for `result`/`error`. |
| `timestamp`| string (ISO-8601) | yes      | Sender time, with offset. Parseable by `datetime.fromisoformat`. |
| `hmac`     | string            | yes      | HMAC-SHA256 hex (64 chars), see below.   |

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
  "hmac": "5d41402abc4b2a76b9719d911017c592…"
}
```

## HMAC

```
hmac      = HMAC_SHA256(secret, canonical)
canonical = id + from + to + type + timestamp
          + json(content,  sort_keys=True, separators=(",",":"))
          + json(reply_to, sort_keys=True, separators=(",",":"))
```

- `secret` is the shared relay secret (`A2A_SECRET`).
- Hex-encoded, lowercase.
- **`reply_to` is signed.** It decides where a `result` is routed, so leaving it
  outside the signature would let anyone redirect a reply by editing one field.
  (v1.0 omitted it — v1.1 fixes that. A v1.0 client is rejected by a v1.1 relay
  and vice versa; upgrade both sides together.)
- Relay verifies on `POST /send`. Inbound agents verify on `GET /inbox`.

## Semantics

- **Delivery:** at-least-once. The relay stores until the recipient acks it
  (`DELETE /inbox/:id`). Recipients must treat `id` as unique and skip
  already-processed ids (idempotency). Re-acking an already-acked message is a
  no-op returning `404`, not an error state.
- **Dead-letter:** messages that fail validation or HMAC verification are written
  to `dead-letter/` with a `.reason.txt` sidecar. The relay does not retry them;
  an operator inspects and replays if needed.
- **Ordering:** per-`from`, oldest-arrival-first (file mtime). No cross-agent
  ordering claim.
- **Size:** max 256 KiB per message (`A2A_MAX_BYTES`, configurable). Larger bodies
  are rejected with `413` before parsing.
- **Type rules:**
  - `message` — fire-and-forget chat.
  - `task` — request; sender expects a `result` with `reply_to` set.
  - `result` — MUST carry `reply_to` pointing at the task's `id`.
  - `error` — MUST carry `reply_to`; used when a task fails instead of `result`.

## Validation

A message is rejected (`400`) when any of these fail — the relay never touches the
filesystem with attacker-controlled strings:

- `id` is not a canonical UUID (`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
- `from`/`to` don't match the agent id pattern
- `type` is not one of the four names
- `result`/`error` without a UUID `reply_to`, or any non-UUID `reply_to`
- `from == to`
- `timestamp` is not ISO-8601
- `hmac` is not 64 hex chars
- the serialised message exceeds `A2A_MAX_BYTES`

## Versioning

`v1.1` is pinned by this file. Breaking changes bump the major number and require
a new relay. Clients MAY include `"format": "v1.1"`; relays default to v1.1.
