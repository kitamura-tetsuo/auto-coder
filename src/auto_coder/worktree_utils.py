"""Private Git workspace utilities for local LLM execution."""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Generator, Optional, Union

from .logger_config import get_logger
from .utils import _COMMAND_EXECUTION_CWD, bind_command_execution_cwd, reset_command_execution_cwd

logger = get_logger(__name__)

_DISPOSABLE_DIRECTORY_NAMES = frozenset({".venv", "venv", ".agent-tmp", ".mypy_cache", ".pytest_cache", ".cache", "__pycache__", "node_modules"})


class WorkspacePreparationError(RuntimeError):
    """Raised when a private execution workspace cannot be prepared safely."""


@dataclass
class LocalWorkspaceOwnership:
    """Explicit execution and handoff releases required before disposal."""

    execution_released: bool = False
    handoff_released: bool = False
    _disposer: Optional[Callable[[], None]] = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def release_execution(self) -> None:
        with self._lock:
            self.execution_released = True
            self._dispose_if_released()

    def release_handoff(self) -> None:
        with self._lock:
            self.handoff_released = True
            self._dispose_if_released()

    def retain_until_released(self, disposer: Callable[[], None]) -> None:
        with self._lock:
            self._disposer = disposer
            self._dispose_if_released()

    def _dispose_if_released(self) -> None:
        if self.execution_released and self.handoff_released and self._disposer is not None:
            disposer = self._disposer
            self._disposer = None
            disposer()

    @property
    def can_dispose(self) -> bool:
        return self.execution_released and self.handoff_released


@dataclass(frozen=True)
class LocalWorkspaceBinding:
    """Controller-owned identity and immutable starting-state record."""

    invocation_id: str
    caller_root: Path
    caller_git_dir: Path
    caller_common_dir: Path
    initial_head: str
    initial_commit: str
    index_checksum: str
    file_snapshot_checksum: str
    workspace: Path
    ownership: LocalWorkspaceOwnership


_CURRENT_LOCAL_WORKSPACE: contextvars.ContextVar[Optional[LocalWorkspaceBinding]] = contextvars.ContextVar("auto_coder_local_workspace", default=None)


def get_current_local_workspace() -> Optional[LocalWorkspaceBinding]:
    """Return the binding owned by the current local invocation, if any."""
    return _CURRENT_LOCAL_WORKSPACE.get()


@dataclass(frozen=True)
class _CapturedFile:
    relative_path: str
    contents: bytes
    mode: int
    symlink: bool


@dataclass(frozen=True)
class _SourceSnapshot:
    binding: LocalWorkspaceBinding
    staged_diff: bytes
    unstaged_diff: bytes
    untracked: tuple[_CapturedFile, ...]
    directories: tuple[tuple[str, int], ...]
    tracked_modes: tuple[tuple[str, int], ...]
    consistency_token: str


def _resolve_target(cwd: Optional[Union[Path, str]] = None) -> Path:
    if cwd is not None:
        return Path(cwd)
    cmd_cwd = _COMMAND_EXECUTION_CWD.get()
    return Path(cmd_cwd) if cmd_cwd else Path.cwd()


def _git(target: Path, *args: str, input_data: Optional[bytes] = None) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(["git", *args], cwd=target, input=input_data, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise WorkspacePreparationError(f"git {' '.join(args)} failed: {detail}")
    return result


def is_inside_git_worktree(cwd: Optional[Union[Path, str]] = None) -> bool:
    """Return whether *cwd* is an attached or detached linked worktree."""
    try:
        return (_resolve_target(cwd) / ".git").is_file()
    except OSError:
        return False


def is_git_repository(cwd: Optional[Union[Path, str]] = None) -> bool:
    """Check if the given directory is inside a Git worktree."""
    try:
        result = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=_resolve_target(cwd), capture_output=True, text=True)
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def _path_bytes(path: Path) -> tuple[bytes, int, bool]:
    if path.is_symlink():
        return os.fsencode(os.readlink(path)), stat.S_IMODE(path.lstat().st_mode), True
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode), False


def _listed_paths(root: Path, *args: str) -> tuple[str, ...]:
    raw = _git(root, "ls-files", "-z", *args).stdout
    return tuple(os.fsdecode(item) for item in raw.split(b"\0") if item)


