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


class WorkspaceHandoffError(WorkspacePreparationError):
    """Raised when a completed private result cannot be applied atomically."""


@dataclass(frozen=True)
class WorkspaceFileState:
    """A non-dereferencing snapshot of one supported working-tree path."""

    relative_path: str
    contents: bytes
    mode: int
    symlink: bool


@dataclass
class LocalWorkspaceOwnership:
    """Explicit execution and handoff releases required before disposal."""

    execution_released: bool = False
    handoff_released: bool = False
    session_released: bool = True
    handoff_authorized: bool = False
    authorized_turn_id: Optional[str] = None
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

    def retain_session(self) -> None:
        """Keep the private root alive for an explicitly owned provider session."""
        with self._lock:
            self.session_released = False

    def release_session(self) -> None:
        with self._lock:
            self.session_released = True
            self._dispose_if_released()

    def authorize_handoff(self, invocation_id: str, turn_id: str, expected_invocation_id: str) -> None:
        """Authorize copying only for the exact successfully settled generation."""
        with self._lock:
            if invocation_id != expected_invocation_id or not turn_id:
                raise WorkspaceHandoffError("handoff evidence belongs to a different result generation")
            self.handoff_authorized = True
            self.authorized_turn_id = turn_id

    def retain_until_released(self, disposer: Callable[[], None]) -> None:
        with self._lock:
            self._disposer = disposer
            self._dispose_if_released()

    def _dispose_if_released(self) -> None:
        if self.execution_released and self.handoff_released and self.session_released and self._disposer is not None:
            disposer = self._disposer
            self._disposer = None
            disposer()

    @property
    def can_dispose(self) -> bool:
        return self.execution_released and self.handoff_released and self.session_released


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
    initial_files: tuple[WorkspaceFileState, ...] = ()
    caller_git_identity: Optional[tuple[int, int]] = None
    caller_common_identity: Optional[tuple[int, int]] = None


_CURRENT_LOCAL_WORKSPACE: contextvars.ContextVar[Optional[LocalWorkspaceBinding]] = contextvars.ContextVar("auto_coder_local_workspace", default=None)


def get_current_local_workspace() -> Optional[LocalWorkspaceBinding]:
    """Return the binding owned by the current local invocation, if any."""
    return _CURRENT_LOCAL_WORKSPACE.get()


@contextlib.contextmanager
def bind_retained_local_workspace(
    binding: LocalWorkspaceBinding,
    *,
    is_noedit: bool,
) -> Generator[str, None, None]:
    """Re-enter the exact retained root and hand off this turn's editable delta."""
    if not binding.workspace.is_dir():
        raise WorkspacePreparationError("retained local workspace is unavailable")
    if _CURRENT_LOCAL_WORKSPACE.get() is not None:
        raise WorkspacePreparationError("a local workspace is already bound")
    binding_token = _CURRENT_LOCAL_WORKSPACE.set(binding)
    execution_token = bind_command_execution_cwd(str(binding.workspace))
    try:
        yield str(binding.workspace)
        if not is_noedit:
            sync_worktree_changes_back(binding.workspace, binding.caller_root, binding)
    finally:
        reset_command_execution_cwd(execution_token)
        _CURRENT_LOCAL_WORKSPACE.reset(binding_token)


