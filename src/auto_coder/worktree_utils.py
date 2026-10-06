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

from .execution_trace import EventKind, Outcome, get_trace_collector
from .logger_config import get_logger
from .security_utils import redact_string
from .utils import _COMMAND_EXECUTION_CWD, CommandExecutor, bind_command_execution_cwd, reset_command_execution_cwd

logger = get_logger(__name__)

_DISPOSABLE_DIRECTORY_NAMES = frozenset({".venv", "venv", ".agent-tmp", ".mypy_cache", ".pytest_cache", ".cache", "__pycache__", "node_modules", "coverage", "coverage-backups", "htmlcov", "playwright-report", "test-results"})
_WORKSPACE_FREE_RESERVE = 1024**3


class WorkspacePreparationError(RuntimeError):
    """Raised when a private execution workspace cannot be prepared safely."""


class WorkspaceHandoffError(WorkspacePreparationError):
    """Raised when a completed private result cannot be applied atomically."""


def record_implementation_workspace_tests_skipped(binding: LocalWorkspaceBinding) -> None:
    """Report that the automatic baseline was intentionally not run (CI-repair policy).

    Emits a SKIPPED result for this invocation only. It runs nothing, writes no
    baseline log, and asserts neither dependency preparation nor verification.
    """
    logger.info("Skipping initial tests in private implementation workspace {} (CI-repair policy); baseline not run", binding.workspace)
    get_trace_collector().record_event(
        EventKind.STAGE_RESULT,
        "local.workspace-tests",
        "local-backend",
        label="Implementation workspace initial tests",
        outcome=Outcome.SKIPPED,
        facts={"invocation_id": binding.invocation_id, "baseline": "not_run", "reason": "ci_repair_policy"},
    )


def run_implementation_workspace_tests(binding: LocalWorkspaceBinding, test_script_path: str) -> None:
    """Prepare a new editable root through the target's startup-validated script.

    Ordinary test failures are baseline evidence for the implementation task.
    Launch failures and timeouts refuse provider submission. Do not probe for
    the script again or redirect execution into another repository/container.
    """
    collector = get_trace_collector()
    facts = {"invocation_id": binding.invocation_id, "test_script": test_script_path}
    collector.record_event(EventKind.STAGE_STARTED, "local.workspace-tests", "local-backend", label="Implementation workspace initial tests", facts=facts)
    logger.info("Running initial tests in private implementation workspace {}", binding.workspace)
    result = CommandExecutor.run_command(
        ["bash", test_script_path],
        cwd=str(binding.workspace),
        timeout=CommandExecutor.DEFAULT_TIMEOUTS["test"],
        env_overrides={"INSIDE_TARGET_EXECUTION": "true", "AM_I_AUTOCODER_CONTAINER": "false"},
        stdin_text="",
    )
    log_path = binding.workspace / ".agent-tmp" / "initial-tests.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(redact_string(f"exit_code={result.returncode}\n{result.stdout}\n{result.stderr}"), encoding="utf-8")
    collector.record_event(EventKind.STAGE_RESULT, "local.workspace-tests", "local-backend", label="Implementation workspace initial tests", outcome=Outcome.COMPLETED if result.success else Outcome.FAILED, facts={**facts, "exit_code": result.returncode, "log_path": ".agent-tmp/initial-tests.log"})
    if result.returncode < 0 or result.returncode in {126, 127}:
        raise WorkspacePreparationError(f"implementation workspace test script could not complete (exit_code={result.returncode}); see {log_path}")
    if not result.success:
        logger.warning("Initial implementation tests failed (exit_code={}); baseline log: {}", result.returncode, log_path)


@dataclass(frozen=True)
class WorkspaceFileState:
    """A non-dereferencing snapshot of one supported working-tree path."""

    relative_path: str
    checksum: str
    mode: int
    symlink: bool
    source_path: Path = field(compare=False, repr=False)
    link_target: bytes = b""


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

    def begin_execution(self) -> None:
        """A retained turn needs its own positive writer-settlement release."""
        with self._lock:
            self.execution_released = False

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


