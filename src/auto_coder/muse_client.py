"""Non-interactive Muse Code CLI client with Git lifecycle enforcement."""

from __future__ import annotations

import json
import os
import shlex
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

logger = get_logger(__name__)

_MUTATING_GIT_COMMANDS = {
    "add",
    "am",
    "checkout",
    "cherry-pick",
    "clean",
    "commit",
    "fetch",
    "merge",
    "mv",
    "pull",
    "push",
    "rebase",
    "reset",
    "restore",
    "revert",
    "rm",
    "stash",
    "switch",
    "update-ref",
}


@dataclass(frozen=True)
class _GitState:
    branch: Optional[str]
    head: str
    status: bytes
    staged_patch: bytes
    unstaged_patch: bytes
    untracked_files: tuple[tuple[str, bytes, bool], ...]
    ignored_files: tuple[tuple[str, bytes, bool], ...]
    refs: tuple[tuple[str, str], ...]


class MuseClient(LLMClientBase):
    """Run Muse Code while retaining Auto-Coder's ownership of Git state."""

    def __init__(self, backend_name: Optional[str] = None) -> None:
        super().__init__()
        config = get_llm_config()
        self.config_backend = config.get_backend_config(backend_name or "muse")
        self.model_name = (self.config_backend and self.config_backend.model) or "muse-spark-1.3"
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

    @staticmethod
    def _git(*args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(["git", *args], cwd=Path.cwd(), capture_output=True, check=False)

    def _snapshot(self) -> _GitState:
        head = self._git("rev-parse", "HEAD")
        if head.returncode != 0:
            raise RuntimeError("Muse backend requires a Git repository with an existing HEAD")
        branch_result = self._git("symbolic-ref", "--quiet", "--short", "HEAD")
        status = self._git("status", "--porcelain=v2", "--untracked-files=all")
        if status.returncode != 0:
            raise RuntimeError("Unable to snapshot repository state before Muse execution")
        untracked_files = self._snapshot_files(self._git("ls-files", "--others", "--exclude-standard", "-z").stdout)
        ignored_files = self._snapshot_files(self._git("ls-files", "--others", "--ignored", "--exclude-standard", "-z").stdout)
        refs = self._snapshot_refs()
        return _GitState(
            branch=branch_result.stdout.decode().strip() if branch_result.returncode == 0 else None,
            head=head.stdout.decode().strip(),
            status=status.stdout,
            staged_patch=self._git("diff", "--cached", "--binary").stdout,
            unstaged_patch=self._git("diff", "--binary").stdout,
            untracked_files=untracked_files,
            ignored_files=ignored_files,
            refs=refs,
        )

    @staticmethod
    def _snapshot_files(raw_paths: bytes) -> tuple[tuple[str, bytes, bool], ...]:
        files = []
        for raw_path in filter(None, raw_paths.split(b"\0")):
            relative_path = os.fsdecode(raw_path)
            path = Path.cwd() / relative_path
            if path.is_symlink():
                files.append((relative_path, os.fsencode(os.readlink(path)), True))
            elif path.is_file():
                files.append((relative_path, path.read_bytes(), False))
        return tuple(files)

    def _snapshot_refs(self) -> tuple[tuple[str, str], ...]:
        result = self._git("for-each-ref", "--format=%(refname) %(objectname)")
        if result.returncode != 0:
            raise RuntimeError("Unable to snapshot Git refs for Muse execution")
        refs = []
        for line in result.stdout.splitlines():
            ref_name, object_name = os.fsdecode(line).split(" ", 1)
            refs.append((ref_name, object_name))
        return tuple(refs)

    def _restore_refs(self, state: _GitState) -> None:
        expected = dict(state.refs)
        current = dict(self._snapshot_refs())
        for ref_name in current.keys() - expected.keys():
            if self._git("update-ref", "-d", ref_name).returncode != 0:
                raise RuntimeError(f"Auto-Coder could not remove Muse-created ref {ref_name}")
        for ref_name, object_name in expected.items():
            if current.get(ref_name) != object_name and self._git("update-ref", ref_name, object_name).returncode != 0:
                raise RuntimeError(f"Auto-Coder could not restore Muse-modified ref {ref_name}")

    def _restore_lifecycle(self, state: _GitState) -> None:
        if state.branch:
            restored = self._git("checkout", "-f", state.branch)
            if restored.returncode != 0:
                restored = self._git("checkout", "-B", state.branch, state.head)
        else:
            restored = self._git("checkout", "--detach", "-f", state.head)
        reset = self._git("reset", "--mixed", state.head)
        if restored.returncode != 0 or reset.returncode != 0:
            raise RuntimeError("Muse changed Git lifecycle state and Auto-Coder could not restore it")
        self._restore_refs(state)

    def _restore_index(self, state: _GitState) -> None:
        if self._git("reset", "--mixed", state.head).returncode != 0:
            raise RuntimeError("Auto-Coder could not unstage Muse changes")
        if state.staged_patch:
            result = subprocess.run(["git", "apply", "--binary", "--cached"], cwd=Path.cwd(), input=state.staged_patch, capture_output=True)
            if result.returncode != 0:
                raise RuntimeError("Auto-Coder could not restore the pre-Muse index")

    def _restore_repository(self, state: _GitState) -> None:
        """Restore the exact tracked/index/untracked state captured for no-edit."""
        self._restore_lifecycle(state)
        if self._git("reset", "--hard", state.head).returncode != 0 or self._git("clean", "-fdx").returncode != 0:
            raise RuntimeError("Muse changed repository state and Auto-Coder could not restore it")
        for patch, cached in ((state.staged_patch, True), (state.unstaged_patch, False)):
            if not patch:
                continue
            args = ["git", "apply", "--binary"]
            if cached:
                args.append("--cached")
            result = subprocess.run(args, cwd=Path.cwd(), input=patch, capture_output=True)
            if result.returncode != 0:
                raise RuntimeError("Muse changed repository state and Auto-Coder could not restore its pre-run patch")
            if cached and self._git("checkout-index", "-a", "-f").returncode != 0:
                raise RuntimeError("Muse changed repository state and Auto-Coder could not restore its working tree")
        for relative_path, contents, is_symlink in state.untracked_files + state.ignored_files:
            path = Path.cwd() / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            if is_symlink:
                path.symlink_to(os.fsdecode(contents))
            else:
                path.write_bytes(contents)

    @staticmethod
    def _trace_contains_git_mutation(trace_path: str) -> bool:
        """Return whether Git Trace2 observed a lifecycle-mutating command."""
        try:
            with open(trace_path, encoding="utf-8") as trace_file:
                for line in trace_file:
                    event = json.loads(line)
                    if event.get("event") == "cmd_name" and event.get("name") in _MUTATING_GIT_COMMANDS:
                        return True
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Unable to audit Git commands executed by Muse: {exc}") from exc
        return False

    def _assert_invariants(self, before: _GitState, is_noedit: bool, mutation_observed: bool = False) -> None:
        after = self._snapshot()
        lifecycle_changed = (after.branch, after.head) != (before.branch, before.head)
        refs_changed = after.refs != before.refs
        index_changed = after.staged_patch != before.staged_patch
        noedit_changed = is_noedit and (after.status != before.status or after.unstaged_patch != before.unstaged_patch or after.untracked_files != before.untracked_files or after.ignored_files != before.ignored_files)
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
            raise RuntimeError(f"Muse execution violated the Git-state invariant ({detail} changed)")

    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        before = self._snapshot()
        processed = self.config_backend.replace_placeholders(model_name=self.model_name) if self.config_backend else {}
        options = processed.get("options_for_noedit" if is_noedit and self.options_for_noedit else "options", self.options_for_noedit if is_noedit and self.options_for_noedit else self.options)
        command = shlex.split(os.environ.get("AUTOCODER_MUSE_CLI", "muse"))
        command.extend(["exec", *options])
        command.extend(self.consume_extra_args())
        command.append(render_prompt("muse.execution", task_prompt=prompt, mode="no-edit" if is_noedit else "edit"))
        env = os.environ.copy()
        if self.config_backend and self.config_backend.api_key and "MUSE_API_KEY" not in env:
            env["MUSE_API_KEY"] = self.config_backend.api_key

        trace_file = tempfile.NamedTemporaryFile(prefix="auto-coder-muse-git-trace-", delete=False)
        trace_path = trace_file.name
        trace_file.close()
        env["GIT_TRACE2_EVENT"] = trace_path

        logger.warning("LLM invocation: Muse Code CLI is being called. Keep LLM calls minimized.")
        logger.info("Running Muse Code in non-interactive %s mode", "no-edit" if is_noedit else "edit")
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=self.timeout, env=env)
        except subprocess.TimeoutExpired as exc:
            mutation_observed = self._trace_contains_git_mutation(trace_path)
            os.unlink(trace_path)
            self._assert_invariants(before, is_noedit, mutation_observed)
            raise AutoCoderTimeoutError(f"Muse Code CLI timed out after {self.timeout} seconds") from exc
        except OSError as exc:
            os.unlink(trace_path)
            raise RuntimeError(f"Muse Code CLI could not be executed: {exc}") from exc

        output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        mutation_observed = self._trace_contains_git_mutation(trace_path)
        os.unlink(trace_path)
        self._assert_invariants(before, is_noedit, mutation_observed)
        markers = self.usage_markers or ["rate limit", "usage limit", "quota exceeded", "429"]
        if has_usage_marker_match(output, markers):
            raise AutoCoderUsageLimitError(output or "Muse Code usage limit reached")
        if result.returncode != 0:
            raise RuntimeError(f"Muse Code CLI failed with return code {result.returncode}\n{output}")
        return output

    def check_mcp_server_configured(self, server_name: str) -> bool:
        return False

    def add_mcp_server_config(self, server_name: str, command: str, args: list[str]) -> bool:
        return False