def refresh_local_workspace_binding(binding: LocalWorkspaceBinding) -> LocalWorkspaceBinding:
    """Advance a retained binding to the exact post-handoff caller checkpoint.

    The invocation and private-root identities remain unchanged.  Both roots must
    have identical supported file state, otherwise no next-generation binding can
    be issued.
    """
    caller = binding.caller_root.resolve()
    workspace = binding.workspace.resolve()
    tracked = _listed_paths(caller, "--cached")
    untracked = tuple(path for path in _listed_paths(caller, "--others") if not any(part in _DISPOSABLE_DIRECTORY_NAMES for part in Path(path).parts))
    token, index_checksum, git_dir_text = _source_token(caller, tracked, untracked)
    private_paths = set(_listed_paths(workspace, "--cached"))
    private_paths.update(path for path in _listed_paths(workspace, "--others") if not any(part in _DISPOSABLE_DIRECTORY_NAMES for part in Path(path).parts))
    caller_states = _capture_file_states(caller, set(tracked) | set(untracked))
    private_states = _capture_file_states(workspace, private_paths)
    if caller_states != private_states:
        raise WorkspaceHandoffError("retained private root does not match the post-handoff caller checkpoint")
    git_dir = Path(git_dir_text).resolve()
    common_dir = Path(_git(caller, "rev-parse", "--git-common-dir").stdout.strip().decode())
    if not common_dir.is_absolute():
        common_dir = (caller / common_dir).resolve()
    return LocalWorkspaceBinding(
        invocation_id=binding.invocation_id,
        caller_root=caller,
        caller_git_dir=git_dir,
        caller_common_dir=common_dir,
        initial_head=binding.initial_head,
        initial_commit=_git(caller, "rev-parse", "HEAD").stdout.strip().decode(),
        index_checksum=index_checksum,
        file_snapshot_checksum=token,
        workspace=workspace,
        ownership=binding.ownership,
        initial_files=private_states,
        caller_git_identity=(git_dir.stat().st_dev, git_dir.stat().st_ino),
        caller_common_identity=(common_dir.stat().st_dev, common_dir.stat().st_ino),
    )


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


def _capture_file_states(root: Path, paths: set[str]) -> tuple[WorkspaceFileState, ...]:
    states: list[WorkspaceFileState] = []
    for relative in sorted(paths):
        path = root / relative
        try:
            contents, mode, symlink = _path_bytes(path)
        except (FileNotFoundError, IsADirectoryError):
            continue
        states.append(WorkspaceFileState(relative, contents, mode, symlink))
    return tuple(states)


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
        initial_files=_capture_file_states(
            root,
            set(tracked) | {path for path in _listed_paths(root, "--others", "--exclude-standard") if not any(part in _DISPOSABLE_DIRECTORY_NAMES for part in Path(path).parts)},
        ),
        workspace=workspace,
        ownership=ownership or LocalWorkspaceOwnership(),
        caller_git_identity=(Path(git_dir_text).resolve().stat().st_dev, Path(git_dir_text).resolve().stat().st_ino),
        caller_common_identity=(common_dir.stat().st_dev, common_dir.stat().st_ino),
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


_HANDOFF_LOCKS: dict[Path, threading.Lock] = {}
_HANDOFF_LOCKS_GUARD = threading.Lock()


def _handoff_lock(root: Path) -> threading.Lock:
    with _HANDOFF_LOCKS_GUARD:
        return _HANDOFF_LOCKS.setdefault(root.resolve(), threading.Lock())


