"""Stable rendezvous paths for repository-scoped runtime coordination."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import threading
import time
import weakref
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional


class LockAcquisitionTimeout(TimeoutError):
    """A runtime lock could not be acquired within its bounded wait."""


@dataclass
class _FileLockState:
    lock: threading.RLock = field(default_factory=threading.RLock)
    depth: int = 0


_file_locks: weakref.WeakValueDictionary[tuple[int, str], _FileLockState] = weakref.WeakValueDictionary()
_file_locks_guard = threading.Lock()


@contextmanager
def file_lock(path: Path, *, opener: Optional[Callable[[], int]] = None, timeout: float = 30.0, reentrant: bool = True) -> Iterator[None]:
    """Serialize a canonical file across processes, instances, and threads.

    Reentry on the same thread shares the outer file descriptor. Separate
    descriptors for the same inode would otherwise deadlock even in one process.
    The PID in the registry key keeps forked children out of inherited reentry.
    """
    key = (os.getpid(), str(path.expanduser().resolve()))
    with _file_locks_guard:
        state = _file_locks.get(key)
        if state is None:
            state = _FileLockState()
            _file_locks[key] = state
    deadline = time.monotonic() + timeout
    if not state.lock.acquire(timeout=max(0.0, timeout)):
        raise LockAcquisitionTimeout(f"Timed out acquiring runtime lock '{path}'")
    try:
        if state.depth:
            if not reentrant:
                raise LockAcquisitionTimeout(f"Runtime lock '{path}' already has an active sender")
            state.depth += 1
            try:
                yield
            finally:
                state.depth -= 1
            return
        fd = opener() if opener is not None else os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o660)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LockAcquisitionTimeout(f"Timed out acquiring runtime lock '{path}'")
                    time.sleep(min(0.05, remaining))
            state.depth = 1
            try:
                yield
            finally:
                state.depth = 0
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    finally:
        state.lock.release()


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
        old_umask = os.umask(0)
        try:
            for part in directory.relative_to(base).parts:
                current /= part
                try:
                    current.mkdir(mode=0o2770)
                except FileExistsError:
                    continue
                try:
                    os.chown(current, -1, shared_gid)
                except OSError:
                    pass
                try:
                    os.chmod(current, 0o2770)
                except OSError:
                    pass
        finally:
            os.umask(old_umask)
    except OSError as exc:
        raise RuntimeError(f"Cannot establish shared runtime lock directory '{current}': {exc}") from exc
