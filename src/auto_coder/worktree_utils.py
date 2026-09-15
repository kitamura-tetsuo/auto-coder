"""Git worktree utilities for local LLM isolation."""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Generator, Optional, Union

from .logger_config import get_logger
from .utils import _COMMAND_EXECUTION_CWD, bind_command_execution_cwd, reset_command_execution_cwd

logger = get_logger(__name__)

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


def _resolve_target(cwd: Optional[Union[Path, str]] = None) -> Path:
    if cwd is not None:
        return Path(cwd)
    cmd_cwd = _COMMAND_EXECUTION_CWD.get()
    if cmd_cwd:
        return Path(cmd_cwd)
    return Path.cwd()


def is_inside_git_worktree(cwd: Optional[Union[Path, str]] = None) -> bool:
    """Check if the given directory (or current execution cwd) is an isolated worktree.

    In a linked git worktree, ``.git`` is a file pointing to the main gitdir,
    rather than a directory.
    """
    target = _resolve_target(cwd)
    try:
        git_path = target / ".git"
        if git_path.is_file():
            return True
    except OSError:
        pass
    return False


def is_git_repository(cwd: Optional[Union[Path, str]] = None) -> bool:
    """Check if the given directory is inside any git repository or worktree."""
    target = _resolve_target(cwd)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=target,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def _seed_worktree_from_target(target: Path, worktree: Path) -> None:
    """Seed the newly created worktree with target repository's dirty state.

    Copies:
    1. Staged tracked changes (applied with git apply --cached and checked out).
    2. Unstaged tracked changes (applied with git apply).
    3. Untracked and ignored files (excluding disposable directories).
    4. Non-disposable directories (including empty dirs) and directory permissions.
    """
    # 1. Apply staged changes if any
    staged_diff = subprocess.run(
        ["git", "diff", "--cached", "--binary"],
        cwd=target,
        capture_output=True,
    )
    if staged_diff.returncode == 0 and staged_diff.stdout.strip():
        subprocess.run(
            ["git", "apply", "--cached", "--binary"],
            cwd=worktree,
            input=staged_diff.stdout,
            capture_output=True,
        )
        subprocess.run(
            ["git", "checkout-index", "-a", "-f"],
            cwd=worktree,
            capture_output=True,
        )

    # 2. Apply unstaged changes if any
    unstaged_diff = subprocess.run(
        ["git", "diff", "--binary"],
        cwd=target,
        capture_output=True,
    )
    if unstaged_diff.returncode == 0 and unstaged_diff.stdout.strip():
        subprocess.run(
            ["git", "apply", "--binary", "--whitespace=nowarn"],
            cwd=worktree,
            input=unstaged_diff.stdout,
            capture_output=True,
        )

    # 3. Copy untracked and ignored files (excluding disposable directories)
    untracked_res = subprocess.run(
        ["git", "ls-files", "--others", "-z"],
        cwd=target,
        capture_output=True,
    )
    if untracked_res.returncode == 0 and untracked_res.stdout:
        for raw_path in filter(None, untracked_res.stdout.split(b"\0")):
            rel = os.fsdecode(raw_path)
            parts = Path(rel).parts
            if any(part in _DISPOSABLE_DIRECTORY_NAMES for part in parts):
                continue
            src_file = target / rel
            dst_file = worktree / rel
            try:
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                if src_file.is_symlink():
                    if dst_file.exists() or dst_file.is_symlink():
                        dst_file.unlink()
                    dst_file.symlink_to(os.readlink(src_file))
                elif src_file.is_file():
                    shutil.copy2(src_file, dst_file)
            except OSError as exc:
                logger.warning("Failed to seed file {} to isolated worktree: {}", rel, exc)

    # 4. Replicate directory hierarchy, empty directories, and permissions
    for root, dirs, _ in os.walk(target, followlinks=False):
        dirs[:] = [d for d in dirs if d not in _DISPOSABLE_DIRECTORY_NAMES]
        for d in dirs:
            src_dir = Path(root) / d
            rel_dir = src_dir.relative_to(target)
            dst_dir = worktree / rel_dir
            try:
                if not dst_dir.exists():
                    dst_dir.mkdir(parents=True, exist_ok=True)
                dst_dir.chmod(stat.S_IMODE(src_dir.stat().st_mode))
            except OSError:
                pass


