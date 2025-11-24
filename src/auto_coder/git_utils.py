"""
Git utilities compatibility module.

This module provides backward compatibility by re-exporting functions from the new focused modules:
- git_info.py: Read-only git information functions
- git_branch.py: Branch management and operations
- git_commit.py: Commit and push operations

This module exists to maintain compatibility with existing imports.
For new code, prefer importing directly from the specific modules.
"""

# Re-export functions from git_branch.py (branch operations)
from .git_branch import (
    branch_context,
    branch_exists,
    extract_number_from_branch,
    get_all_branches,
    get_branches_by_pattern,
    git_checkout_branch,
    git_commit_with_retry,
    git_pull,
    migrate_pr_branches,
    resolve_pull_conflicts,
    switch_to_branch,
    try_llm_commit_push,
    try_llm_dprint_fallback,
    validate_branch_name,
)

# Re-export functions from git_commit.py (commit and push operations)
from .git_commit import (
    commit_and_push_changes,
    ensure_pushed,
    git_push,
    save_commit_failure_history,
)

# Re-export functions from git_info.py (read-only git functions)
from .git_info import (
    check_unpushed_commits,
    get_commit_log,
    get_current_branch,
    get_current_repo_name,
    is_git_repository,
    parse_github_repo_from_url,
)

# Import CommandExecutor and CommandResult for test compatibility
from .utils import CommandExecutor, CommandResult

__all__ = [
    # From utils.py for test compatibility
    "CommandExecutor",
    "CommandResult",
    # From git_info.py
    "check_unpushed_commits",
    "get_commit_log",
    "get_current_branch",
    "get_current_repo_name",
    "is_git_repository",
    "parse_github_repo_from_url",
    # From git_branch.py
    "branch_context",
    "branch_exists",
    "extract_number_from_branch",
    "get_all_branches",
    "get_branches_by_pattern",
    "git_checkout_branch",
    "git_commit_with_retry",
    "git_pull",
    "migrate_pr_branches",
    "resolve_pull_conflicts",
    "switch_to_branch",
    "try_llm_commit_push",
    "try_llm_dprint_fallback",
    "validate_branch_name",
    # From git_commit.py
    "commit_and_push_changes",
    "ensure_pushed",
    "git_push",
    "save_commit_failure_history",
]
