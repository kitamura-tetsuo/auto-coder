"""Non-interactive Muse Code CLI client using the shared local boundary."""

from __future__ import annotations

import ctypes
import json
import os
import select
import shlex
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn, Optional, Sequence

from .exceptions import AutoCoderTimeoutError, AutoCoderUsageLimitError
from .execution_trace import EventKind, Outcome, get_trace_collector
from .llm_backend_config import get_llm_config
from .llm_client_base import LLMClientBase
from .local_execution_boundary import get_current_local_execution_boundary
from .logger_config import get_logger
from .prompt_loader import render_prompt
from .usage_marker_utils import has_http_429_marker, has_usage_marker_match
from .utils import _COMMAND_EXECUTION_CWD

logger = get_logger(__name__)

_MUSE_MSP_DIAGNOSTIC_SCHEMAS = frozenset(
    {
        (1, "sha256:b1e6676d624e116e2c1b150fec3192200d2cbca8ed79898e44f8921759c7872f"),
        (1, "sha256:e0e163db6ccf00dbe68402ce55d6319b3edc33c421f31e9583b587b2de8a118f"),
    }
)
_MUSE_MSP_CLIENT_NAME = "auto_coder"
_MUSE_141_MODEL_ALIASES = {"muse-spark-1.3": "muse-spark-1.3-contributor"}
_MUSE_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
# Auto-Coder policy (not a Muse timeout): how long one host-resolved approval may stay pending.
_MUSE_APPROVAL_SETTLEMENT_SECONDS = 5.0
_APPROVAL_POLICY_RESULTS = frozenset({"deny", "allow"})


class _MspEndOfStream(Exception):
    """Signal an expected host EOF while draining a completed turn."""


_READ_ONLY_GIT_COMMANDS = {
    "blame",
    "cat-file",
    "describe",
    "diff",
    "diff-tree",
    "for-each-ref",
    "grep",
    "log",
    "ls-files",
    "ls-tree",
    "merge-base",
    "name-rev",
    "rev-list",
    "rev-parse",
    "shortlog",
    "show",
    "show-ref",
    "status",
}

_DISPOSABLE_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        ".agent-tmp",
        ".mypy_cache",
        ".pytest_cache",
        ".cache",
        "__pycache__",
        "node_modules",
    }
)
_DISPOSABLE_DIRECTORY_PREFIXES = tuple(f"{name}/" for name in _DISPOSABLE_DIRECTORY_NAMES)


def _read_only_special_git_command(name: object, argv: object) -> bool:
    """Recognize inspection-only forms of Git commands that also mutate."""
    if not isinstance(argv, list) or not all(isinstance(argument, str) for argument in argv):
        return False
    try:
        command_index = argv.index(str(name))
    except ValueError:
        return False
    arguments = argv[command_index + 1 :]
    if name == "branch":
        mutating_flags = {"-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy", "--edit-description", "--set-upstream-to", "--unset-upstream"}
        if any(argument in mutating_flags for argument in arguments):
            return False
        if any(argument in {"--list", "-l", "--contains", "--no-contains", "--merged", "--no-merged"} for argument in arguments):
            return True
        return not any(not argument.startswith("-") for argument in arguments)
    if name == "symbolic-ref":
        return len([argument for argument in arguments if not argument.startswith("-")]) <= 1
    if name == "tag":
        return not any(not argument.startswith("-") for argument in arguments)
    if name == "worktree":
        return bool(arguments) and arguments[0] == "list"
    return False


def _execution_cwd() -> Path:
    override = _COMMAND_EXECUTION_CWD.get()
    return Path(override) if override else Path.cwd()


@dataclass(frozen=True)
class _WorkspaceFile:
    path: str
    contents: bytes
    is_symlink: bool
    mode: int


@dataclass(frozen=True)
class _WorkspaceMode:
    path: str
    mode: int


@dataclass(frozen=True)
class _GitState:
    worktree: Path
    git_dir: Path
    branch: Optional[str]
    head: str
    index_state: bytes
    status: bytes
    staged_patch: bytes
    unstaged_patch: bytes
    untracked_files: tuple[_WorkspaceFile, ...]
    ignored_files: tuple[_WorkspaceFile, ...]
    tracked_modes: tuple[_WorkspaceMode, ...]
    directory_modes: tuple[_WorkspaceMode, ...]


@dataclass(frozen=True)
class _MspOptions:
    host_arguments: list[str]
    reasoning_effort: Optional[str]
    noedit: bool
    approval_denial: bool


@dataclass
class _ApprovalObservation:
    """One approval observed in the current invocation, keyed by session and approval id."""

    first_seen: float
    turn_id: Optional[str] = None
    opened: bool = False
    policy_result: Optional[str] = None

    @property
    def pending(self) -> bool:
        return self.policy_result is None


@dataclass
class _ApprovalState:
    """Per-invocation approval observations; never carried between invocations."""

    eligible: bool = False
    session_id: Optional[str] = None
    turn_id: Optional[str] = None
    completed_while_pending: bool = False
    records: dict[tuple[str, str], _ApprovalObservation] = field(default_factory=dict)


