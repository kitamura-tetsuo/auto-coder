"""Serialize controller mutations of the same physical Git checkout."""

from __future__ import annotations

import subprocess
from contextlib import ExitStack, contextmanager
from functools import wraps
from pathlib import Path
from typing import Callable, Iterator, Optional, ParamSpec, TypeVar

from .logger_config import get_logger
from .runtime_locks import LockAcquisitionTimeout, ensure_lock_directory, file_lock, lock_path, runtime_root
from .shutdown_context import new_work_allowed
from .utils import _COMMAND_EXECUTION_CWD

P = ParamSpec("P")
T = TypeVar("T")
logger = get_logger(__name__)


@contextmanager
def checkout_lock(cwd: Optional[str] = None) -> Iterator[None]:
    """Hold a reentrant checkout lease, interruptibly while waiting."""
    root = Path(cwd or _COMMAND_EXECUTION_CWD.get() or Path.cwd()).resolve()
    result = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=root, capture_output=True, text=True, check=False)
    identity = Path(result.stdout.strip()).resolve() if result.returncode == 0 else root
    directory = identity.stat()
    path = lock_path("local/checkout", runtime_root(), "checkout", f"{directory.st_dev}:{directory.st_ino}")
    ensure_lock_directory(path, directory.st_gid)
    waiting = False
    with ExitStack() as stack:
        while True:
            try:
                stack.enter_context(file_lock(path, timeout=0.1))
                if waiting:
                    logger.info("Acquired controller checkout ownership: {}", root)
                break
            except LockAcquisitionTimeout:
                if not waiting:
                    logger.info("Waiting for controller checkout ownership: {}", root)
                    waiting = True
                if not new_work_allowed():
                    raise RuntimeError("Checkout mutation deferred because graceful shutdown is draining")
        yield


def _worktree_common_directory(root: Path) -> Path:
    result = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=root, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("Cannot identify Git common directory for worktree registration")
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = root / common
    common = common.resolve(strict=True)
    if not common.is_dir():
        raise RuntimeError("Git common directory is not a directory")
    return common


@contextmanager
def worktree_registry_lock(cwd: str) -> Iterator[None]:
    """Serialize Git preparation/removal transactions, not validation."""
    root = Path(cwd).absolute()
    common = _worktree_common_directory(root)
    directory = common.stat()
    identity = (directory.st_dev, directory.st_ino)
    path = lock_path("local/worktrees", runtime_root(), "registry", f"{identity[0]}:{identity[1]}")
    ensure_lock_directory(path, directory.st_gid)
    with file_lock(path):
        current = _worktree_common_directory(root).stat()
        if (current.st_dev, current.st_ino) != identity:
            raise RuntimeError("Git common directory changed while awaiting registration")
        yield


def serialize_checkout(function: Callable[P, T]) -> Callable[P, T]:
    """Protect a controller operation using its ambient command checkout."""

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        with checkout_lock():
            return function(*args, **kwargs)

    return wrapped
