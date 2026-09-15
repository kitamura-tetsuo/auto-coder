"""Non-interactive Muse Code CLI client with Git lifecycle enforcement."""

from __future__ import annotations

import ctypes
import json
import os
import shlex
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .exceptions import AutoCoderTimeoutError, AutoCoderUsageLimitError
from .llm_backend_config import get_llm_config
from .llm_client_base import LLMClientBase
from .logger_config import get_logger
from .prompt_loader import render_prompt
from .usage_marker_utils import has_usage_marker_match
from .utils import _COMMAND_EXECUTION_CWD

logger = get_logger(__name__)

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
    branch: Optional[str]
    head: str
    status: bytes
    staged_patch: bytes
    unstaged_patch: bytes
    untracked_files: tuple[_WorkspaceFile, ...]
    ignored_files: tuple[_WorkspaceFile, ...]
    tracked_modes: tuple[_WorkspaceMode, ...]
    directory_modes: tuple[_WorkspaceMode, ...]
    refs: tuple[tuple[str, str], ...]


class MuseClient(LLMClientBase):
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
        self.options_for_noedit = (self.config_backend and self.config_backend.options_for_noedit) or []
        self.usage_markers = (self.config_backend and self.config_backend.usage_markers) or []
        self.timeout = (self.config_backend and self.config_backend.timeout) or 7200

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
        cwd = self._execution_cwd()
        head = self._git("rev-parse", "HEAD", cwd=cwd)
        if head.returncode != 0:
            raise RuntimeError("Muse backend requires a Git repository with an existing HEAD")
        branch_result = self._git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=cwd)
        status = self._git("status", "--porcelain=v2", "--untracked-files=all", cwd=cwd)
        if status.returncode != 0:
            raise RuntimeError("Unable to snapshot repository state before Muse execution")
        untracked_files = self._snapshot_files(self._git("ls-files", "--others", "--exclude-standard", "-z", cwd=cwd).stdout, cwd=cwd)
        ignored_files = self._snapshot_files(self._git("ls-files", "--others", "--ignored", "--exclude-standard", "-z", cwd=cwd).stdout, cwd=cwd)
        tracked_modes = self._snapshot_modes(self._git("ls-files", "-z", cwd=cwd).stdout, cwd=cwd)
        directory_modes = self._snapshot_directory_modes(cwd=cwd)
        refs = self._snapshot_refs(cwd=cwd)
        return _GitState(
            branch=branch_result.stdout.decode().strip() if branch_result.returncode == 0 else None,
            head=head.stdout.decode().strip(),
            status=status.stdout,
            staged_patch=self._git("diff", "--cached", "--binary", cwd=cwd).stdout,
            unstaged_patch=self._git("diff", "--binary", cwd=cwd).stdout,
            untracked_files=untracked_files,
            ignored_files=ignored_files,
            tracked_modes=tracked_modes,
            directory_modes=directory_modes,
            refs=refs,
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

    def _snapshot_refs(self, cwd: Optional[Path] = None) -> tuple[tuple[str, str], ...]:
        result = self._git("for-each-ref", "--format=%(refname) %(objectname)", cwd=cwd)
        if result.returncode != 0:
            raise RuntimeError("Unable to snapshot Git refs for Muse execution")
        refs = []
        for line in result.stdout.splitlines():
            ref_name, object_name = os.fsdecode(line).split(" ", 1)
            refs.append((ref_name, object_name))
        return tuple(refs)

    def _restore_refs(self, state: _GitState, cwd: Optional[Path] = None) -> None:
        target_cwd = cwd or self._execution_cwd()
        expected = dict(state.refs)
        current = dict(self._snapshot_refs(cwd=target_cwd))
        for ref_name in current.keys() - expected.keys():
            if self._git("update-ref", "-d", ref_name, cwd=target_cwd).returncode != 0:
                raise RuntimeError(f"Auto-Coder could not remove Muse-created ref {ref_name}")
        for ref_name, object_name in expected.items():
            if current.get(ref_name) != object_name and self._git("update-ref", ref_name, object_name, cwd=target_cwd).returncode != 0:
                raise RuntimeError(f"Auto-Coder could not restore Muse-modified ref {ref_name}")

    def _restore_lifecycle(self, state: _GitState, cwd: Optional[Path] = None) -> None:
        target_cwd = cwd or self._execution_cwd()
        if state.branch:
            restored = self._git("checkout", "-f", state.branch, cwd=target_cwd)
            if restored.returncode != 0:
                restored = self._git("checkout", "-B", state.branch, state.head, cwd=target_cwd)
        else:
            restored = self._git("update-ref", "--no-deref", "HEAD", state.head, cwd=target_cwd)
        reset = self._git("reset", "--mixed", state.head, cwd=target_cwd)
        if restored.returncode != 0 or reset.returncode != 0:
            raise RuntimeError("Muse changed Git lifecycle state and Auto-Coder could not restore it")
        self._restore_refs(state, cwd=target_cwd)

    def _restore_index(self, state: _GitState, cwd: Optional[Path] = None) -> None:
        target_cwd = cwd or self._execution_cwd()
        if self._git("reset", "--mixed", state.head, cwd=target_cwd).returncode != 0:
            raise RuntimeError("Auto-Coder could not unstage Muse changes")
        if state.staged_patch:
            result = subprocess.run(["git", "apply", "--binary", "--cached"], cwd=target_cwd, input=state.staged_patch, capture_output=True)
            if result.returncode != 0:
                raise RuntimeError("Auto-Coder could not restore the pre-Muse index")

    def _restore_repository(self, state: _GitState, cwd: Optional[Path] = None) -> None:
        """Restore the exact tracked/index/untracked state captured for no-edit."""
        target_cwd = cwd or self._execution_cwd()
        self._restore_lifecycle(state, cwd=target_cwd)
        clean_args = ["clean", "-fdx"]
        for name in sorted(_DISPOSABLE_DIRECTORY_NAMES):
            clean_args.extend(["-e", f"{name}/", "-e", name])
        if self._git("reset", "--hard", state.head, cwd=target_cwd).returncode != 0 or self._git(*clean_args, cwd=target_cwd).returncode != 0:
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
        after = self._snapshot()
        lifecycle_changed = (after.branch, after.head) != (before.branch, before.head)
        refs_changed = after.refs != before.refs
        index_changed = after.staged_patch != before.staged_patch
        noedit_changed = is_noedit and (
            after.status != before.status or after.unstaged_patch != before.unstaged_patch or after.untracked_files != before.untracked_files or after.ignored_files != before.ignored_files or after.tracked_modes != before.tracked_modes or after.directory_modes != before.directory_modes
        )
        if is_noedit and (lifecycle_changed or noedit_changed):
            self._restore_repository(before)
        elif lifecycle_changed or refs_changed:
            self._restore_lifecycle(before)
            self._restore_index(before)
        elif index_changed:
            self._restore_index(before)
        if lifecycle_changed or refs_changed or index_changed or noedit_changed or mutation_observed:
            detail = "Git lifecycle or index" if lifecycle_changed or refs_changed or index_changed else "working tree"
            if mutation_observed:
                detail = "Git lifecycle command"
            logger.warning(
                "Muse Git-state invariant violated: detail={} lifecycle={} refs={} index={} noedit={} mutation_observed={}",
                detail,
                lifecycle_changed,
                refs_changed,
                index_changed,
                noedit_changed,
                mutation_observed,
            )
            raise RuntimeError(f"Muse execution violated the Git-state invariant ({detail} changed)")

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

    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        cwd = self._execution_cwd()
        before = self._snapshot()
        effective_noedit = is_noedit or self.use_noedit_options
        processed = self.config_backend.replace_placeholders(model_name=self.model_name) if self.config_backend else {}
        options = processed.get("options_for_noedit" if effective_noedit and self.options_for_noedit else "options", self.options_for_noedit if effective_noedit and self.options_for_noedit else self.options)
        command = shlex.split(os.environ.get("AUTOCODER_MUSE_CLI", "muse"))
        raw_arguments = [*options, *self.consume_extra_args()]
        self._reject_competing_prompt_sources(raw_arguments)
        invocation_arguments = [arg for arg in raw_arguments if arg != "exec"]
        if effective_noedit:
            invocation_arguments = [arg for arg in invocation_arguments if arg not in {"--yolo", "--disable-sandbox"}]
            for required_flag in ("--disable-write", "--disable-shell", "--disable-approval"):
                if required_flag not in invocation_arguments:
                    invocation_arguments.append(required_flag)
        rendered_prompt = render_prompt("muse.execution", task_prompt=prompt, mode="no-edit" if effective_noedit else "edit")
        env = os.environ.copy()
        if self.config_backend and self.config_backend.api_key and "MUSE_API_KEY" not in env:
            env["MUSE_API_KEY"] = self.config_backend.api_key

        trace_file = tempfile.NamedTemporaryFile(prefix="auto-coder-muse-git-trace-", delete=False)
        trace_path = trace_file.name
        trace_file.close()
        env["GIT_TRACE2_EVENT"] = trace_path
        try:
            prompt_path = self._create_prompt_file(rendered_prompt)
        except BaseException:
            try:
                os.unlink(trace_path)
            except OSError as cleanup_exc:
                logger.error("Unable to remove Muse Git trace file %s: %s", trace_path, cleanup_exc)
            raise
        command.extend(["exec", *invocation_arguments, "--prompt-file", str(prompt_path)])

        try:
            logger.warning("LLM invocation: Muse Code CLI is being called. Keep LLM calls minimized.")
            logger.info("Running Muse Code in non-interactive %s mode", "no-edit" if effective_noedit else "edit")
            try:
                result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=self.timeout, env=env)
            except subprocess.TimeoutExpired as exc:
                mutation_observed = self._trace_contains_git_mutation(trace_path)
                self._assert_invariants(before, effective_noedit, mutation_observed)
                raise AutoCoderTimeoutError(f"Muse Code CLI timed out after {self.timeout} seconds") from exc
            except OSError as exc:
                raise RuntimeError(f"Muse Code CLI could not be executed: {exc}") from exc

            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            combined_output = "\n".join(part for part in (stdout, stderr) if part).strip()
            mutation_observed = self._trace_contains_git_mutation(trace_path)
            self._assert_invariants(before, effective_noedit, mutation_observed)
            markers = self.usage_markers or ["rate limit", "usage limit", "quota exceeded", "429"]
            if has_usage_marker_match(combined_output, markers):
                raise AutoCoderUsageLimitError(combined_output or "Muse Code usage limit reached")
            if result.returncode != 0:
                from .adversarial_validator import _extract_muse_jsonl_result

                jsonl_detected, _, jsonl_error = _extract_muse_jsonl_result(stdout)
                if jsonl_detected and jsonl_error:
                    raise RuntimeError(f"Muse Code CLI failed with return code {result.returncode}: {jsonl_error}")
                raise RuntimeError(f"Muse Code CLI failed with return code {result.returncode}\n{combined_output}")

            from .adversarial_validator import _extract_muse_jsonl_result

            jsonl_detected, extracted_text, jsonl_error = _extract_muse_jsonl_result(stdout)
            if jsonl_detected:
                if jsonl_error:
                    raise RuntimeError(f"Muse Code CLI event stream error: {jsonl_error}")
                final_output = extracted_text if extracted_text is not None else ""
            else:
                final_output = stdout or stderr
        except BaseException as exc:
            try:
                prompt_path.unlink()
            except OSError as cleanup_exc:
                logger.error("Unable to remove Muse prompt file %s: %s", prompt_path, cleanup_exc)
                exc.add_note(f"Muse prompt file cleanup failed: {cleanup_exc}")
            raise
        else:
            try:
                prompt_path.unlink()
            except OSError as cleanup_exc:
                logger.error("Unable to remove Muse prompt file %s: %s", prompt_path, cleanup_exc)
                raise RuntimeError(f"Muse completed but its prompt file could not be removed: {cleanup_exc}") from cleanup_exc
            return final_output
        finally:
            try:
                os.unlink(trace_path)
            except OSError as exc:
                logger.error("Unable to remove Muse Git trace file %s: %s", trace_path, exc)

    def check_mcp_server_configured(self, server_name: str) -> bool:
        return False

    def add_mcp_server_config(self, server_name: str, command: str, args: list[str]) -> bool:
        return False