def _assert_safe_destination(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or relative in {"", ".git"}:
        raise WorkspaceHandoffError(f"unsafe result path: {relative}")
    destination = root / candidate
    current = root
    for part in candidate.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise WorkspaceHandoffError(f"destination ancestor is a symlink: {relative}")
        if current.exists() and not current.is_dir():
            raise WorkspaceHandoffError(f"destination ancestor is not a directory: {relative}")
    return destination


def _restore_path(path: Path, state: Optional[WorkspaceFileState]) -> None:
    if path.exists() or path.is_symlink():
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    if state is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if state.symlink:
        path.symlink_to(os.fsdecode(state.contents))
    else:
        path.write_bytes(state.contents)
        path.chmod(state.mode)


def _result_has_file_delta(source: Path, binding: LocalWorkspaceBinding) -> bool:
    baseline_states = {item.relative_path: item for item in binding.initial_files}
    final_paths = set(_listed_paths(source, "--cached"))
    final_paths.update(path for path in _listed_paths(source, "--others", "--exclude-standard") if not any(part in _DISPOSABLE_DIRECTORY_NAMES for part in Path(path).parts))
    final_paths.update(baseline_states)
    final_states = {item.relative_path: item for item in _capture_file_states(source, final_paths)}
    return baseline_states != final_states


def sync_worktree_changes_back(
    source_worktree: Union[Path, str],
    target_repo: Union[Path, str],
    baseline: Union[str, LocalWorkspaceBinding] = "HEAD",
) -> None:
    """Apply the final file delta while the immutable caller checkpoint still matches.

    Git history and the private index are deliberately irrelevant: the baseline and
    final working-file snapshots determine the result.
    """
    source = Path(source_worktree)
    target = Path(target_repo).resolve()
    if not isinstance(baseline, LocalWorkspaceBinding):
        raise WorkspaceHandoffError("an immutable workspace binding is required for handoff")
    binding = baseline
    if source.resolve() != binding.workspace.resolve() or target != binding.caller_root.resolve():
        raise WorkspaceHandoffError("result root or caller target does not match its binding")
    if _git(source, "ls-files", "-u").stdout:
        raise WorkspaceHandoffError("private result has an unresolved index")
    private_git_dir = Path(_git(source, "rev-parse", "--absolute-git-dir").stdout.strip().decode())
    unfinished = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD", "rebase-merge", "rebase-apply", "sequencer")
    if any((private_git_dir / marker).exists() for marker in unfinished):
        raise WorkspaceHandoffError("private result has an unfinished Git operation")

    baseline_states = {item.relative_path: item for item in binding.initial_files}
    final_paths = set(_listed_paths(source, "--cached"))
    final_paths.update(path for path in _listed_paths(source, "--others", "--exclude-standard") if not any(part in _DISPOSABLE_DIRECTORY_NAMES for part in Path(path).parts))
    # A baseline source file remains in scope even when the result adds an ignore rule.
    final_paths.update(baseline_states)
    final_states = {item.relative_path: item for item in _capture_file_states(source, final_paths)}
    changed = sorted(path for path in baseline_states.keys() | final_states.keys() if baseline_states.get(path) != final_states.get(path))

    with _handoff_lock(target):
        tracked = _listed_paths(target, "--cached")
        context_untracked = tuple(path for path in _listed_paths(target, "--others") if not any(part in _DISPOSABLE_DIRECTORY_NAMES for part in Path(path).parts))
        current_token, _, current_git_dir = _source_token(target, tracked, context_untracked)
        current_common_dir = Path(_git(target, "rev-parse", "--git-common-dir").stdout.strip().decode())
        if not current_common_dir.is_absolute():
            current_common_dir = (target / current_common_dir).resolve()
        current_git_path = Path(current_git_dir).resolve()
        current_git_identity = (current_git_path.stat().st_dev, current_git_path.stat().st_ino)
        current_common_identity = (current_common_dir.stat().st_dev, current_common_dir.stat().st_ino)
        if (
            current_git_path != binding.caller_git_dir.resolve()
            or current_common_dir != binding.caller_common_dir.resolve()
            or (binding.caller_git_identity is not None and current_git_identity != binding.caller_git_identity)
            or (binding.caller_common_identity is not None and current_common_identity != binding.caller_common_identity)
        ):
            raise WorkspaceHandoffError("caller Git identity changed; refusing stale result")
        if current_token != binding.file_snapshot_checksum:
            raise WorkspaceHandoffError("caller checkpoint changed; refusing stale result")

        destinations = {path: _assert_safe_destination(target, path) for path in changed}
        before: dict[str, Optional[WorkspaceFileState]] = {}
        for relative, destination in destinations.items():
            try:
                contents, mode, symlink = _path_bytes(destination)
                before[relative] = WorkspaceFileState(relative, contents, mode, symlink)
            except (FileNotFoundError, IsADirectoryError):
                before[relative] = None
            if destination.exists() and destination.is_dir() and not destination.is_symlink():
                raise WorkspaceHandoffError(f"destination path/type conflict: {relative}")

        applied: list[str] = []
        try:
            for relative in changed:
                applied.append(relative)
                _restore_path(destinations[relative], final_states.get(relative))
        except OSError as exc:
            for relative in reversed(applied):
                _restore_path(destinations[relative], before[relative])
            raise WorkspaceHandoffError(f"result application failed and was rolled back: {exc}") from exc


@contextlib.contextmanager
def isolated_local_llm_worktree(
    base_cwd: Optional[Union[Path, str]] = None,
    is_noedit: bool = False,
    ownership: Optional[LocalWorkspaceOwnership] = None,
    require_handoff_authorization: bool = False,
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
            if require_handoff_authorization and not snapshot.binding.ownership.handoff_authorized and _result_has_file_delta(workspace, snapshot.binding):
                raise WorkspaceHandoffError("successful generation-bound handoff evidence is missing")
            sync_worktree_changes_back(workspace, snapshot.binding.caller_root, snapshot.binding)
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
