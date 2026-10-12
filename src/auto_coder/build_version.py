"""Build-only version lookup used by setuptools dynamic metadata."""

from __future__ import annotations

import os
from pathlib import Path

from .build_identity import BuildIdentityError, deserialize_build_identity, resolve_build_identity

IDENTITY_FILE = ".auto-coder-build-identity.json"
IDENTITY_ENV = "AUTO_CODER_BUILD_IDENTITY_FILE"


def get_build_version() -> str:
    """Return a validated carried identity or this checkout's identity."""

    root = Path(__file__).resolve().parents[2]
    supplied = os.environ.get(IDENTITY_ENV)
    carried = Path(supplied) if supplied else root / IDENTITY_FILE
    if carried.is_file():
        record = deserialize_build_identity(carried.read_text(encoding="utf-8"))
        observed = resolve_build_identity(root)
        if observed.state != "unknown" and observed != record:
            raise BuildIdentityError("Supplied build identity disagrees with the source checkout")
        return record.version
    return resolve_build_identity(root).version
