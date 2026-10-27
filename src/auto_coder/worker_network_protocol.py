"""Closed version-1 machine-auth protocol shared by the bridge and its client."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
from pathlib import Path

PROTOCOL_VERSION = "1"
MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_TARGET_LENGTH = 2048
MAX_CLOCK_SKEW = 60
MAX_NONCES = 2048
_TOKEN = re.compile(r"[a-f0-9]{32}\Z")


def valid_token(value: object) -> bool:
    return isinstance(value, str) and _TOKEN.fullmatch(value) is not None


def load_machine_secret(path: Path) -> bytes:
    """Read a dedicated operator-mounted base64 key, not an OAuth credential."""
    if not path.is_absolute() or not path.is_file():
        raise ValueError("machine credential path must be an existing absolute file")
    raw = path.read_bytes()
    if not 40 <= len(raw) <= 1024:
        raise ValueError("machine credential has an invalid size")
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("machine credential is not base64") from error
    if len(key) < 32 or len(key) > 128:
        raise ValueError("machine credential must contain 32-128 random bytes")
    return key


def request_mac(
    key: bytes,
    *,
    method: str,
    target: str,
    instance: str,
    generation: str,
    timestamp: str,
    nonce: str,
    request_id: str,
    body: bytes,
    worker_run: str = "",
    worker_generation: str = "",
) -> str:
    fields = (
        "auto-coder-supervisor-v1",
        method,
        target,
        instance,
        generation,
        timestamp,
        nonce,
        request_id,
        worker_run,
        worker_generation,
        hashlib.sha256(body).hexdigest(),
    )
    return hmac.new(key, "\n".join(fields).encode("utf-8"), hashlib.sha256).hexdigest()


def response_mac(key: bytes, *, request_id: str, generation: str, body: bytes) -> str:
    material = f"auto-coder-supervisor-response-v1\n{request_id}\n{generation}\n{hashlib.sha256(body).hexdigest()}"
    return hmac.new(key, material.encode("utf-8"), hashlib.sha256).hexdigest()