def _filesystem_token(root: Path, tracked: tuple[str, ...], untracked: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(set(tracked + untracked)):
        path = root / relative
        digest.update(os.fsencode(relative) + b"\0")
        try:
            data, mode, symlink = _path_bytes(path)
        except (FileNotFoundError, IsADirectoryError):
            digest.update(b"missing\0")
            continue
        digest.update(str(mode).encode() + bytes([symlink]) + hashlib.sha256(data).digest())
    return digest.hexdigest()


def _source_token(root: Path, tracked: tuple[str, ...], untracked: tuple[str, ...]) -> tuple[str, str, str]:
    head = _git(root, "rev-parse", "HEAD").stdout.strip().decode()
    symbolic = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=root, capture_output=True)
    head_identity = symbolic.stdout.strip() if symbolic.returncode == 0 else b"detached:" + head.encode()
    git_dir = Path(_git(root, "rev-parse", "--absolute-git-dir").stdout.strip().decode())
    index_path = Path(os.fsdecode(_git(root, "rev-parse", "--git-path", "index").stdout.strip()))
    if not index_path.is_absolute():
        index_path = root / index_path
    try:
        index_digest = hashlib.sha256(index_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise WorkspacePreparationError(f"unable to read caller index: {exc}") from exc
    token = hashlib.sha256(head_identity + b"\0" + head.encode() + b"\0" + index_digest.encode() + b"\0" + _filesystem_token(root, tracked, untracked).encode()).hexdigest()
    return token, index_digest, str(git_dir)


def _capture_source(target: Path, workspace: Path, ownership: Optional[LocalWorkspaceOwnership] = None) -> _SourceSnapshot:
    root = Path(_git(target, "rev-parse", "--show-toplevel").stdout.strip().decode()).resolve()
    if _git(root, "ls-files", "-u").stdout:
        raise WorkspacePreparationError("unmerged indexes are not supported")
    if _git(root, "ls-files", "-s").stdout.find(b"160000 ") >= 0:
        raise WorkspacePreparationError("Git submodule entries are not supported")
    sparse = subprocess.run(["git", "sparse-checkout", "list"], cwd=root, capture_output=True)
    if sparse.returncode == 0:
        raise WorkspacePreparationError("sparse checkouts are not supported")

    tracked = _listed_paths(root, "--cached")
    untracked_paths = tuple(path for path in _listed_paths(root, "--others") if not any(part in _DISPOSABLE_DIRECTORY_NAMES for part in Path(path).parts))
    token, index_digest, git_dir_text = _source_token(root, tracked, untracked_paths)
    commit = _git(root, "rev-parse", "HEAD").stdout.strip().decode()
    symbolic = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=root, capture_output=True, text=True)
    initial_head = symbolic.stdout.strip() if symbolic.returncode == 0 else f"detached:{commit}"
    common_dir = Path(_git(root, "rev-parse", "--git-common-dir").stdout.strip().decode())
    if not common_dir.is_absolute():
        common_dir = (root / common_dir).resolve()

    files: list[_CapturedFile] = []
    for relative in untracked_paths:
        path = root / relative
        try:
            contents, mode, symlink = _path_bytes(path)
        except OSError as exc:
            raise WorkspacePreparationError(f"unable to capture {relative}: {exc}") from exc
        files.append(_CapturedFile(relative, contents, mode, symlink))

    tracked_modes: list[tuple[str, int]] = []
    for relative in tracked:
        path = root / relative
        if path.exists() and not path.is_symlink() and path.is_file():
            tracked_modes.append((relative, stat.S_IMODE(path.stat().st_mode)))

    directories: list[tuple[str, int]] = []
    for current, names, _ in os.walk(root, followlinks=False):
        names[:] = [name for name in names if name != ".git" and name not in _DISPOSABLE_DIRECTORY_NAMES]
        for name in names:
            path = Path(current) / name
            if not path.is_symlink():
                directories.append((str(path.relative_to(root)), stat.S_IMODE(path.stat().st_mode)))

    binding = LocalWorkspaceBinding(
        invocation_id=uuid.uuid4().hex,
        caller_root=root,
        caller_git_dir=Path(git_dir_text).resolve(),
        caller_common_dir=common_dir,
        initial_head=initial_head,
        initial_commit=commit,
        index_checksum=index_digest,
        file_snapshot_checksum=token,
        workspace=workspace,
        ownership=ownership or LocalWorkspaceOwnership(),
    )
    return _SourceSnapshot(
        binding=binding,
        staged_diff=_git(root, "diff", "--cached", "--binary", "--full-index").stdout,
        unstaged_diff=_git(root, "diff", "--binary", "--full-index").stdout,
        untracked=tuple(files),
        directories=tuple(directories),
        tracked_modes=tuple(tracked_modes),
        consistency_token=token,
    )


