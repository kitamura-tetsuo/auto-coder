"""Branch Manager for git branch operations.

This module provides a context manager for handling git branch switches and
restoring the original state, similar to how LabelManager handles labels.
"""

import threading
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Optional

from .checkout_lock import checkout_lock
from .git_branch import switch_to_branch
from .git_commit import ensure_pushed
from .git_info import get_current_branch, is_git_repository
from .logger_config import get_logger
from .utils import _COMMAND_EXECUTION_CWD

logger = get_logger(__name__)


class BranchManager:
    """Context manager for Unified git branch operations.

    This context manager handles switching to a specific branch for a task and
    ensuring the environment is returned to the original branch when done.

    Usage:
        with BranchManager(branch_name="feature-1", create_new=True, base_branch="main"):
            # Do work on feature-1
            pass
        # Automatically back on original branch
    """

    # Track active branches to handle reentrancy if needed
    _active_branches: set[tuple[int, str, str]] = set()

    def __init__(
        self,
        branch_name: str,
        create_new: bool = False,
        base_branch: Optional[str] = None,
        cwd: Optional[str] = None,
        check_unpushed: bool = True,
        remote: str = "origin",
    ):
        """Initialize BranchManager.

        Args:
            branch_name: Name of the branch to switch to
            create_new: Whether to create the branch if it doesn't exist
            base_branch: Base branch to create from (required if create_new=True)
            cwd: Working directory
            check_unpushed: Whether to check for unpushed commits before processing
            remote: Remote name
        """
        self.branch_name = branch_name
        self.create_new = create_new
        self.base_branch = base_branch
        self.cwd = cwd
        self.check_unpushed = check_unpushed
        self.remote = remote

        self.original_branch: Optional[str] = None
        self._checkout_lease: Optional[AbstractContextManager[None]] = None
        self._branch_key: Optional[tuple[int, str, str]] = None
        self._reentered = False
        self._switched = False

    def __enter__(self) -> "BranchManager":
        """Switch to the target branch."""
        self._checkout_lease = checkout_lock(self.cwd)
        self._checkout_lease.__enter__()
        self._branch_key = (threading.get_ident(), str(Path(self.cwd or _COMMAND_EXECUTION_CWD.get() or Path.cwd()).resolve()), self.branch_name)
        try:
            return self._enter_locked()
        except BaseException:
            BranchManager._active_branches.discard(self._branch_key)
            self._checkout_lease.__exit__(None, None, None)
            self._checkout_lease = None
            raise

    def _enter_locked(self) -> "BranchManager":
        """Capture and switch branches only after acquiring checkout ownership."""
        # Reentrancy detection
        assert self._branch_key is not None
        branch_key = self._branch_key

        if branch_key in BranchManager._active_branches:
            self._reentered = True
            logger.debug(f">>> Already active on branch {self.branch_name} in this thread")
            return self

        BranchManager._active_branches.add(branch_key)

        # Store original branch
        self.original_branch = get_current_branch(cwd=self.cwd)
        if not self.original_branch:
            raise RuntimeError("Failed to get current branch before switching")

        # specific optimization: if we are already on the target branch (and not creating new for reset purposes)
        if self.original_branch == self.branch_name and not self.create_new:
            logger.info(f"Already on branch '{self.branch_name}', staying on current branch")
            return self

        logger.info(f"Switching to branch '{self.branch_name}' (from '{self.original_branch}')")

        switch_result = switch_to_branch(branch_name=self.branch_name, create_new=self.create_new, base_branch=self.base_branch, cwd=self.cwd, publish=True, pull_after_switch=True)

        if not switch_result.success:
            # Clean up tracking before raising
            BranchManager._active_branches.discard(branch_key)
            raise RuntimeError(f"Failed to switch to branch '{self.branch_name}': {switch_result.stderr}")

        self._switched = True

        # Check unpushed commits if requested
        if self.check_unpushed:
            self._check_unpushed_commits()

        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Return to the original branch."""
        try:
            self._exit_locked()
        finally:
            if self._checkout_lease is not None:
                self._checkout_lease.__exit__(exc_type, exc_val, exc_tb)
                self._checkout_lease = None

    def _exit_locked(self) -> None:
        """Restore the caller before allowing another checkout user to enter."""
        assert self._branch_key is not None
        branch_key = self._branch_key

        if self._reentered:
            logger.debug(f">>> Skipping exit (reentrant) for branch {self.branch_name}")
            return

        BranchManager._active_branches.discard(branch_key)

        # Implementation logic to return to original branch
        if not self._switched:
            # We didn't switch, so no need to switch back
            return

        if not self.original_branch:
            # Should not happen if _switched is True
            return

        if is_git_repository(self.cwd):
            current = get_current_branch(cwd=self.cwd)
            if current != self.original_branch:
                logger.info(f"Returning to original branch '{self.original_branch}'")
                result = switch_to_branch(branch_name=self.original_branch, cwd=self.cwd, pull_after_switch=True)
                if not result.success:
                    logger.warning(f"Failed to return to branch '{self.original_branch}': {result.stderr}")
            else:
                logger.info(f"Already on original branch '{self.original_branch}'")
        else:
            logger.warning("Not in a git repository, cannot switch back to original branch")

    def _check_unpushed_commits(self) -> None:
        """Check for unpushed commits and push them."""
        logger.info("Checking for unpushed commits...")
        push_result = ensure_pushed(cwd=self.cwd, remote=self.remote)
        if push_result.success and "No unpushed commits" not in push_result.stdout:
            logger.info("Successfully pushed unpushed commits")
        elif not push_result.success:
            logger.warning(f"Failed to push unpushed commits: {push_result.stderr}")