def current_local_caller_identity(cwd: Optional[Union[Path, str]] = None) -> str:
    """Return an inode-bound identity for the caller repository at this boundary."""
    target = _resolve_target(cwd)
    root = Path(_git(target, "rev-parse", "--show-toplevel").stdout.strip().decode()).resolve()
    git_dir = Path(_git(root, "rev-parse", "--absolute-git-dir").stdout.strip().decode()).resolve()
    common_dir = Path(_git(root, "rev-parse", "--git-common-dir").stdout.strip().decode())
    if not common_dir.is_absolute():
        common_dir = (root / common_dir).resolve()
    root_stat = root.stat()
    git_stat = git_dir.stat()
    common_stat = common_dir.stat()
    return f"{root}:{root_stat.st_dev}:{root_stat.st_ino}:{git_stat.st_dev}:{git_stat.st_ino}:{common_stat.st_dev}:{common_stat.st_ino}"


def current_local_caller_checkpoint(binding: LocalWorkspaceBinding) -> str:
    """Return the current caller checkpoint after validating its Git identity."""
    caller = binding.caller_root.resolve()
    git_dir = Path(_git(caller, "rev-parse", "--absolute-git-dir").stdout.strip().decode()).resolve()
    common_dir = Path(_git(caller, "rev-parse", "--git-common-dir").stdout.strip().decode())
    if not common_dir.is_absolute():
        common_dir = (caller / common_dir).resolve()
    if (
        git_dir != binding.caller_git_dir.resolve()
        or common_dir != binding.caller_common_dir.resolve()
        or (binding.caller_git_identity is not None and (git_dir.stat().st_dev, git_dir.stat().st_ino) != binding.caller_git_identity)
        or (binding.caller_common_identity is not None and (common_dir.stat().st_dev, common_dir.stat().st_ino) != binding.caller_common_identity)
    ):
        raise WorkspaceHandoffError("caller Git identity changed; refusing continuation")
    tracked = _listed_paths(caller, "--cached")
    untracked = tuple(path for path in _listed_paths(caller, "--others") if not any(part in _DISPOSABLE_DIRECTORY_NAMES for part in Path(path).parts))
    checkpoint, _, _ = _source_token(caller, tracked, untracked)
    return checkpoint


@contextlib.contextmanager
def bind_retained_local_workspace(
    binding: LocalWorkspaceBinding,
    *,
    is_noedit: bool,
) -> Generator[str, None, None]:
    """Re-enter the exact retained root without granting result handoff."""
    if not binding.workspace.is_dir():
        raise WorkspacePreparationError("retained local workspace is unavailable")
    if _CURRENT_LOCAL_WORKSPACE.get() is not None:
        raise WorkspacePreparationError("a local workspace is already bound")
    binding_token = _CURRENT_LOCAL_WORKSPACE.set(binding)
    execution_token = bind_command_execution_cwd(str(binding.workspace))
    try:
        yield str(binding.workspace)
    finally:
        reset_command_execution_cwd(execution_token)
        _CURRENT_LOCAL_WORKSPACE.reset(binding_token)


def refresh_local_workspace_binding(binding: LocalWorkspaceBinding) -> LocalWorkspaceBinding:
    """Advance a retained binding to the exact post-handoff caller checkpoint.

    The invocation and private-root identities remain unchanged.  Both roots must
    have identical handoff-supported file state, otherwise no next-generation binding can
    be issued.
    """
    caller = binding.caller_root.resolve()
    workspace = binding.workspace.resolve()
    tracked = _listed_paths(caller, "--cached")
    untracked = tuple(path for path in _listed_paths(caller, "--others") if not any(part in _DISPOSABLE_DIRECTORY_NAMES for part in Path(path).parts))
    token, index_checksum, git_dir_text = _source_token(caller, tracked, untracked)
    # Compare the same source scope used by handoff. New ignored build output is
    # private runtime context, while baseline and privately tracked paths remain
    # source even if the provider changes ignore rules or the private index.
    result_paths = _result_file_paths(caller, binding) | _result_file_paths(workspace, binding)
    caller_states = _capture_file_states(caller, result_paths)
    private_states = _capture_file_states(workspace, result_paths)
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
class _SourceSnapshot:
    binding: LocalWorkspaceBinding
    staged_diff: Path
    unstaged_diff: Path
    untracked: tuple[WorkspaceFileState, ...]
    directories: tuple[tuple[str, int], ...]
    tracked_modes: tuple[tuple[str, int], ...]
    consistency_token: str


def _resolve_target(cwd: Optional[Union[Path, str]] = None) -> Path:
    if cwd is not None:
        return Path(cwd)
    cmd_cwd = _COMMAND_EXECUTION_CWD.get()
    return Path(cmd_cwd) if cmd_cwd else Path.cwd()