def _seed_private_repository(snapshot: _SourceSnapshot) -> None:
    root = snapshot.binding.caller_root
    workspace = snapshot.binding.workspace
    clone = subprocess.run(["git", "clone", "--no-hardlinks", "--no-checkout", str(root), str(workspace)], capture_output=True)
    if clone.returncode != 0:
        detail = clone.stderr.decode("utf-8", errors="replace").strip()
        raise WorkspacePreparationError(f"private repository clone failed: {detail}")
    _git(workspace, "checkout", "--detach", snapshot.binding.initial_commit)
    for key in ("user.name", "user.email"):
        value = subprocess.run(["git", "config", "--get", key], cwd=root, capture_output=True)
        if value.returncode == 0:
            _git(workspace, "config", "--local", key, value.stdout.rstrip(b"\n").decode())
    if snapshot.staged_diff:
        _git(workspace, "apply", "--cached", "--binary", input_data=snapshot.staged_diff)
        _git(workspace, "checkout-index", "-a", "-f")
        removed = _git(workspace, "diff", "--cached", "--name-only", "--diff-filter=D", "-z").stdout
        for raw_path in filter(None, removed.split(b"\0")):
            path = workspace / os.fsdecode(raw_path)
            if path.exists() or path.is_symlink():
                path.unlink()
    if snapshot.unstaged_diff:
        _git(workspace, "apply", "--binary", "--whitespace=nowarn", input_data=snapshot.unstaged_diff)
    for relative, mode in snapshot.tracked_modes:
        path = workspace / relative
        if path.exists() and not path.is_symlink():
            path.chmod(mode)
    for relative, mode in snapshot.directories:
        destination = workspace / relative
        destination.mkdir(parents=True, exist_ok=True)
        destination.chmod(mode)
    for item in snapshot.untracked:
        destination = workspace / item.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            destination.unlink()
        if item.symlink:
            destination.symlink_to(os.fsdecode(item.contents))
        else:
            destination.write_bytes(item.contents)
            destination.chmod(item.mode)

    tracked = _listed_paths(root, "--cached")
    untracked = tuple(path for path in _listed_paths(root, "--others") if not any(part in _DISPOSABLE_DIRECTORY_NAMES for part in Path(path).parts))
    current_token, _, _ = _source_token(root, tracked, untracked)
    if current_token != snapshot.consistency_token:
        raise WorkspacePreparationError("caller Git identity, index, or files changed during workspace preparation")


def sync_worktree_changes_back(source_worktree: Union[Path, str], target_repo: Union[Path, str], baseline: str = "HEAD") -> None:
    """Copy the private repository's final file state back without copying Git state."""
    source = Path(source_worktree)
    target = Path(target_repo)
    del baseline  # The caller's dirty starting state is the file-copy baseline.
    source_tracked = set(_listed_paths(source, "--cached"))
    target_tracked = set(_listed_paths(target, "--cached"))
    source_untracked = {path for path in _listed_paths(source, "--others", "--exclude-standard") if not any(part in _DISPOSABLE_DIRECTORY_NAMES for part in Path(path).parts)}
    for relative in sorted(source_tracked | target_tracked | source_untracked):
        src = source / relative
        dst = target / relative
        if not src.exists() and not src.is_symlink():
            if relative in target_tracked and (dst.exists() or dst.is_symlink()):
                dst.unlink()
            continue
        source_value = _path_bytes(src)
        try:
            target_value = _path_bytes(dst)
        except (FileNotFoundError, IsADirectoryError):
            target_value = None
        if source_value == target_value:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        contents, mode, symlink = source_value
        if symlink:
            dst.symlink_to(os.fsdecode(contents))
        else:
            dst.write_bytes(contents)
            dst.chmod(mode)


@contextlib.contextmanager
def isolated_local_llm_worktree(
    base_cwd: Optional[Union[Path, str]] = None,
    is_noedit: bool = False,
    ownership: Optional[LocalWorkspaceOwnership] = None,
) -> Generator[str, None, None]:
    """Bind a Git-backed invocation to an independently cloned private repository."""
    target = _resolve_target(base_cwd)
    if not is_git_repository(target):
        yield str(target)
        return

    parent = Path(tempfile.mkdtemp(prefix="auto_coder_llm_"))
    workspace = parent / "repository"
    execution_token = None
    binding_token = None
    snapshot: Optional[_SourceSnapshot] = None
    try:
        snapshot = _capture_source(target, workspace, ownership)
        _seed_private_repository(snapshot)
    except WorkspacePreparationError:
        shutil.rmtree(parent, ignore_errors=True)
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(parent, ignore_errors=True)
        raise WorkspacePreparationError(f"private workspace preparation failed: {exc}") from exc

    try:
        binding_token = _CURRENT_LOCAL_WORKSPACE.set(snapshot.binding)
        execution_token = bind_command_execution_cwd(str(workspace))
        logger.debug("Bound private local LLM repository {} for invocation {}", workspace, snapshot.binding.invocation_id)
        yield str(workspace)
        if ownership is None:
            snapshot.binding.ownership.release_execution()
        if not is_noedit:
            sync_worktree_changes_back(workspace, snapshot.binding.caller_root, snapshot.binding.initial_commit)
        snapshot.binding.ownership.release_handoff()
    finally:
        if execution_token is not None:
            reset_command_execution_cwd(execution_token)
        if binding_token is not None:
            _CURRENT_LOCAL_WORKSPACE.reset(binding_token)
        if snapshot is not None and snapshot.binding.ownership.can_dispose:
            shutil.rmtree(parent, ignore_errors=True)
        elif snapshot is not None:
            logger.warning("Retaining private workspace {} until all owners release it", workspace)
            snapshot.binding.ownership.retain_until_released(lambda: shutil.rmtree(parent, ignore_errors=True))