def sync_worktree_changes_back(source_worktree: Union[Path, str], target_repo: Union[Path, str]) -> None:
    """Synchronize modified, deleted, and untracked files from an isolated worktree back to the target workspace."""
    src = Path(source_worktree)
    dst = Path(target_repo)

    # 1. Apply tracked modifications and deletions via git diff
    diff_res = subprocess.run(
        ["git", "diff", "--binary", "HEAD"],
        cwd=src,
        capture_output=True,
    )
    applied = False
    if diff_res.returncode == 0 and diff_res.stdout.strip():
        apply_res = subprocess.run(
            ["git", "apply", "--binary", "--whitespace=nowarn"],
            cwd=dst,
            input=diff_res.stdout,
            capture_output=True,
        )
        if apply_res.returncode == 0:
            applied = True
        else:
            logger.warning(
                "Failed to apply worktree diff back to target workspace: {}",
                apply_res.stderr.decode("utf-8", errors="replace").strip(),
            )

    # Fallback or supplementary: if git apply did not succeed, synchronize changed files directly
    if not applied and diff_res.returncode == 0 and diff_res.stdout.strip():
        status_res = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=src,
            capture_output=True,
            text=True,
        )
        if status_res.returncode == 0:
            for line in status_res.stdout.splitlines():
                if len(line) < 4:
                    continue
                code = line[:2]
                path_str = line[3:].strip()
                if " -> " in path_str:
                    path_str = path_str.split(" -> ")[1].strip()
                src_f = src / path_str
                dst_f = dst / path_str
                if "D" in code:
                    if dst_f.exists() or dst_f.is_symlink():
                        try:
                            dst_f.unlink()
                        except OSError:
                            pass
                elif src_f.exists():
                    try:
                        dst_f.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_f, dst_f)
                    except OSError:
                        pass

    # 2. Copy untracked new files
    untracked_res = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=src,
        capture_output=True,
    )
    if untracked_res.returncode == 0 and untracked_res.stdout:
        for raw_path in filter(None, untracked_res.stdout.split(b"\0")):
            rel = os.fsdecode(raw_path)
            parts = Path(rel).parts
            if any(part in _DISPOSABLE_DIRECTORY_NAMES for part in parts):
                continue
            src_file = src / rel
            dst_file = dst / rel
            try:
                if src_file.is_symlink():
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    if dst_file.exists() or dst_file.is_symlink():
                        dst_file.unlink()
                    dst_file.symlink_to(os.readlink(src_file))
                elif src_file.is_file():
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dst_file)
            except OSError as exc:
                logger.warning("Failed to copy untracked file {} back to target workspace: {}", rel, exc)


@contextlib.contextmanager
def isolated_local_llm_worktree(
    base_cwd: Optional[Union[Path, str]] = None,
    is_noedit: bool = False,
) -> Generator[str, None, None]:
    """Isolate local LLM execution in a detached git worktree.

    - If the directory is already inside an isolated linked worktree (where ``.git`` is a file)
      or not a git repository, yields the target directory directly without nesting.
    - Otherwise, creates an ephemeral detached worktree at ``HEAD``, binds
      ``_COMMAND_EXECUTION_CWD`` to it for the duration of the execution context, and cleans
      it up in ``finally``.
    - If ``is_noedit`` is False, changes made in the worktree are synchronized back to the
      target workspace before the worktree is destroyed.
    """
    target = _resolve_target(base_cwd)

    # If already an isolated worktree or not a git repository, run directly
    if is_inside_git_worktree(target) or not is_git_repository(target):
        yield str(target)
        return

    worktree_dir: Optional[str] = None
    execution_token = None
    try:
        worktree_dir = tempfile.mkdtemp(prefix="auto_coder_llm_wt_")
        add_res = subprocess.run(
            ["git", "worktree", "add", "--detach", worktree_dir, "HEAD"],
            cwd=target,
            capture_output=True,
            text=True,
        )
        if add_res.returncode != 0:
            logger.warning(
                "Failed to create isolated git worktree for local LLM: {}",
                add_res.stderr.strip(),
            )
            shutil.rmtree(worktree_dir, ignore_errors=True)
            worktree_dir = None
            yield str(target)
            return

        _seed_worktree_from_target(target, Path(worktree_dir))
        execution_token = bind_command_execution_cwd(worktree_dir)
        logger.debug("Bound isolated local LLM worktree at {} (is_noedit={})", worktree_dir, is_noedit)
        yield worktree_dir
    finally:
        if execution_token is not None:
            reset_command_execution_cwd(execution_token)

        if worktree_dir and os.path.exists(worktree_dir):
            try:
                if not is_noedit:
                    sync_worktree_changes_back(worktree_dir, target)
            finally:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", worktree_dir],
                    cwd=target,
                    capture_output=True,
                )
                shutil.rmtree(worktree_dir, ignore_errors=True)
                logger.debug("Cleaned up isolated local LLM worktree at {}", worktree_dir)