def _nonempty_str(value: object) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _uuid7() -> str:
    """Create an RFC 9562 UUIDv7 without depending on Python 3.14's uuid.uuid7."""
    timestamp_ms = int(time.time_ns() // 1_000_000) & ((1 << 48) - 1)
    random_bits = int.from_bytes(os.urandom(10), "big") & ((1 << 74) - 1)
    value = timestamp_ms << 80
    value |= 0x7 << 76
    value |= (random_bits >> 62) << 64
    value |= 0b10 << 62
    value |= random_bits & ((1 << 62) - 1)
    return str(uuid.UUID(int=value))


class MuseClient(LLMClientBase):
    supports_retained_local_continuation = True

    """Run Muse Code while retaining Auto-Coder's ownership of Git state."""

    def __init__(self, backend_name: Optional[str] = None, use_noedit_options: bool = False) -> None:
        super().__init__()
        config = get_llm_config()
        self.config_backend = config.get_backend_config(backend_name or "muse")
        self.model_name = (self.config_backend and self.config_backend.model) or "muse-spark-1.3"
        self.use_noedit_options = use_noedit_options
        if use_noedit_options and self.config_backend and self.config_backend.options_for_noedit:
            self.options = self.config_backend.options_for_noedit
        else:
            self.options = (self.config_backend and self.config_backend.options) or []
        self.use_noedit_options = use_noedit_options or "--no-edit" in self.options
        self.options_for_noedit = (self.config_backend and self.config_backend.options_for_noedit) or []
        self.usage_markers = (self.config_backend and self.config_backend.usage_markers) or []
        self.timeout = (self.config_backend and self.config_backend.timeout) or 7200
        self._msp_stdout = bytearray()
        self._msp_stderr = bytearray()
        self._approvals = _ApprovalState()

        override = os.environ.get("AUTOCODER_MUSE_CLI")
        command = shlex.split(override) if override else ["muse"]
        try:
            result = subprocess.run(command + ["--version"], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Muse Code CLI is unavailable: {exc}") from exc
        if result.returncode != 0:
            raise RuntimeError("Muse Code CLI is installed but unusable; run 'muse --version' and verify your installation")

    @classmethod
    def _execution_cwd(cls) -> Path:
        return _execution_cwd()

    @classmethod
    def _git(cls, *args: str, cwd: Optional[Path] = None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(["git", *args], cwd=cwd or cls._execution_cwd(), capture_output=True, check=False)

    def _snapshot(self) -> _GitState:
        return self._snapshot_at(self._execution_cwd())

    def _snapshot_at(self, cwd: Path) -> _GitState:
        """Capture one explicitly bound worktree, independent of later context changes."""
        cwd = cwd.resolve()
        head = self._git("rev-parse", "HEAD", cwd=cwd)
        if head.returncode != 0:
            raise RuntimeError("Muse backend requires a Git repository with an existing HEAD")
        branch_result = self._git("symbolic-ref", "--quiet", "HEAD", cwd=cwd)
        git_dir_result = self._git("rev-parse", "--path-format=absolute", "--git-dir", cwd=cwd)
        status = self._git("status", "--porcelain=v2", "--untracked-files=all", cwd=cwd)
        index_state = self._git("ls-files", "--stage", "-z", cwd=cwd)
        staged_patch = self._git("diff", "--cached", "--binary", cwd=cwd)
        unstaged_patch = self._git("diff", "--binary", cwd=cwd)
        untracked = self._git("ls-files", "--others", "--exclude-standard", "-z", cwd=cwd)
        ignored = self._git("ls-files", "--others", "--ignored", "--exclude-standard", "-z", cwd=cwd)
        tracked = self._git("ls-files", "-z", cwd=cwd)
        required_results = (status, git_dir_result, index_state, staged_patch, unstaged_patch, untracked, ignored, tracked)
        if branch_result.returncode not in (0, 1) or any(result.returncode != 0 for result in required_results):
            raise RuntimeError("Unable to snapshot repository state before Muse execution")
        untracked_files = self._snapshot_files(untracked.stdout, cwd=cwd)
        ignored_files = self._snapshot_files(ignored.stdout, cwd=cwd)
        tracked_modes = self._snapshot_modes(tracked.stdout, cwd=cwd)
        directory_modes = self._snapshot_directory_modes(cwd=cwd)
        return _GitState(
            worktree=cwd.resolve(),
            git_dir=Path(os.fsdecode(git_dir_result.stdout).strip()).resolve(),
            branch=branch_result.stdout.decode().strip() if branch_result.returncode == 0 else None,
            head=head.stdout.decode().strip(),
            index_state=index_state.stdout,
            status=status.stdout,
            staged_patch=staged_patch.stdout,
            unstaged_patch=unstaged_patch.stdout,
            untracked_files=untracked_files,
            ignored_files=ignored_files,
            tracked_modes=tracked_modes,
            directory_modes=directory_modes,
        )

    @classmethod
    def _snapshot_files(cls, raw_paths: bytes, cwd: Optional[Path] = None) -> tuple[_WorkspaceFile, ...]:
        files = []
        target_cwd = cwd or cls._execution_cwd()
        for raw_path in filter(None, raw_paths.split(b"\0")):
            relative_path = os.fsdecode(raw_path)
            normalized = relative_path.replace(os.sep, "/")
            if any(normalized.startswith(prefix) or f"/{prefix}" in f"/{normalized}" for prefix in _DISPOSABLE_DIRECTORY_PREFIXES):
                continue
            path = target_cwd / relative_path
            if path.is_symlink():
                files.append(_WorkspaceFile(relative_path, os.fsencode(os.readlink(path)), True, stat.S_IMODE(path.lstat().st_mode)))
            elif path.is_file():
                files.append(_WorkspaceFile(relative_path, path.read_bytes(), False, stat.S_IMODE(path.stat().st_mode)))
        return tuple(files)

    @classmethod
    def _snapshot_modes(cls, raw_paths: bytes, cwd: Optional[Path] = None) -> tuple[_WorkspaceMode, ...]:
        modes = []
        target_cwd = cwd or cls._execution_cwd()
        for raw_path in filter(None, raw_paths.split(b"\0")):
            relative_path = os.fsdecode(raw_path)
            normalized = relative_path.replace(os.sep, "/")
            if any(normalized.startswith(prefix) or f"/{prefix}" in f"/{normalized}" for prefix in _DISPOSABLE_DIRECTORY_PREFIXES):
                continue
            path = target_cwd / relative_path
            if path.exists() and not path.is_symlink():
                modes.append(_WorkspaceMode(relative_path, stat.S_IMODE(path.stat().st_mode)))
        return tuple(modes)

    @classmethod
    def _snapshot_directory_modes(cls, cwd: Optional[Path] = None) -> tuple[_WorkspaceMode, ...]:
        root = cwd or cls._execution_cwd()
        modes = [_WorkspaceMode(".", stat.S_IMODE(root.stat().st_mode))]
        for current_root, directories, _files in os.walk(root, followlinks=False):
            directories[:] = sorted(directory for directory in directories if not (Path(current_root) == root and directory in _DISPOSABLE_DIRECTORY_NAMES))
            for directory in directories:
                path = Path(current_root) / directory
                if not path.is_symlink():
                    modes.append(_WorkspaceMode(str(path.relative_to(root)), stat.S_IMODE(path.stat().st_mode)))
        return tuple(modes)

    def _restore_lifecycle(self, state: _GitState) -> None:
        """Restore only HEAD and index state private to the captured worktree."""
        target_cwd = state.worktree
        current_git_dir = self._git("rev-parse", "--path-format=absolute", "--git-dir", cwd=target_cwd)
        if current_git_dir.returncode != 0 or Path(os.fsdecode(current_git_dir.stdout).strip()).resolve() != state.git_dir:
            raise RuntimeError(f"Muse recovery refused: original worktree unavailable worktree={target_cwd} git_dir={state.git_dir}")
        if state.branch:
            current = self._git("rev-parse", "--verify", state.branch, cwd=target_cwd)
            current_oid = os.fsdecode(current.stdout).strip() if current.returncode == 0 else "<missing>"
            if current_oid != state.head:
                raise RuntimeError("Muse recovery skipped unsafe shared-ref repair " f"worktree={target_cwd} ref={state.branch} before={state.head} current={current_oid}: " "invocation ownership and current-state authority are unavailable")
            restored = self._git("symbolic-ref", "HEAD", state.branch, cwd=target_cwd)
        else:
            restored = self._git("update-ref", "--no-deref", "HEAD", state.head, cwd=target_cwd)
        index = self._git("read-tree", state.head, cwd=target_cwd)
        if restored.returncode != 0 or index.returncode != 0:
            raise RuntimeError("Muse changed Git lifecycle state and Auto-Coder could not restore it")

    def _restore_index(self, state: _GitState) -> None:
        target_cwd = state.worktree
        if self._git("read-tree", state.head, cwd=target_cwd).returncode != 0:
            raise RuntimeError("Auto-Coder could not unstage Muse changes")
        if state.staged_patch:
            result = subprocess.run(["git", "apply", "--binary", "--cached"], cwd=target_cwd, input=state.staged_patch, capture_output=True)
            if result.returncode != 0:
                raise RuntimeError("Auto-Coder could not restore the pre-Muse index")

    def _restore_repository(self, state: _GitState) -> None:
        """Restore the exact tracked/index/untracked state captured for no-edit."""
        target_cwd = state.worktree
        self._restore_lifecycle(state)
        clean_args = ["clean", "-fdx"]
        for name in sorted(_DISPOSABLE_DIRECTORY_NAMES):
            clean_args.extend(["-e", f"{name}/", "-e", name])
        if self._git("checkout-index", "-a", "-f", cwd=target_cwd).returncode != 0 or self._git(*clean_args, cwd=target_cwd).returncode != 0:
            raise RuntimeError("Muse changed repository state and Auto-Coder could not restore it")
        for patch, cached in ((state.staged_patch, True), (state.unstaged_patch, False)):
            if not patch:
                continue
            args = ["git", "apply", "--binary"]
            if cached:
                args.append("--cached")
            result = subprocess.run(args, cwd=target_cwd, input=patch, capture_output=True)
            if result.returncode != 0:
                raise RuntimeError("Muse changed repository state and Auto-Coder could not restore its pre-run patch")
            if cached and self._git("checkout-index", "-a", "-f", cwd=target_cwd).returncode != 0:
                raise RuntimeError("Muse changed repository state and Auto-Coder could not restore its working tree")
        for workspace_file in state.untracked_files + state.ignored_files:
            path = target_cwd / workspace_file.path
            path.parent.mkdir(parents=True, exist_ok=True)
            if workspace_file.is_symlink:
                path.symlink_to(os.fsdecode(workspace_file.contents))
            else:
                path.write_bytes(workspace_file.contents)
                path.chmod(workspace_file.mode)
        for workspace_mode in state.tracked_modes:
            path = target_cwd / workspace_mode.path
            if path.exists() and not path.is_symlink():
                path.chmod(workspace_mode.mode)
        for directory_mode in state.directory_modes:
            path = target_cwd / directory_mode.path
            if not path.exists():
                path.mkdir(parents=True)
        for directory_mode in reversed(state.directory_modes):
            path = target_cwd / directory_mode.path
            if not path.is_symlink():
                path.chmod(directory_mode.mode)

    @staticmethod
    def _trace_contains_git_mutation(trace_path: str) -> bool:
        """Fail closed unless every traced Git command is observably read-only."""
        try:
            starts = {}
            with open(trace_path, encoding="utf-8") as trace_file:
                for line in trace_file:
                    event = json.loads(line)
                    if event.get("event") == "start":
                        starts[event.get("sid")] = event.get("argv")
                    elif event.get("event") == "cmd_name":
                        name = event.get("name")
                        if name not in _READ_ONLY_GIT_COMMANDS and not _read_only_special_git_command(name, starts.get(event.get("sid"))):
                            logger.warning("Git trace observed non-read-only command: name=%r argv=%r", name, starts.get(event.get("sid")))
                            return True
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Unable to audit Git commands executed by Muse: {exc}") from exc
        return False

    def _assert_invariants(self, before: _GitState, is_noedit: bool, mutation_observed: bool = False) -> None:
        after = self._snapshot_at(before.worktree)
        lifecycle_changed = (after.git_dir, after.branch, after.head) != (before.git_dir, before.branch, before.head)
        index_changed = after.index_state != before.index_state
        noedit_changed = is_noedit and (
            after.status != before.status or after.unstaged_patch != before.unstaged_patch or after.untracked_files != before.untracked_files or after.ignored_files != before.ignored_files or after.tracked_modes != before.tracked_modes or after.directory_modes != before.directory_modes
        )
        recovery_error: Optional[RuntimeError] = None
        try:
            if is_noedit and (lifecycle_changed or noedit_changed):
                self._restore_repository(before)
            elif lifecycle_changed:
                self._restore_lifecycle(before)
                self._restore_index(before)
            elif index_changed:
                self._restore_index(before)
        except RuntimeError as exc:
            recovery_error = exc
        if lifecycle_changed or index_changed or noedit_changed or mutation_observed:
            detail = "Git lifecycle or index" if lifecycle_changed or index_changed else "working tree"
            if mutation_observed:
                detail = "Git lifecycle command"
            logger.warning(
                "Muse Git-state invariant violated: detail={} lifecycle={} index={} noedit={} mutation_observed={}",
                detail,
                lifecycle_changed,
                index_changed,
                noedit_changed,
                mutation_observed,
            )
            failure = RuntimeError(f"Muse execution violated the Git-state invariant ({detail} changed)")
            if recovery_error is not None:
                failure.add_note(f"Recovery failed or was unsafe: {recovery_error}")
            raise failure

    @staticmethod
    def _path_is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True

    def _safe_prompt_directory(self) -> Path:
        """Select a temporary directory outside the worktree and Git metadata."""
        cwd = self._execution_cwd()
        repository = cwd.resolve()
        protected = [repository]
        for argument in ("--git-dir", "--git-common-dir"):
            result = self._git("rev-parse", "--path-format=absolute", argument, cwd=cwd)
            if result.returncode != 0:
                raise RuntimeError("Unable to locate Git metadata for Muse prompt isolation")
            meta_path = Path(os.fsdecode(result.stdout).strip()).resolve()
            protected.append(meta_path)
            if meta_path.name == ".git":
                protected.append(meta_path.parent)

        candidates = [Path(tempfile.gettempdir())]
        if os.name == "posix":
            candidates.append(Path("/tmp"))
        for candidate in candidates:
            resolved = candidate.resolve()
            if any(self._path_is_within(resolved, root) for root in protected):
                continue
            if resolved.is_dir() and os.access(resolved, os.W_OK | os.X_OK):
                return resolved
        raise RuntimeError("No safe temporary directory is available outside the repository for the Muse prompt file")

    def _create_prompt_file(self, prompt: str) -> Path:
        """Write one complete rendered prompt to an exclusively created private file."""
        path: Optional[Path] = None
        descriptor: Optional[int] = None
        try:
            descriptor, raw_path = tempfile.mkstemp(prefix="auto-coder-muse-prompt-", dir=self._safe_prompt_directory())
            path = Path(raw_path)
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as prompt_file:
                descriptor = None
                prompt_file.write(prompt.encode("utf-8"))
                prompt_file.flush()
                os.fsync(prompt_file.fileno())
            file_stat = path.stat()
            if not stat.S_ISREG(file_stat.st_mode) or (os.name == "posix" and stat.S_IMODE(file_stat.st_mode) != 0o600):
                raise RuntimeError("Muse prompt transport did not produce a private regular file")
            return path
        except BaseException as exc:
            cleanup_error: Optional[OSError] = None
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as close_exc:
                    cleanup_error = close_exc
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError as unlink_exc:
                    cleanup_error = cleanup_error or unlink_exc
            failure = RuntimeError(f"Unable to prepare the complete Muse prompt file: {exc}")
            if cleanup_error is not None:
                failure.add_note(f"Prompt file cleanup also failed: {cleanup_error}")
            raise failure from exc

    @staticmethod
    def _reject_competing_prompt_sources(arguments: list[str]) -> None:
        for argument in arguments:
            if argument in {"--prompt", "--prompt-file"} or argument.startswith(("--prompt=", "--prompt-file=")):
                raise RuntimeError("Muse options must not configure a prompt source; Auto-Coder owns --prompt-file")

    @classmethod
    def _is_usage_limit_exhausted(
        cls,
        stdout: str,
        stderr: str,
        returncode: int,
        usage_markers: Sequence[object],
    ) -> bool:
        """Check whether Muse exhausted its quota or hit a rate limit.

        When stdout is a Muse JSONL stream, usage markers and HTTP 429 checks are
        evaluated ONLY against explicit diagnostic/error events or stderr rather
        than raw stdout. This prevents false positives caused by user prompts, git
        diffs (e.g. documentation changes containing 'rate limit' or 'error: 429'),
        or tool outputs echoed in the JSONL stream.
        """
        combined_output = "\n".join(part for part in (stdout, stderr) if part).strip()
        if not combined_output:
            return False

        events: list[dict[str, object]] = []
        is_jsonl_stream = False
        for line in stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("muse:"):
                continue
            try:
                parsed = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(parsed, dict) and ("record_type" in parsed or "payload_type" in parsed):
                is_jsonl_stream = True
                events.append(parsed)

        if not is_jsonl_stream:
            return has_usage_marker_match(combined_output, usage_markers) or has_http_429_marker(combined_output)

        # In a JSONL stream, extract diagnostic/error events
        diagnostic_events: list[dict[str, object]] = []
        for event in events:
            payload_type = str(event.get("payload_type", ""))
            payload = event.get("payload")
            record_type = str(event.get("record_type", ""))

            is_diagnostic = False
            if record_type == "error" or payload_type in {"run.terminal.failed", "task.lifecycle.failed", "error"}:
                is_diagnostic = True
            elif isinstance(payload, dict):
                payload_kind = str(payload.get("kind", ""))
                event_obj = payload.get("event")
                if payload_kind == "run":
                    if isinstance(event_obj, dict):
                        event_kind = str(event_obj.get("kind", ""))
                        if event_kind in {"failed", "error"} or event_obj.get("terminal") == "failed" or "error" in event_obj:
                            is_diagnostic = True
                    if payload.get("terminal") == "failed" or "error" in payload:
                        is_diagnostic = True

            if is_diagnostic:
                diagnostic_events.append(event)

        diagnostic_parts = [stderr] if stderr else []
        diagnostic_parts.extend(json.dumps(ev, ensure_ascii=False) for ev in diagnostic_events)
        diagnostic_output = "\n".join(diagnostic_parts).strip()

        if diagnostic_output:
            return has_usage_marker_match(diagnostic_output, usage_markers) or has_http_429_marker(diagnostic_output)

        if returncode != 0 and not events:
            return has_usage_marker_match(combined_output, usage_markers) or has_http_429_marker(combined_output)

        return False

    def _msp_timeout(self) -> AutoCoderTimeoutError:
        return AutoCoderTimeoutError(f"Muse MSP invocation timed out after {self.timeout} seconds")

    def _msp_send(self, process: subprocess.Popen[bytes], frame: dict[str, object], deadline: float) -> None:
        if process.stdin is None:
            raise RuntimeError("Muse MSP host has no input stream")
        payload = memoryview((json.dumps(frame, separators=(",", ":")) + "\n").encode())
        stdin_fd = process.stdin.fileno()
        stderr_fd = process.stderr.fileno() if process.stderr is not None else None
        while payload:
            self._check_approval_expiry()
            remaining = self._wait_budget(deadline)
            if remaining <= 0:
                self._check_approval_expiry()
                if time.monotonic() >= deadline:
                    raise self._msp_timeout()
                continue
            reads = [stderr_fd] if stderr_fd is not None else []
            readable, writable, _ = select.select(reads, [stdin_fd], [], remaining)
            if stderr_fd is not None and stderr_fd in readable:
                self._drain_msp_stderr(stderr_fd)
            if stdin_fd not in writable:
                continue
            try:
                written = os.write(stdin_fd, payload)
            except BlockingIOError:
                continue
            if written <= 0:
                raise RuntimeError("Muse MSP host stopped accepting protocol input")
            payload = payload[written:]

    def _drain_msp_stderr(self, stderr_fd: int) -> None:
        try:
            chunk = os.read(stderr_fd, 65536)
        except BlockingIOError:
            return
        if chunk:
            self._msp_stderr.extend(chunk)
            if len(self._msp_stderr) > 8192:
                del self._msp_stderr[:-8192]

    def _raise_msp_failure(self, message: str, payload: object) -> None:
        diagnostic = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
        markers = self.usage_markers or ["rate limit", "usage limit", "quota exceeded"]
        if self._is_usage_limit_exhausted("", diagnostic, 1, markers):
            raise AutoCoderUsageLimitError(diagnostic or "Muse Code usage limit reached")
        raise RuntimeError(f"{message}: {diagnostic}")

    def _msp_wait(
        self,
        process: subprocess.Popen[bytes],
        request_id: int,
        deadline: float,
        notifications: list[dict[str, object]],
        *,
        clean_eof: bool = False,
    ) -> dict[str, object]:
        if process.stdout is None:
            raise RuntimeError("Muse MSP host has no output stream")
        stdout_fd = process.stdout.fileno()
        stderr_fd = process.stderr.fileno() if process.stderr is not None else None
        while True:
            self._check_approval_expiry()
            newline = self._msp_stdout.find(b"\n")
            if newline >= 0:
                raw = bytes(self._msp_stdout[:newline])
                del self._msp_stdout[: newline + 1]
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise RuntimeError("Muse MSP host emitted an invalid protocol frame") from exc
                if not isinstance(frame, dict):
                    raise RuntimeError("Muse MSP host emitted a non-object protocol frame")
                if "method" in frame:
                    method = frame["method"]
                    if not isinstance(method, str):
                        raise RuntimeError("Muse MSP host emitted a frame with a non-string method")
                    if "id" in frame:
                        # Server-originated RPC: its id namespace is independent of ours.
                        self._msp_handle_server_request(process, frame, method, deadline)
                    else:
                        self._msp_handle_notification(frame, method)
                        notifications.append(frame)
                        if request_id == -1:
                            return {}
                    continue
                response_id = frame.get("id")
                if "id" in frame and type(response_id) is int and response_id == request_id:
                    if "error" in frame:
                        self._raise_msp_failure("Muse MSP request failed", frame["error"])
                    result = frame.get("result")
                    if not isinstance(result, dict):
                        raise RuntimeError("Muse MSP response did not contain an object result")
                    return result
                continue

            remaining = self._wait_budget(deadline)
            if remaining <= 0:
                self._check_approval_expiry()
                if time.monotonic() >= deadline:
                    raise self._msp_timeout()
                continue
            reads = [stdout_fd]
            if stderr_fd is not None:
                reads.append(stderr_fd)
            readable, _, _ = select.select(reads, [], [], remaining)
            if not readable:
                if time.monotonic() >= deadline:
                    raise self._msp_timeout()
                continue
            if stderr_fd is not None and stderr_fd in readable:
                self._drain_msp_stderr(stderr_fd)
            if stdout_fd not in readable:
                continue
            try:
                chunk = os.read(stdout_fd, 65536)
            except BlockingIOError:
                continue
            if chunk:
                self._msp_stdout.extend(chunk)
                continue
            if clean_eof:
                raise _MspEndOfStream
            detail = self._msp_stderr.decode(errors="replace").strip()
            self._raise_msp_failure("Muse MSP host exited before completing the request", detail)

    def _fail_interactive_request(self, method: str, params: object) -> NoReturn:
        details = params if isinstance(params, dict) else {}
        # Preserve correlation, never raw commands, prompts, subjects, or choices.
        facts = {
            "method": method,
            "requested_approval_policy": "denyUnmatched" if self._approval_denial_requested else "hostDefault",
            "effective_approval_policy": self._observed_approval_mode or "unknown",
        }
        for field_name in ("sessionId", "turnId", "approvalId", "requestId"):
            value = details.get(field_name)
            if isinstance(value, str):
                facts[field_name] = value[:200]
        get_trace_collector().record_event(EventKind.STAGE_RESULT, "llm.muse-interactive-request", "local-backend", label="Muse interactive request blocked", outcome=Outcome.BLOCKED, facts=facts)
        logger.error("Muse unattended execution received an interactive request: {}", facts)
        raise RuntimeError(f"Muse MSP unattended execution cannot wait for interactive request {method}; configure required Muse permission rules and prepare dependencies before invocation")

    # --- Host-resolved approval observation (confirmed denyUnmatched only) ---

    def _pending_approvals(self) -> list[tuple[tuple[str, str], _ApprovalObservation]]:
        return [(key, record) for key, record in self._approvals.records.items() if record.pending]

    def _wait_budget(self, deadline: float) -> float:
        """Seconds until the overall deadline or the earliest pending approval deadline."""
        now = time.monotonic()
        limits = [deadline - now]
        limits.extend(record.first_seen + _MUSE_APPROVAL_SETTLEMENT_SECONDS - now for _, record in self._pending_approvals())
        return max(min(limits), 0.0) if len(limits) > 1 else limits[0]

    def _check_approval_expiry(self) -> None:
        now = time.monotonic()
        for (session_id, approval_id), record in self._pending_approvals():
            if now - record.first_seen >= _MUSE_APPROVAL_SETTLEMENT_SECONDS:
                params = {"sessionId": session_id, "approvalId": approval_id, "turnId": record.turn_id}
                self._fail_interactive_request("approval/settlement-expired", params)

    def _bind_admitted_turn(self, turn_id: str) -> None:
        """Check pre-acknowledgement approval traffic against the actual admitted turn."""
        state = self._approvals
        state.turn_id = turn_id
        for key, record in list(state.records.items()):
            if record.turn_id is None or record.turn_id == turn_id:
                continue
            if record.opened:
                self._fail_interactive_request("approval/incompatible-turn", {"sessionId": key[0], "approvalId": key[1], "turnId": record.turn_id})
            del state.records[key]

    def _assert_approvals_settled(self) -> None:
        state = self._approvals
        pending = self._pending_approvals()
        if pending or state.completed_while_pending:
            session_id, approval_id = pending[0][0] if pending else (state.session_id, None)
            self._fail_interactive_request("approval/unresolved-at-completion", {"sessionId": session_id, "approvalId": approval_id, "turnId": state.turn_id})

    @staticmethod
    def _valid_rpc_id(value: object) -> bool:
        return type(value) is int or (type(value) is str)

    def _msp_handle_server_request(self, process: subprocess.Popen[bytes], frame: dict[str, object], method: str, deadline: float) -> None:
        request_id = frame["id"]
        if not self._valid_rpc_id(request_id):
            raise RuntimeError("Muse MSP host emitted a server request with an invalid id")
        params = frame.get("params")
        if method == "approval/request":
            identity = self._approval_identity(params, require_turn=True)
            if identity is None:
                self._msp_send(process, {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "Invalid approval request identity"}}, deadline)
                self._fail_interactive_request(method, params)
            # The empty result is only a presentation receipt, never a decision.
            self._msp_send(process, {"jsonrpc": "2.0", "id": request_id, "result": {}}, deadline)
            if not self._approvals.eligible:
                self._fail_interactive_request(method, params)
            self._observe_approval_opening(method, params, identity)
            return
        self._msp_send(
            process,
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Auto-Coder does not support interactive MSP requests"}},
            deadline,
        )
        self._fail_interactive_request(method, params)

    def _msp_handle_notification(self, frame: dict[str, object], method: str) -> None:
        params = frame.get("params")
        state = self._approvals
        if method == "userInput/requested":
            self._fail_interactive_request(method, params)
        if method in {"approval/requested", "approval/updated"}:
            if not state.eligible:
                self._fail_interactive_request(method, params)
            if method == "approval/requested":
                identity = self._approval_identity(params, require_turn=True)
                if identity is None:
                    self._fail_interactive_request(method, params)
                self._observe_approval_opening(method, params, identity)
            else:
                self._observe_approval_update(method, params)
        elif method == "approval/resolved" and state.eligible:
            self._observe_approval_resolution(params)
        elif method == "turn/completed" and state.eligible and self._pending_approvals():
            state.completed_while_pending = True

    @staticmethod
    def _approval_identity(params: object, *, require_turn: bool) -> Optional[tuple[str, Optional[str], str]]:
        if not isinstance(params, dict):
            return None
        session_id = _nonempty_str(params.get("sessionId"))
        approval_id = _nonempty_str(params.get("approvalId"))
        turn_id = _nonempty_str(params.get("turnId"))
        if session_id is None or approval_id is None or (require_turn and turn_id is None):
            return None
        return session_id, turn_id, approval_id

    def _observe_approval_opening(self, method: str, params: object, identity: Optional[tuple[str, Optional[str], str]]) -> None:
        state = self._approvals
        assert identity is not None
        session_id, turn_id, approval_id = identity
        if session_id != state.session_id or (state.turn_id is not None and turn_id != state.turn_id):
            self._fail_interactive_request(method, params)
        record = state.records.get((session_id, approval_id))
        if record is None:
            state.records[(session_id, approval_id)] = _ApprovalObservation(first_seen=time.monotonic(), turn_id=turn_id, opened=True)
            return
        if record.turn_id is not None and record.turn_id != turn_id:
            self._fail_interactive_request(method, params)
        # Duplicates neither renew the age nor reopen a settled approval.
        record.turn_id = turn_id
        record.opened = True

    def _observe_approval_update(self, method: str, params: object) -> None:
        state = self._approvals
        identity = self._approval_identity(params, require_turn=False)
        if identity is None:
            self._fail_interactive_request(method, params)
        session_id, turn_id, approval_id = identity
        if session_id != state.session_id or (turn_id is not None and state.turn_id is not None and turn_id != state.turn_id):
            return
        # An update cannot establish turn identity or completion; it only starts the clock.
        state.records.setdefault((session_id, approval_id), _ApprovalObservation(first_seen=time.monotonic()))

    def _observe_approval_resolution(self, params: object) -> None:
        state = self._approvals
        identity = self._approval_identity(params, require_turn=True)
        policy_result = params.get("policyResult") if isinstance(params, dict) else None
        if identity is None or policy_result not in _APPROVAL_POLICY_RESULTS:
            return
        session_id, turn_id, approval_id = identity
        if session_id != state.session_id or (state.turn_id is not None and turn_id != state.turn_id):
            return
        record = state.records.get((session_id, approval_id))
        if record is None:
            state.records[(session_id, approval_id)] = _ApprovalObservation(first_seen=time.monotonic(), turn_id=turn_id, policy_result=str(policy_result))
            return
        if record.turn_id is not None and record.turn_id != turn_id:
            return
        if record.policy_result is not None and record.policy_result != policy_result:
            raise RuntimeError("Muse MSP host reported conflicting policy results for one approval")
        record.turn_id = turn_id
        record.policy_result = str(policy_result)

    @staticmethod
    def _session_metadata(result: dict[str, object]) -> dict[str, object]:
        session = result.get("session")
        if not isinstance(session, dict):
            raise RuntimeError("Muse MSP response omitted session metadata")
        return session

    def _msp_options(self, effective_noedit: bool) -> _MspOptions:
        processed = self.config_backend.replace_placeholders(model_name=self.model_name) if self.config_backend else {}
        configured = processed.get(
            "options_for_noedit" if effective_noedit and self.options_for_noedit else "options",
            self.options_for_noedit if effective_noedit and self.options_for_noedit else self.options,
        )
        arguments = [*configured, *self.consume_extra_args()]
        self._reject_competing_prompt_sources(arguments)
        host_arguments = ["serve"]
        reasoning: Optional[str] = None
        approval_denial = effective_noedit
        index = 0
        harmless = {"exec", "--json"}
        while index < len(arguments):
            argument = arguments[index]
            if argument in harmless:
                index += 1
                continue
            if argument == "--disable-approval":
                approval_denial = True
                index += 1
                continue
            if argument == "--no-edit":
                effective_noedit = True
                approval_denial = True
                for flag in ("--disable-write", "--disable-shell"):
                    if flag not in host_arguments:
                        host_arguments.append(flag)
                index += 1
                continue
            if argument in {"--model", "--reasoning-effort"}:
                if index + 1 >= len(arguments):
                    raise RuntimeError(f"Muse option {argument} requires a value")
                value = arguments[index + 1]
                if argument == "--model" and value != self.model_name:
                    raise RuntimeError("Muse configured model conflicts with the selected backend model")
                if argument == "--reasoning-effort":
                    reasoning = value
                index += 2
                continue
            if argument.startswith("--model="):
                if argument.split("=", 1)[1] != self.model_name:
                    raise RuntimeError("Muse configured model conflicts with the selected backend model")
                index += 1
                continue
            if argument.startswith("--reasoning-effort="):
                reasoning = argument.split("=", 1)[1]
                index += 1
                continue
            if argument in {"--disable-write", "--disable-shell"}:
                host_arguments.append(argument)
                index += 1
                continue
            if argument == "--trust-workspace":
                raise RuntimeError("Muse workspace trust was requested without independent PR-review authorization")
            raise RuntimeError(f"Muse option is not representable through MSP: {argument}")
        if reasoning is not None and reasoning not in _MUSE_REASONING_EFFORTS:
            raise RuntimeError(f"Muse reasoning effort is not supported by MSP: {reasoning}")
        if effective_noedit:
            for flag in ("--disable-write", "--disable-shell"):
                if flag not in host_arguments:
                    host_arguments.append(flag)
        return _MspOptions(host_arguments, reasoning, effective_noedit, approval_denial)

    @staticmethod
    def _validate_command_ack(result: dict[str, object], command_id: str, *, approval_change: bool = False) -> None:
        if result.get("commandId") != command_id or result.get("status") != "accepted":
            raise RuntimeError("Muse MSP returned an invalid or uncorrelated command acknowledgement")
        if approval_change:
            if result.get("applyOutcome") not in {"completed", "noop"}:
                raise RuntimeError("Muse MSP did not apply approval denial")
            effective_mode = result.get("effectiveMode")
            if not isinstance(effective_mode, dict) or effective_mode.get("mode") != "denyUnmatched":
                raise RuntimeError("Muse MSP did not confirm effective approval denial")

    @staticmethod
    def _process_group_exists(process_group: int) -> bool:
        """Return whether any writer remains in an owned POSIX process group."""
        proc = Path("/proc")
        if proc.is_dir():
            try:
                for entry in proc.iterdir():
                    if not entry.name.isdigit():
                        continue
                    raw = (entry / "stat").read_text()
                    fields = raw[raw.rfind(")") + 2 :].split()
                    if len(fields) > 2 and int(fields[2]) == process_group and fields[0] != "Z":
                        return True
                return False
            except (OSError, ValueError):
                pass
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return False
        except PermissionError as exc:
            raise RuntimeError("Muse writer settlement could not inspect its process group") from exc
        return True

    @classmethod
    def _settle_process_group(cls, process: subprocess.Popen[bytes]) -> None:
        """Stop every process in the invocation-owned session before handoff."""
        if os.name != "posix":
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            return

        process_group = process.pid
        if not cls._process_group_exists(process_group):
            return
        os.killpg(process_group, signal.SIGTERM)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            process.poll()
            if not cls._process_group_exists(process_group):
                return
            time.sleep(0.01)
        os.killpg(process_group, signal.SIGKILL)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            process.poll()
            if not cls._process_group_exists(process_group):
                return
            time.sleep(0.01)
        raise RuntimeError("Muse process group did not terminate after SIGKILL; writer settlement is unknown")

    def _run_msp_turn(self, prompt: str, is_noedit: bool, session_id: Optional[str]) -> str:
        # An invocation owns only the identity it establishes successfully.
        # Clear before snapshot/configuration/rendering so any pre-host failure
        # cannot expose a previous invocation's session as its own result.
        self._last_session_id = None
        effective_noedit = is_noedit or self.use_noedit_options
        cwd = self._execution_cwd().resolve()
        msp_options = self._msp_options(effective_noedit)
        effective_noedit = msp_options.noedit
        self._approval_denial_requested = msp_options.approval_denial
        self._observed_approval_mode: Optional[str] = None
        self._approvals = _ApprovalState()
        boundary = get_current_local_execution_boundary()
        if not effective_noedit:
            if boundary is None:
                raise RuntimeError("Editable Muse execution requires a controller-owned local workspace binding")
            if not boundary.editable or boundary.backend_type.lower() != "muse" or boundary.binding.workspace.resolve() != cwd:
                raise RuntimeError("Editable Muse execution has an incompatible local workspace binding")
        before = self._snapshot_at(cwd)
        rendered_prompt = render_prompt(
            "muse.execution",
            task_prompt=prompt,
            mode="no-edit" if effective_noedit else "edit",
            result_root=str(cwd),
        )
        env = os.environ.copy()
        if self.config_backend and self.config_backend.api_key and "MUSE_API_KEY" not in env:
            env["MUSE_API_KEY"] = self.config_backend.api_key
        trace_path: Optional[str] = None
        if effective_noedit:
            trace = tempfile.NamedTemporaryFile(prefix="auto-coder-muse-git-trace-", delete=False)
            trace_path = trace.name
            trace.close()
            env["GIT_TRACE2_EVENT"] = trace_path
        command = shlex.split(os.environ.get("AUTOCODER_MUSE_CLI", "muse")) + msp_options.host_arguments
        process: Optional[subprocess.Popen[bytes]] = None
        deadline = time.monotonic() + self.timeout
        notifications: list[dict[str, object]] = []
        command_ids: set[str] = set()

        def new_command_id() -> str:
            command_id = _uuid7()
            while command_id in command_ids:
                command_id = _uuid7()
            command_ids.add(command_id)
            return command_id

        completed_session_id: Optional[str] = None
        final_output: Optional[str] = None
        invocation_error: Optional[BaseException] = None
        try:
            logger.warning("LLM invocation: Muse Code MSP host is being called. Keep LLM calls minimized.")
            process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=os.name == "posix", bufsize=0)
            self._msp_stdout.clear()
            self._msp_stderr.clear()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    os.set_blocking(stream.fileno(), False)
            self._msp_send(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"clientInfo": {"name": _MUSE_MSP_CLIENT_NAME, "version": "1"}},
                },
                deadline,
            )
            initialized = self._msp_wait(process, 1, deadline, notifications)
            server_info = initialized.get("serverInfo")
            schema = initialized.get("schema")
            host_version = server_info.get("version") if isinstance(server_info, dict) else None
            if not isinstance(schema, dict):
                raise RuntimeError("Muse MSP initialization returned missing or non-object schema metadata " f"(host version={host_version!r}; required schema.version=1 and a nonblank schema.fingerprint)")
            schema_version = schema.get("version")
            schema_fingerprint = schema.get("fingerprint")
            observed_schema = (schema_version, schema_fingerprint)
            if type(schema_version) is not int or schema_version != 1:
                raise RuntimeError("Muse MSP initialization has unsupported schema.version " f"(host version={host_version!r}; observed version={schema_version!r}; required integer 1)")
            if not isinstance(schema_fingerprint, str) or not schema_fingerprint.strip():
                raise RuntimeError("Muse MSP initialization has invalid schema.fingerprint " f"(host version={host_version!r}; observed fingerprint={schema_fingerprint!r}; required nonblank string)")
            if observed_schema not in _MUSE_MSP_DIAGNOSTIC_SCHEMAS:
                logger.warning(
                    "Muse MSP schema fingerprint is not in Auto-Coder's diagnostic reference set; "
                    "execution continues with required runtime contract checks "
                    f"(schema version={schema_version!r}, fingerprint={schema_fingerprint!r}, host version={host_version!r}). "
                    "This does not certify host compatibility."
                )
            self._msp_send(process, {"jsonrpc": "2.0", "method": "initialized"}, deadline)
            session_command_id = new_command_id()
            if session_id is None:
                params: dict[str, object] = {"commandId": session_command_id, "workspaceRoot": str(cwd), "modelId": self.model_name}
                if msp_options.approval_denial:
                    params["approvalMode"] = "denyUnmatched"
                method = "session/start"
            else:
                if not session_id.strip():
                    raise ValueError("Muse session ID must be nonempty")
                params = {"commandId": session_command_id, "sessionId": session_id}
                method = "session/resume"
            self._msp_send(process, {"jsonrpc": "2.0", "id": 2, "method": method, "params": params}, deadline)
            opened = self._msp_wait(process, 2, deadline, notifications)
            metadata = self._session_metadata(opened)
            canonical_id = metadata.get("sessionId")
            if not isinstance(canonical_id, str) or not canonical_id:
                raise RuntimeError("Muse MSP returned an invalid session identity")
            if session_id is not None and canonical_id != session_id:
                raise RuntimeError("Muse MSP resumed a different session identity")
            workspace = metadata.get("workspaceRoot")
            if not isinstance(workspace, str) or workspace != str(cwd):
                raise RuntimeError("Muse MSP session belongs to an incompatible workspace")
            effective_model = metadata.get("modelId")
            model_matches = effective_model == self.model_name
            resolved_model = _MUSE_141_MODEL_ALIASES.get(self.model_name)
            model_matches = model_matches or (resolved_model is not None and effective_model == resolved_model)
            if not model_matches:
                raise RuntimeError("Muse MSP session omitted or uses an incompatible model")
            approval_mode = metadata.get("approvalMode")
            observed_mode = approval_mode.get("mode") if isinstance(approval_mode, dict) else None
            if isinstance(observed_mode, str):
                self._observed_approval_mode = observed_mode
            pending = opened.get("pendingRequests")
            if pending not in (None, []):
                get_trace_collector().record_event(
                    EventKind.STAGE_RESULT,
                    "llm.muse-interactive-request",
                    "local-backend",
                    label="Muse interactive request blocked",
                    outcome=Outcome.BLOCKED,
                    facts={
                        "method": method,
                        "requested_approval_policy": "denyUnmatched" if self._approval_denial_requested else "hostDefault",
                        "effective_approval_policy": self._observed_approval_mode or "unknown",
                        "sessionId": canonical_id,
                        "reason": "pending interactive requests",
                    },
                )
                raise RuntimeError("Muse MSP session has pending interactive requests")
            denial_confirmed = observed_mode == "denyUnmatched"
            if session_id is None:
                if msp_options.approval_denial and not denial_confirmed:
                    raise RuntimeError("Muse MSP fresh session did not confirm approval denial")
            elif msp_options.approval_denial and not denial_confirmed:
                approval_command_id = new_command_id()
                approval_params: dict[str, object] = {
                    "commandId": approval_command_id,
                    "sessionId": canonical_id,
                    "mode": "denyUnmatched",
                }
                self._msp_send(process, {"jsonrpc": "2.0", "id": 3, "method": "session/setApprovalMode", "params": approval_params}, deadline)
                approval_result = self._msp_wait(process, 3, deadline, notifications)
                self._validate_command_ack(approval_result, approval_command_id, approval_change=True)
                self._observed_approval_mode = "denyUnmatched"
            command_id = new_command_id()
            # Automatic settlement is eligible only after denyUnmatched is confirmed for this session.
            self._approvals = _ApprovalState(eligible=self._observed_approval_mode == "denyUnmatched", session_id=canonical_id)
            turn_params: dict[str, object] = {"commandId": command_id, "sessionId": canonical_id, "input": [{"type": "text", "text": rendered_prompt}]}
            if msp_options.reasoning_effort is not None:
                turn_params["reasoningEffort"] = msp_options.reasoning_effort
            turn_request_id = 4
            self._msp_send(process, {"jsonrpc": "2.0", "id": turn_request_id, "method": "turn/start", "params": turn_params}, deadline)
            turn_ack = self._msp_wait(process, turn_request_id, deadline, notifications)
            self._validate_command_ack(turn_ack, command_id)
            turn_id = turn_ack.get("turnId")
            if not isinstance(turn_id, str) or not turn_id or type(turn_ack.get("startedNewTurn")) is not bool or not isinstance(turn_ack.get("disposition"), str) or not turn_ack.get("disposition"):
                raise RuntimeError("Muse MSP did not acknowledge the submitted turn")
            self._bind_admitted_turn(turn_id)
            terminal: Optional[dict[str, object]] = None
            notification_cursor = 0
            while terminal is None:
                if notification_cursor == len(notifications):
                    # Wait for a deliberately unused response id while collecting notifications.
                    self._msp_wait(process, -1, deadline, notifications)
                for event in notifications[notification_cursor:]:
                    notification_cursor += 1
                    params_obj = event.get("params")
                    if event.get("method") != "turn/completed":
                        continue
                    if not isinstance(params_obj, dict) or params_obj.get("turnId") != turn_id:
                        raise RuntimeError("Muse MSP terminal belongs to an incompatible turn")
                    if params_obj.get("sessionId") != canonical_id:
                        raise RuntimeError("Muse MSP terminal belongs to an incompatible session")
                    if terminal is None:
                        terminal = params_obj
            if terminal.get("terminal") != "completed":
                self._raise_msp_failure("Muse MSP turn did not complete successfully", terminal)
            # A resolution arriving after completion cannot rescue an unresolved approval.
            self._assert_approvals_settled()
            # A single-turn host remains open waiting for more input. Close our
            # input after completion, then consume every remaining notification
            # through host EOF so a later contradictory terminal cannot escape
            # correlation checks merely by arriving after the first terminal.
            if process.stdin is not None:
                process.stdin.close()
            try:
                while process.poll() is None:
                    poll_deadline = min(deadline, time.monotonic() + 0.05)
                    try:
                        self._msp_wait(process, -1, poll_deadline, notifications, clean_eof=True)
                    except AutoCoderTimeoutError:
                        if time.monotonic() >= deadline:
                            raise
            except _MspEndOfStream:
                pass
            for event in notifications:
                params_obj = event.get("params")
                if event.get("method") != "turn/completed":
                    continue
                if not isinstance(params_obj, dict) or params_obj.get("turnId") != turn_id:
                    raise RuntimeError("Muse MSP terminal belongs to an incompatible turn")
                if params_obj.get("sessionId") != canonical_id:
                    raise RuntimeError("Muse MSP terminal belongs to an incompatible session")
            self._assert_approvals_settled()
            answers: list[str] = []
            for event in notifications:
                params_obj = event.get("params")
                if event.get("method") != "item/completed" or not isinstance(params_obj, dict):
                    continue
                if params_obj.get("sessionId") != canonical_id:
                    raise RuntimeError("Muse MSP assistant item belongs to an incompatible session")
                item = params_obj.get("item")
                if isinstance(item, dict) and item.get("turnId") == turn_id and item.get("kind") in {"message", "agentMessage"} and item.get("status") == "completed" and (item.get("kind") == "agentMessage" or item.get("role") == "assistant") and isinstance(item.get("text"), str):
                    answers.append(str(item["text"]))
            if not answers:
                raise RuntimeError("Muse MSP turn completed without final assistant text")
            completed_session_id = canonical_id
            final_output = answers[-1]
        except subprocess.TimeoutExpired as exc:
            invocation_error = AutoCoderTimeoutError(f"Muse MSP invocation timed out after {self.timeout} seconds")
            invocation_error.__cause__ = exc
        except BaseException as exc:
            invocation_error = exc
        finally:
            if process is not None:
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
                if invocation_error is None:
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                try:
                    self._settle_process_group(process)
                    if process.poll() is None:
                        process.wait(timeout=2)
                except BaseException as settlement_error:
                    if invocation_error is None:
                        invocation_error = settlement_error
                    else:
                        invocation_error.add_note(f"Muse writer settlement also failed: {settlement_error}")
                if process.returncode != 0 and invocation_error is None:
                    invocation_error = RuntimeError(f"Muse MSP host exited with nonzero status {process.returncode}")
            try:
                # Editable work executes in the controller-owned private repository.
                # Its Git index, HEAD, refs, branches, stashes, and worktrees are
                # implementation state and must survive for generation handoff.
                # Only no-edit turns retain the mutation audit and exact snapshot
                # invariant.
                if effective_noedit:
                    assert trace_path is not None
                    mutation_observed = self._trace_contains_git_mutation(trace_path)
                    self._assert_invariants(before, True, mutation_observed)
            except BaseException as invariant_error:
                self._last_session_id = None
                if invocation_error is None:
                    invocation_error = invariant_error
                else:
                    invocation_error.add_note(f"Muse repository invariant check also failed: {invariant_error}")
            finally:
                if trace_path is not None:
                    os.unlink(trace_path)

        if invocation_error is not None:
            self._last_session_id = None
            raise invocation_error.with_traceback(invocation_error.__traceback__)
        if completed_session_id is None or final_output is None:
            self._last_session_id = None
            raise RuntimeError("Muse MSP invocation ended without a completed result")
        self._last_session_id = completed_session_id
        if boundary is not None:
            boundary.record_writer_completion(boundary.binding.invocation_id)
            boundary.record_violation_observation(boundary.binding.invocation_id)
        return final_output

    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        return self._run_msp_turn(prompt, is_noedit, None)

    def continue_session(self, session_id: str, prompt: str, is_noedit: bool = False) -> str:
        return self._run_msp_turn(prompt, is_noedit, session_id)

    def get_last_session_id(self) -> Optional[str]:
        return self._last_session_id

    def check_mcp_server_configured(self, server_name: str) -> bool:
        return False

    def add_mcp_server_config(self, server_name: str, command: str, args: list[str]) -> bool:
        return False
