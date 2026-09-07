"""Stable rendezvous paths for repository-scoped runtime coordination."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Optional


def runtime_root() -> Path:
    configured = os.environ.get("AUTO_CODER_RUNTIME_ROOT", "")
    root = Path(configured).expanduser() if configured else Path.home() / ".auto-coder" / "runtime"
    return root.resolve()


def _repository_parts(repository: str) -> tuple[str, str]:
    parts = repository.split("/", 1)
    owner, name = (parts[0], parts[1]) if len(parts) == 2 else ("local", parts[0])

    def safe(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", value)
        if not cleaned or cleaned in {".", ".."}:
            raise ValueError(f"Invalid repository lock namespace: {repository!r}")
        return cleaned

    return safe(owner), safe(name)


def lock_path(repository: str, state_path: Path, purpose: str, key: Optional[str] = None) -> Path:
    """Map a logical lock to a persistent, process-independent runtime path."""
    owner, name = _repository_parts(repository)
    store_id = hashlib.sha256(str(state_path.expanduser().resolve()).encode("utf-8")).hexdigest()
    key_id = "" if key is None else "-" + hashlib.sha256(key.encode("utf-8")).hexdigest()
    return runtime_root() / "locks" / owner / name / f"{purpose}-{store_id}{key_id}.lock"


def ensure_lock_directory(path: Path, shared_gid: Optional[int] = None) -> None:
    """Create a lock namespace, optionally making every level group-shared."""
    directory = path.parent
    if shared_gid is None:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(f"Cannot create or traverse runtime lock directory '{directory}': {exc}") from exc
        return
    base = runtime_root()
    current = base
    try:
        base.mkdir(parents=True, exist_ok=True)
        for part in directory.relative_to(base).parts:
            current /= part
            try:
                current.mkdir()
            except FileExistsError:
                continue
            os.chown(current, -1, shared_gid)
            os.chmod(current, 0o2770)
    except OSError as exc:
        raise RuntimeError(f"Cannot establish shared runtime lock directory '{current}': {exc}") from exc
