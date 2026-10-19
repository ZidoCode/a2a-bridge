"""Shared HMAC/canonical helpers for a2a-bridge v1. Stdlib only."""
import hashlib
import hmac
import json


def canonical(m: dict) -> bytes:
    return (
        f"{m['id']}{m['from']}{m['to']}{m['type']}{m['timestamp']}"
        f"{json.dumps(m.get('content', ''), sort_keys=True, separators=(',', ':'))}"
    ).encode()


def sign(secret: bytes, m: dict) -> str:
    return hmac.new(secret, canonical(m), hashlib.sha256).hexdigest()
