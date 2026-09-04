"""Local Muse Code CLI backend with Git lifecycle invariant enforcement."""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .exceptions import AutoCoderTimeoutError, AutoCoderUsageLimitError
from .llm_backend_config import get_llm_config
from .llm_client_base import LLMClientBase
from .llm_output_logger import LLMOutputLogger
from .logger_config import get_logger
from .prompt_loader import render_prompt
from .usage_marker_utils import has_usage_marker_match
from .utils import CommandExecutor

logger = get_logger(__name__)


@dataclass(frozen=True)
class GitExecutionState:
    """Git state that a local backend is not permitted to mutate."""

    head: str
    branch: Optional[str]
    index: bytes
    status: bytes


class MuseGitStateGuard:
    """Detect Muse-owned Git mutations and restore a safe pre-run state."""

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self._backup_dir = Path(tempfile.mkdtemp(prefix="auto-coder-muse-git-"))
        self.before = self._snapshot()
        self._capture_content()

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", *args],
            cwd=self.cwd,
            capture_output=True,
            check=check,
        )

    def _snapshot(self) -> GitExecutionState:
        head = self._git("rev-parse", "HEAD").stdout.decode().strip()
        branch_result = self._git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        branch = branch_result.stdout.decode().strip() if branch_result.returncode == 0 else None
        index = hashlib.sha256(self._git("diff", "--cached", "--binary").stdout).digest()
        status_parts = [
            self._git("status", "--porcelain=v1", "-z", "--untracked-files=all").stdout,
            self._git("diff", "--cached", "--binary").stdout,
            self._git("diff", "--binary").stdout,
        ]
        for raw_path in self._git("ls-files", "--others", "--exclude-standard", "-z").stdout.split(b"\0"):
            if not raw_path:
                continue
            path = self.cwd / raw_path.decode("utf-8", errors="surrogateescape")
            status_parts.append(raw_path)
            if path.is_symlink():
                status_parts.append(os.readlink(path).encode("utf-8", errors="surrogateescape"))
            elif path.is_file():
                status_parts.append(path.read_bytes())
        status = hashlib.sha256(b"\0".join(status_parts)).digest()
        return GitExecutionState(head=head, branch=branch, index=index, status=status)

    def _capture_content(self) -> None:
        (self._backup_dir / "index.patch").write_bytes(self._git("diff", "--cached", "--binary").stdout)
        (self._backup_dir / "worktree.patch").write_bytes(self._git("diff", "--binary").stdout)
        untracked = self._git("ls-files", "--others", "--exclude-standard", "-z").stdout.split(b"\0")
        manifest: list[str] = []
        for raw_path in untracked:
            if not raw_path:
                continue
            relative = raw_path.decode("utf-8", errors="surrogateescape")
            source = self.cwd / relative
            target = self._backup_dir / "untracked" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                target.symlink_to(os.readlink(source))
            elif source.is_file():
                shutil.copy2(source, target)
            manifest.append(relative)
        (self._backup_dir / "untracked.list").write_text("\n".join(manifest), encoding="utf-8")

    def validate(self, noedit: bool) -> None:
        """Raise after restoring state when Muse changed forbidden Git state."""
        after = self._snapshot()
        identity_changed = (after.head, after.branch) != (self.before.head, self.before.branch)
        index_changed = after.index != self.before.index
        content_changed = after.status != self.before.status
        if not identity_changed and not index_changed and (not noedit or not content_changed):
            return

        self._restore_content()

        if identity_changed:
            violation = "branch/HEAD"
        elif index_changed:
            violation = "index"
        else:
            violation = "working tree"
        raise RuntimeError(f"Muse violated the Auto-Coder Git-state invariant by changing {violation}; the execution was rejected and the pre-run Git state was restored")

    def _restore_content(self) -> None:
        self._restore_identity()
        self._git("reset", "--hard", self.before.head)
        self._git("clean", "-fd")
        index_patch = self._backup_dir / "index.patch"
        worktree_patch = self._backup_dir / "worktree.patch"
        if index_patch.stat().st_size:
            self._git("apply", "--binary", "--index", str(index_patch))
        if worktree_patch.stat().st_size:
            self._git("apply", "--binary", str(worktree_patch))
        manifest = (self._backup_dir / "untracked.list").read_text(encoding="utf-8")
        for relative in manifest.splitlines():
            source = self._backup_dir / "untracked" / relative
            target = self.cwd / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                target.symlink_to(os.readlink(source))
            else:
                shutil.copy2(source, target)

    def close(self) -> None:
        shutil.rmtree(self._backup_dir, ignore_errors=True)

    def _restore_identity(self) -> None:
        if self.before.branch:
            # Restore the named branch even if Muse moved its ref with a commit/reset.
            self._git("checkout", "--force", self.before.branch, check=False)
            self._git("reset", "--hard", self.before.head)
        else:
            self._git("checkout", "--detach", "--force", self.before.head)


