"""LiveKit erişim jetonu (JWT, HS256). Ek bağımlılık gerektirmez."""
import base64
import hashlib
import hmac
import json
import time


def _b64(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def make_token(api_key: str, api_secret: str, identity: str, room: str,
               ttl_seconds: int = 900, name: str = "", now: float | None = None) -> str:
    now = int(now if now is not None else time.time())
    claims = {
        "iss": api_key,
        "sub": identity,
        "nbf": now,
        "exp": now + ttl_seconds,
        "name": name or identity,
        "video": {
            "room": room,
            "roomJoin": True,
            "canPublish": True,
            "canSubscribe": True,
            "canPublishData": True,
        },
    }
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = _b64(json.dumps(header, separators=(",", ":")).encode()) + b"." + \
        _b64(json.dumps(claims, separators=(",", ":")).encode())
    signature = hmac.new(api_secret.encode(), signing_input, hashlib.sha256).digest()
    return (signing_input + b"." + _b64(signature)).decode()