def _git(target: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(["git", *args], cwd=target, capture_output=True)
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


def _file_checksum(path: Path) -> str:
    """Hash regular files with bounded memory, including large runtime context."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _capture_file_state(root: Path, relative: str, storage: Optional[Path] = None) -> WorkspaceFileState:
    path = root / relative
    if path.is_symlink():
        target = os.fsencode(os.readlink(path))
        return WorkspaceFileState(relative, hashlib.sha256(target).hexdigest(), stat.S_IMODE(path.lstat().st_mode), True, path, target)
    mode = stat.S_IMODE(path.stat().st_mode)
    source = path
    if storage is not None:
        # Flat, unique names avoid conflicts between deleted files and new directories.
        source = storage / uuid.uuid4().hex
        shutil.copyfile(path, source)
    return WorkspaceFileState(relative, _file_checksum(source), mode, False, source)


def _capture_file_states(root: Path, paths: set[str]) -> tuple[WorkspaceFileState, ...]:
    states: list[WorkspaceFileState] = []
    for relative in sorted(paths):
        try:
            states.append(_capture_file_state(root, relative))
        except (FileNotFoundError, IsADirectoryError):
            continue
    return tuple(states)


def _listed_paths(root: Path, *args: str) -> tuple[str, ...]:
    raw = _git(root, "ls-files", "-z", *args).stdout
    return tuple(os.fsdecode(item) for item in raw.split(b"\0") if item)


def _filesystem_token(root: Path, tracked: tuple[str, ...], untracked: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(set(tracked + untracked)):
        digest.update(os.fsencode(relative) + b"\0")
        try:
            state = _capture_file_state(root, relative)
        except (FileNotFoundError, IsADirectoryError):
            digest.update(b"missing\0")
            continue
        digest.update(str(state.mode).encode() + bytes([state.symlink]) + bytes.fromhex(state.checksum))
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
        index_digest = _file_checksum(index_path)
    except OSError as exc:
        raise WorkspacePreparationError(f"unable to read caller index: {exc}") from exc
    token = hashlib.sha256(head_identity + b"\0" + head.encode() + b"\0" + index_digest.encode() + b"\0" + _filesystem_token(root, tracked, untracked).encode()).hexdigest()
    return token, index_digest, str(git_dir)


def _check_workspace_capacity(root: Path, destination: Path, tracked: tuple[str, ...], untracked: tuple[str, ...], common_dir: Path) -> None:
    """Reserve room for context copies, Git data, patches and source rollback.

    This is a preflight estimate, not a quota on provider/test output. Count
    logical sizes conservatively because copying can expand sparse files.
    """
    required = _WORKSPACE_FREE_RESERVE
    for paths, copies in ((tracked, 3), (untracked, 2)):
        for relative in paths:
            path = root / relative
            if not path.is_symlink() and path.is_file():
                required += path.stat().st_size * copies
    for current, _, files in os.walk(common_dir, followlinks=False):
        for name in files:
            path = Path(current) / name
            if not path.is_symlink():
                required += path.stat().st_size
    free = shutil.disk_usage(destination).free
    if free < required:
        raise WorkspacePreparationError(f"insufficient disk space for private workspace: need {required} bytes including reserve, have {free}; remove inactive workspaces or generated artifacts before retrying")


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

    _check_workspace_capacity(root, workspace.parent, tracked, untracked_paths, common_dir)
    storage = workspace.parent / "source-snapshot"
    storage.mkdir()
    files: list[WorkspaceFileState] = []
    for relative in untracked_paths:
        try:
            files.append(_capture_file_state(root, relative, storage))
        except OSError as exc:
            raise WorkspacePreparationError(f"unable to capture {relative}: {exc}") from exc

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
        staged_diff=_capture_git_patch(root, storage / "staged.patch", "--cached"),
        unstaged_diff=_capture_git_patch(root, storage / "unstaged.patch"),
        untracked=tuple(files),
        directories=tuple(directories),
        tracked_modes=tuple(tracked_modes),
        consistency_token=token,
    )


def _capture_git_patch(root: Path, destination: Path, *args: str) -> Path:
    """Spool binary patches to disk instead of retaining their full stdout."""
    with destination.open("wb") as stream:
        result = subprocess.run(["git", "diff", *args, "--binary", "--full-index"], cwd=root, stdout=stream, stderr=subprocess.PIPE)
    if result.returncode != 0:
        raise WorkspacePreparationError(f"unable to capture Git patch: {os.fsdecode(result.stderr).strip()}")
    return destination


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
    if snapshot.staged_diff.stat().st_size:
        _git(workspace, "apply", "--cached", "--binary", str(snapshot.staged_diff))
        _git(workspace, "checkout-index", "-a", "-f")
        removed = _git(workspace, "diff", "--cached", "--name-only", "--diff-filter=D", "-z").stdout
        for raw_path in filter(None, removed.split(b"\0")):
            path = workspace / os.fsdecode(raw_path)
            if path.exists() or path.is_symlink():
                path.unlink()
    if snapshot.unstaged_diff.stat().st_size:
        _git(workspace, "apply", "--binary", "--whitespace=nowarn", str(snapshot.unstaged_diff))
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
        _restore_path(destination, item)

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
        path.symlink_to(os.fsdecode(state.link_target))
    else:
        shutil.copyfile(state.source_path, path)
        path.chmod(state.mode)


def _result_file_paths(root: Path, binding: LocalWorkspaceBinding) -> set[str]:
    """Select tracked, non-ignored source and preserved baseline paths for handoff."""
    paths = set(_listed_paths(root, "--cached"))
    paths.update(path for path in _listed_paths(root, "--others", "--exclude-standard") if not any(part in _DISPOSABLE_DIRECTORY_NAMES for part in Path(path).parts))
    paths.update(state.relative_path for state in binding.initial_files)
    return paths


def _result_has_file_delta(source: Path, binding: LocalWorkspaceBinding) -> bool:
    baseline_states = {item.relative_path: item for item in binding.initial_files}
    final_paths = _result_file_paths(source, binding)
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
    final_paths = _result_file_paths(source, binding)
    final_states = {item.relative_path: item for item in _capture_file_states(source, final_paths)}
    changed = sorted(path for path in baseline_states.keys() | final_states.keys() if baseline_states.get(path) != final_states.get(path))

    with _handoff_lock(target), tempfile.TemporaryDirectory(prefix="handoff-", dir=binding.workspace.parent) as rollback_directory:
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
        for relative in changed:
            if relative not in final_states:
                continue
            try:
                captured = _capture_file_state(source, relative, Path(rollback_directory))
            except OSError as exc:
                raise WorkspaceHandoffError(f"unable to capture final result {relative}: {exc}") from exc
            if captured != final_states[relative]:
                raise WorkspaceHandoffError("private result changed during handoff capture")
            final_states[relative] = captured
        for relative, destination in destinations.items():
            try:
                before[relative] = _capture_file_state(target, relative, Path(rollback_directory))
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
    except BaseException:
        # No provider has started during preparation, including interruption.
        shutil.rmtree(parent, ignore_errors=True)
        raise

    try:
        binding_token = _CURRENT_LOCAL_WORKSPACE.set(snapshot.binding)
        execution_token = bind_command_execution_cwd(str(workspace))
        logger.debug("Bound private local LLM repository {} for invocation {}", workspace, snapshot.binding.invocation_id)
        yield str(workspace)
        if not is_noedit:
            if require_handoff_authorization and not snapshot.binding.ownership.handoff_authorized and _result_has_file_delta(workspace, snapshot.binding):
                raise WorkspaceHandoffError("successful generation-bound handoff evidence is missing")
            sync_worktree_changes_back(workspace, snapshot.binding.caller_root, snapshot.binding)
    finally:
        # Handoff is finished or abandoned even if the provider or application
        # failed. External execution owners still control writer settlement.
        snapshot.binding.ownership.release_handoff()
        if ownership is None:
            snapshot.binding.ownership.release_execution()
        if execution_token is not None:
            reset_command_execution_cwd(execution_token)
        if binding_token is not None:
            _CURRENT_LOCAL_WORKSPACE.reset(binding_token)
        if snapshot is not None and snapshot.binding.ownership.can_dispose:
            shutil.rmtree(parent, ignore_errors=True)
        elif snapshot is not None:
            logger.warning("Retaining private workspace {} until all owners release it", workspace)
            snapshot.binding.ownership.retain_until_released(lambda: shutil.rmtree(parent, ignore_errors=True))