class MuseClient(LLMClientBase):
    """Run Muse Code once in headless mode while Auto-Coder owns Git lifecycle."""

    def __init__(self, backend_name: Optional[str] = None, use_noedit_options: bool = False) -> None:
        super().__init__()
        self.backend_name = backend_name or "muse"
        config = get_llm_config()
        self.config_backend = config.get_backend_config(self.backend_name)
        self.model_name = (self.config_backend and self.config_backend.model) or "muse-spark-1.3-contributor"
        self.default_model = self.model_name
        self.options = list((self.config_backend and self.config_backend.options) or [])
        self.options_for_noedit = list((self.config_backend and self.config_backend.options_for_noedit) or [])
        self.api_key = self.config_backend and self.config_backend.api_key
        self.timeout = (self.config_backend and self.config_backend.timeout) or 1800
        self.usage_markers = list((self.config_backend and self.config_backend.usage_markers) or [])
        self.use_noedit_options = use_noedit_options
        self.output_logger = LLMOutputLogger()

    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        noedit = is_noedit or self.use_noedit_options
        cwd = Path.cwd()
        guard = MuseGitStateGuard(cwd)
        mode = "no-edit" if noedit else "editing"
        governed_prompt = render_prompt("muse.execution", task_prompt=prompt, mode=mode)
        processed = self.config_backend.replace_placeholders(model_name=self.model_name) if self.config_backend else {"options": [], "options_for_noedit": []}
        options = processed["options_for_noedit"] if noedit and processed["options_for_noedit"] else processed["options"]
        override = os.environ.get("AUTOCODER_MUSE_CLI")
        cmd = (shlex.split(override) if override else ["muse"]) + ["exec"]
        cmd.extend(options)
        if noedit:
            for flag in ("--trust-workspace", "--disable-approval", "--disable-write"):
                if flag not in cmd:
                    cmd.append(flag)
        if self.model_name and "--model" not in cmd:
            cmd.extend(["--model", self.model_name])
        cmd.extend(self.consume_extra_args())

        env = os.environ.copy()
        if self.api_key:
            env["META_API_KEY"] = self.api_key

        started = time.time()
        output = ""
        error: Optional[str] = None
        status = "success"
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as prompt_file:
                prompt_file.write(governed_prompt)
                prompt_path = prompt_file.name
            cmd.extend(["--prompt-file", prompt_path])
            logger.warning("LLM invocation: Muse Code CLI is being called. Keep LLM calls minimized.")
            result = CommandExecutor.run_command(cmd, stream_output=True, env=env, idle_timeout=self.timeout)
            output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip())
            if result.returncode == -1 and "timed out" in output.lower():
                raise AutoCoderTimeoutError(output or "Muse Code execution timed out")
            if has_usage_marker_match(output, self.usage_markers or ["rate limit", "usage limit", "quota", "429"]):
                raise AutoCoderUsageLimitError(output)
            if result.returncode != 0:
                raise RuntimeError(f"Muse Code CLI failed with return code {result.returncode}: {output}")
            guard.validate(noedit=noedit)
            return output
        except Exception as exc:
            status = "error"
            error = str(exc)
            # A failed process may still have mutated Git, so enforce the invariant.
            try:
                guard.validate(noedit=noedit)
            except RuntimeError as invariant_exc:
                if error != str(invariant_exc):
                    error = f"{error}; {invariant_exc}"
                raise RuntimeError(error) from exc
            raise
        finally:
            if "prompt_path" in locals():
                Path(prompt_path).unlink(missing_ok=True)
            guard.close()
            self.output_logger.log_interaction(
                backend=self.backend_name,
                model=self.model_name,
                prompt=prompt,
                response=output,
                duration_ms=(time.time() - started) * 1000,
                status=status,
                error=error,
            )

    def switch_to_default_model(self) -> None:
        self.model_name = self.default_model

    def check_mcp_server_configured(self, server_name: str) -> bool:
        return False

    def add_mcp_server_config(self, server_name: str, command: str, args: list[str]) -> bool:
        return False
