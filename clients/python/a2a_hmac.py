"""Shared HMAC/canonical helpers for a2a-bridge v1.1. Stdlib only."""
import hashlib
import hmac
import json


def canonical(m: dict) -> bytes:
    """Canonical bytes covered by the HMAC.

    `reply_to` is part of the signed payload: it drives result routing, so an
    unsigned reply_to would let a tampered copy of a message redirect the answer.
    """
    return (
        f"{m['id']}{m['from']}{m['to']}{m['type']}{m['timestamp']}"
        f"{json.dumps(m.get('content', ''), sort_keys=True, separators=(',', ':'))}"
        f"{json.dumps(m.get('reply_to'), sort_keys=True, separators=(',', ':'))}"
    ).encode()


def sign(secret: bytes, m: dict) -> str:
    return hmac.new(secret, canonical(m), hashlib.sha256).hexdigest()
