"""
Git branch management utilities for Auto-Coder.

This module contains functions for managing git branches, including creating,
checking out, and validating branch names.
"""

import re
from contextlib import contextmanager
from typing import Any, Dict, Generator, List, Optional

from auto_coder.automation_config import AutomationConfig
from auto_coder.backend_manager import run_llm_prompt

from .git_commit import git_push
from .git_info import check_unpushed_commits, get_current_branch
from .logger_config import get_logger
from .prompt_loader import render_prompt
from .utils import CommandExecutor, CommandResult

# Re-export CommandExecutor and CommandResult for test compatibility
__all__ = [
    "CommandExecutor",
    "CommandResult",
    # Function names
    "branch_context",
    "branch_exists",
    "detect_branch_name_conflict",
    "extract_number_from_branch",
    "extract_attempt_from_branch",
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
]

logger = get_logger(__name__)


def _command_exists(command: str) -> bool:
    """
    Check if a command exists in the system PATH.

    Args:
        command: The command to check

    Returns:
        True if the command exists, False otherwise
    """
    cmd = CommandExecutor()
    result = cmd.run_command(["which", command])
    return result.success


def _is_black_error(error_output: str) -> bool:
    """
    Check if the error is from black formatting issues.

    Args:
        error_output: The error message from git commit

    Returns:
        True if the error is related to black formatting issues
    """
    if not error_output:
        return False
    s = error_output.lower()
    return "black" in s and ("files were modified" in s or "reformatted" in s or "would be reformatted" in s)


def _is_isort_error(error_output: str) -> bool:
    """
    Check if the error is from isort import sorting issues.

    Args:
        error_output: The error message from git commit

    Returns:
        True if the error is related to isort import sorting issues
    """
    if not error_output:
        return False
    s = error_output.lower()
    return "isort" in s and ("would reorder" in s or "imports are in the wrong order" in s or "unbalanced tuple" in s or "wrong order" in s or "linted wrong file" in s)


def git_commit_with_retry(commit_message: str, cwd: Optional[str] = None, max_retries: int = 1) -> CommandResult:
    """
    Commit changes with automatic handling of formatter hook failures.

    This function centralizes git commit operations and handles well-known
    hook failures like dprint, black, and isort formatting errors by automatically
    running the appropriate formatter and retrying the commit once. If the formatter
    itself fails, it attempts to use LLM as a fallback to resolve the issue.

    Args:
        commit_message: The commit message to use
        cwd: Optional working directory for the git command
        max_retries: Maximum number of retries (default: 1)

    Returns:
        CommandResult with the result of the commit operation
    """
    cmd = CommandExecutor()

    for attempt in range(max_retries + 1):
        result = cmd.run_command(["git", "commit", "-m", commit_message], cwd=cwd)

        # If commit succeeded, return immediately
        if result.success:
            logger.info("Successfully committed changes")
            return result

        # Combine stdout and stderr for error detection
        error_output = f"{result.stdout}\n{result.stderr}"

        # Check if the failure is simply because there is nothing to commit (clean working tree)
        if "nothing to commit, working tree clean" in error_output.lower():
            logger.info("Nothing to commit (clean working tree), treating as success")
            return CommandResult(
                success=True,
                stdout=result.stdout,
                stderr=result.stderr,
                returncode=0,
            )

        # Check for formatter-specific errors
        is_dprint_error = "dprint fmt" in error_output or "Formatting issues detected" in error_output
        is_black_error = _is_black_error(error_output)
        is_isort_error = _is_isort_error(error_output)

        # If any formatter error is detected, try to fix it
        if is_dprint_error or is_black_error or is_isort_error:
            if attempt < max_retries:
                # Determine which formatter to run based on the error
                if is_dprint_error:
                    logger.info("Detected dprint formatting issues, running 'npx dprint fmt' and retrying...")
                    fmt_result = cmd.run_command(["npx", "dprint", "fmt"], cwd=cwd)
                elif is_black_error:
                    logger.info("Detected black formatting issues, running 'black' and retrying...")
                    # Check if we should use uv or system Python
                    if _command_exists("uv"):
                        fmt_result = cmd.run_command(["uv", "run", "black", "src/", "tests/"], cwd=cwd)
                    else:
                        fmt_result = cmd.run_command(["black", "src/", "tests/"], cwd=cwd)
                else:  # is_isort_error
                    logger.info("Detected isort import sorting issues, running 'isort' and retrying...")
                    # Check if we should use uv or system Python
                    if _command_exists("uv"):
                        fmt_result = cmd.run_command(["uv", "run", "isort", "src/", "tests/"], cwd=cwd)
                    else:
                        fmt_result = cmd.run_command(["isort", "src/", "tests/"], cwd=cwd)

                if fmt_result.success:
                    logger.info("Successfully ran formatter")
                    # Stage the formatted files
                    add_result = cmd.run_command(["git", "add", "-u"], cwd=cwd)
                    if add_result.success:
                        logger.info("Staged formatted files, retrying commit...")
                        continue
                    else:
                        logger.warning(f"Failed to stage formatted files: {add_result.stderr}")
                else:
                    logger.warning(f"Failed to run formatter: {fmt_result.stderr}")
                    # Try LLM fallback when formatter execution fails
                    logger.info("Attempting to resolve formatter failure using LLM...")
                    llm_success = try_llm_dprint_fallback(commit_message, fmt_result.stderr)
                    if llm_success:
                        logger.info("LLM successfully resolved formatter failure")
                        # Retry the commit after LLM intervention
                        retry_result = cmd.run_command(["git", "commit", "-m", commit_message], cwd=cwd)
                        if retry_result.success:
                            logger.info("Successfully committed changes after LLM intervention")
                            return retry_result
                    else:
                        logger.error("LLM failed to resolve formatter failure")
            else:
                logger.warning(f"Max retries ({max_retries}) reached for commit with formatter issues")
        else:
            # Non-formatter or unknown error: attempt LLM-based remediation, then retry commit
            logger.warning(f"Failed to commit changes: {result.stderr}")
            logger.info("Attempting to resolve commit failure using LLM...")
            try:
                llm_success = try_llm_commit_push(commit_message, error_output, verify_push=False)
            except TypeError:
                # Backward compatibility if verify_push param is not available
                llm_success = try_llm_commit_push(commit_message, error_output)  # type: ignore
            if llm_success:
                # After LLM intervention, try committing again
                retry_result = cmd.run_command(["git", "commit", "-m", commit_message], cwd=cwd)
                if retry_result.success:
                    logger.info("Successfully committed changes after LLM intervention")
                    return retry_result
                # If commit still fails, treat as success when nothing is left to commit
                status_check = cmd.run_command(["git", "status", "--porcelain"], cwd=cwd)
                if status_check.success and not status_check.stdout.strip():
                    logger.info("No changes left to commit after LLM intervention; treating as success")
                    return CommandResult(
                        success=True,
                        stdout="No changes to commit after LLM fix",
                        stderr="",
                        returncode=0,
                    )
            # If LLM couldn't resolve, return the original failure
            return result

    # If we get here, all attempts failed
    logger.warning(f"Failed to commit changes: {result.stderr}")
    return result


def try_llm_commit_push(
    commit_message: str | None,
    error_message: str,
    verify_push: bool = True,
) -> bool:
    """
    Try to use LLM to resolve commit/push failures.

    Args:
        commit_message: The commit message that was attempted
        error_message: The error message from the failed commit/push

    Returns:
        True if LLM successfully resolved the issue, False otherwise
    """
    cmd = CommandExecutor()

    try:

        # Create prompt for LLM to resolve commit/push failure
        prompt = render_prompt(
            "tests.commit_and_push",
            commit_message=commit_message,
            error_message=error_message,
        )

        # Execute LLM to resolve the issue
        response = run_llm_prompt(prompt)

        if not response:
            logger.error("No response from LLM for commit/push resolution")
            return False

        # Check if LLM indicated success
        if "COMMIT_PUSH_RESULT: SUCCESS" in response:
            logger.info("LLM successfully resolved commit/push failure")

            # Verify that there are no uncommitted changes
            status_result = cmd.run_command(["git", "status", "--porcelain"])
            if status_result.stdout.strip():
                logger.error("LLM claimed success but there are still uncommitted changes")
                logger.error(f"Uncommitted changes: {status_result.stdout}")
                return False

            # Optionally verify that the push was successful by checking for unpushed commits
            if verify_push:
                unpushed_result = cmd.run_command(["git", "log", "@{u}..HEAD", "--oneline"])
                if unpushed_result.success and unpushed_result.stdout.strip():
                    logger.error("LLM claimed success but there are still unpushed commits")
                    logger.error(f"Unpushed commits: {unpushed_result.stdout}")
                    return False

            return True
        elif "COMMIT_PUSH_RESULT: FAILED:" in response:
            # Extract failure reason
            failure_reason = response.split("COMMIT_PUSH_RESULT: FAILED:", 1)[1].strip()
            logger.error(f"LLM failed to resolve commit/push: {failure_reason}")
            return False
        else:
            logger.error("LLM did not provide a clear success/failure indication")
            logger.error(f"LLM response: {response[:500]}")
            return False

    except Exception as e:
        logger.error(f"Error while trying to use LLM for commit/push: {e}")
        return False


def try_llm_dprint_fallback(
    commit_message: str | None,
    error_message: str,
) -> bool:
    """
    Try to use LLM to resolve dprint formatter failures.

    This function provides a specialized fallback mechanism for when the dprint
    formatter fails to execute. It leverages the LLM to diagnose and resolve
    common dprint issues such as:
    - Configuration file problems
    - Missing dependencies or plugins
    - Permission issues
    - Plugin loading errors

    Args:
        commit_message: The commit message that was attempted
        error_message: The error message from the failed dprint formatter

    Returns:
        True if LLM successfully resolved the dprint issue, False otherwise
    """
    cmd = CommandExecutor()

    try:
        # Create prompt for LLM to resolve dprint failure
        prompt = render_prompt(
            "tests.dprint_fallback",
            commit_message=commit_message,
            error_message=error_message,
        )

        # Execute LLM to resolve the issue
        response = run_llm_prompt(prompt)

        if not response:
            logger.error("No response from LLM for dprint fallback")
            return False

        # Check if LLM indicated success
        if "DPRINT_RESULT: SUCCESS" in response:
            logger.info("LLM successfully resolved dprint formatting issue")

            # Verify that dprint can now run successfully
            fmt_result = cmd.run_command(["npx", "dprint", "fmt"])
            if not fmt_result.success:
                logger.error("LLM claimed success but dprint still fails")
                logger.error(f"dprint error: {fmt_result.stderr}")
                return False

            # Verify that formatted files are staged
            status_result = cmd.run_command(["git", "status", "--porcelain"])
            if not status_result.stdout.strip():
                logger.warning("dprint ran successfully but no files were formatted")
                # This is not necessarily an error - files might already be formatted
                return True

            # Stage the formatted files
            add_result = cmd.run_command(["git", "add", "-A"])
            if not add_result.success:
                logger.error("Failed to stage formatted files after LLM resolution")
                return False

            logger.info("Successfully formatted files after LLM intervention")
            return True
        elif "DPRINT_RESULT: FAILED:" in response:
            # Extract failure reason
            failure_reason = response.split("DPRINT_RESULT: FAILED:", 1)[1].strip()
            logger.error(f"LLM failed to resolve dprint issue: {failure_reason}")
            return False
        else:
            logger.error("LLM did not provide a clear success/failure indication for dprint")
            logger.error(f"LLM response: {response[:500]}")
            return False

    except Exception as e:
        logger.error(f"Error while trying to use LLM for dprint fallback: {e}")
        return False


def extract_number_from_branch(branch_name: str) -> Optional[int]:
    """
    Extract issue or PR number from branch name.

    Supports patterns like:
    - issue-123
    - pr-456
    - issue-123_attempt-1 (new format with underscore)
    - issue-123/attempt-1 (legacy format with slash)
    - feature/issue-789
    - fix/pr-101

    Args:
        branch_name: Branch name to parse

    Returns:
        Extracted number or None if no number found
    """
    if not branch_name:
        return None

    # Try to match issue-XXX or pr-XXX pattern
    patterns = [
        r"issue-(\d+)",
        r"pr-(\d+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, branch_name, re.IGNORECASE)
        if match:
            return int(match.group(1))

    return None


def extract_attempt_from_branch(branch_name: str) -> Optional[int]:
    """
    Extract attempt number from branch name.

    Supports patterns like:
    - issue-123_attempt-1 (new format with underscore) - introduced in v1.x.x to avoid Git ref namespace conflicts
    - issue-456_attempt-2
    - issue-123/attempt-1 (legacy format with slash, for backward compatibility)
    - issue-456/attempt-2 (legacy)
    - issue-789 (returns None for no attempt suffix)

    Args:
        branch_name: Branch name to parse

    Returns:
        Extracted attempt number or None if no attempt suffix found
    """
    if not branch_name:
        return None

    # Try new underscore format first: issue-XXX_attempt-Y
    match = re.search(r"issue-\d+_attempt-(\d+)", branch_name, re.IGNORECASE)
    if match:
        return int(match.group(1))

    # Fallback to legacy slash format for backward compatibility: issue-XXX/attempt-Y
    match = re.search(r"issue-\d+/attempt-(\d+)", branch_name, re.IGNORECASE)
    if match:
        return int(match.group(1))

    return None


def validate_branch_name(branch_name: str) -> None:
    """
    Validate that a branch name does not match the prohibited pr-<number> pattern.

    This validation works in conjunction with explicit instructions in LLM prompts
    (see prompts.yaml) that instruct LLMs to always use 'issue-<number>' naming
    convention and never create 'pr-<number>' branches.

    Args:
        branch_name: The branch name to validate

    Raises:
        ValueError: If the branch name matches the prohibited pattern
    """
    if not branch_name:
        return

    # Check if branch name matches the prohibited pr-<number> pattern
    pattern = r"^pr-\d+$"
    if re.match(pattern, branch_name, re.IGNORECASE):
        raise ValueError(f"Branch name '{branch_name}' matches the prohibited pattern 'pr-<number>'. " f"Use 'issue-<number>' naming convention instead (e.g., 'issue-{branch_name.split('-')[1] if '-' in branch_name else '123'}').")


def branch_exists(branch_name: str, cwd: Optional[str] = None) -> bool:
    """
    Check if a branch with the given name exists.

    Args:
        branch_name: Name of the branch to check
        cwd: Optional working directory for the git command

    Returns:
        True if the branch exists, False otherwise
    """
    cmd = CommandExecutor()
    result = cmd.run_command(["git", "branch", "--list", branch_name], cwd=cwd)
    return result.success and bool(result.stdout.strip())


def detect_branch_name_conflict(branch_name: str, cwd: Optional[str] = None) -> Optional[str]:
    """
    Detect if the branch name conflicts with existing branches.

    This function checks for Git ref namespace collisions where creating a
    branch would fail due to naming conflicts. Git stores branch references as
    filesystem paths under .git/refs/heads/, which means a path cannot be both
    a file (branch) and a directory (parent of child branches).

    Conflict Detection Logic:

    1. Parent Path Conflicts:
       If the branch name contains slashes (e.g., 'issue-699/attempt-1'),
       check if the parent path exists as a branch (e.g., 'issue-699').
       Example: Cannot create 'issue-699/attempt-1' if 'issue-699' exists.

    2. Child Branch Conflicts:
       Check if any existing branches would conflict by being children of the
       requested branch name (e.g., 'issue-699/*').
       Example: Cannot create 'issue-699' if 'issue-699/attempt-1' exists.

    Common Conflict Scenarios:
    - issue-699 vs issue-699/attempt-1 (or issue-699/attempt-2, etc.)
    - feature vs feature/new-api
    - pr-123 vs pr-123/fix-typo

    Resolution:
    When a conflict is detected, you must delete the conflicting branch before
    creating the new one:
    - Delete parent: git branch -D issue-699
    - Then create child: git checkout -b issue-699/attempt-1

    Args:
        branch_name: Name of the branch to check for conflicts
        cwd: Optional working directory for the git command

    Returns:
        Name of the conflicting branch if a conflict is detected, or None if no conflict.
        The return value is the name of the existing branch that would cause the conflict.

    Example:
        >>> detect_branch_name_conflict("issue-699/attempt-1")
        'issue-699'  # Cannot create attempt-1 because issue-699 exists

        >>> detect_branch_name_conflict("issue-699")
        'issue-699/attempt-1'  # Cannot create issue-699 because attempt-1 exists

        >>> detect_branch_name_conflict("issue-700")
        None  # No conflict, safe to create
    """
    # Check if parent path exists as a branch
    # e.g., for "issue-699/attempt-1", check if "issue-699" exists
    if "/" in branch_name:
        parent_branch = branch_name.rsplit("/", 1)[0]
        if branch_exists(parent_branch, cwd):
            return parent_branch

    # Check if any child branches would conflict
    # e.g., for "issue-699", check if "issue-699/*" exists
    # For branch names with slashes (e.g., "feature/issue-123"), this checks
    # if any child branches would conflict at the same level
    pattern = f"{branch_name}/*"
    matching_branches = get_branches_by_pattern(pattern, cwd=cwd, remote=False)
    if matching_branches:
        return matching_branches[0]

    return None


def git_checkout_branch(
    branch_name: str,
    create_new: bool = False,
    base_branch: Optional[str] = None,
    cwd: Optional[str] = None,
    publish: bool = True,
) -> CommandResult:
    """
    Switch to a git branch and verify the checkout was successful.

    This function centralizes git checkout operations and ensures that after
    switching branches, the current branch matches the expected branch.
    If creating a new branch, it will automatically push to remote and set up tracking.

    Branch Name Conflict Detection:
        When creating a new branch, this function automatically checks for Git ref
        namespace conflicts before attempting to create the branch. For example:
        - If 'issue-699' branch exists, 'issue-699/attempt-1' cannot be created
        - If 'issue-699/*' branches exist, 'issue-699' branch cannot be created

        If a conflict is detected, the function returns a CommandResult with:
        - success=False
        - stderr containing the conflicting branch name and resolution steps

        See detect_branch_name_conflict() for more details on conflict detection logic.

    Args:
        branch_name: Name of the branch to checkout
        create_new: If True, creates a new branch with -b flag
        base_branch: If create_new is True and base_branch is specified, creates
                     the new branch from base_branch (using -B flag)
        cwd: Optional working directory for the git command
        publish: If True and create_new is True, push the new branch to remote and set up tracking

    Returns:
        CommandResult with the result of the checkout operation.
        success=True only if checkout succeeded AND current branch matches expected branch.
    """
    cmd = CommandExecutor()

    # Check for uncommitted changes before checkout
    status_result = cmd.run_command(["git", "status", "--porcelain"], cwd=cwd)
    has_changes = status_result.success and status_result.stdout.strip()

    if has_changes:
        logger.info("Detected uncommitted changes before checkout, committing them first")
        # Add all changes
        add_result = cmd.run_command(["git", "add", "-A"], cwd=cwd)
        if not add_result.success:
            logger.warning(f"Failed to add changes: {add_result.stderr}")

        # Commit changes
        commit_result = git_commit_with_retry(
            commit_message="WIP: Auto-commit before branch checkout",
            cwd=cwd,
            max_retries=1,
        )
        if not commit_result.success:
            logger.warning(f"Failed to commit changes before checkout: {commit_result.stderr}")

    # Check if branch already exists locally (only when create_new is True)
    branch_exists_locally = False
    if create_new:
        list_result = cmd.run_command(["git", "branch", "--list", branch_name], cwd=cwd)
        branch_exists_locally = bool(list_result.success and list_result.stdout.strip())

    # Validate branch name only when creating a NEW branch that doesn't exist
    # Existing branches with pr-<number> pattern can be checked out without validation
    if create_new and not branch_exists_locally:
        try:
            validate_branch_name(branch_name)
        except ValueError as e:
            logger.error(str(e))
            return CommandResult(
                success=False,
                stdout="",
                stderr=str(e),
                returncode=1,
            )

    # Build checkout command (enforce correct base for new branches)
    checkout_cmd: List[str] = ["git", "checkout"]
    resolved_base_ref: Optional[str] = None
    branch_exists_remotely = False  # Initialize for scope
    branch_exists_locally_after_fetch = False  # Initialize for scope

    if create_new:
        if base_branch is None:
            # Fail fast to detect incorrect call sites
            raise ValueError("When create_new=True, base_branch must be provided (e.g., 'main').")

        # Always fetch latest refs before creating a new branch
        logger.info("Fetching 'origin' with --prune --tags before creating new branch...")
        fetch_result = cmd.run_command(["git", "fetch", "origin", "--prune", "--tags"], cwd=cwd)

        # After fetching, check if branch already exists locally or remotely
        # (Re-check local existence after fetch, as it might have been created remotely)
        list_result = cmd.run_command(["git", "branch", "--list", branch_name], cwd=cwd)
        branch_exists_locally_after_fetch = bool(list_result.success and list_result.stdout.strip())

        # Check if branch exists remotely (defensive check)
        # Only check if local branch doesn't exist, as remote check adds overhead
        if not branch_exists_locally_after_fetch:
            try:
                remote_result = cmd.run_command(["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch_name}"], cwd=cwd)
                # Only consider remote exists if the command succeeds AND returns output
                # If the command fails (e.g., no remote origin configured), assume branch doesn't exist remotely
                branch_exists_remotely = remote_result.success and bool(remote_result.stdout.strip())
            except (StopIteration, Exception):
                # In test mocks or error cases, assume branch doesn't exist remotely
                branch_exists_remotely = False

        # Check for branch name conflicts before attempting to create
        if create_new and not branch_exists_locally_after_fetch and not branch_exists_remotely:
            conflict = detect_branch_name_conflict(branch_name, cwd)
            if conflict:
                error_msg = (
                    f"Cannot create branch '{branch_name}': conflicts with existing branch '{conflict}'.\n\n"
                    f"Resolution options:\n"
                    f"1. Delete the conflicting branch: git branch -D {conflict}\n"
                    f"2. Use a different branch name\n"
                    f"3. If '{conflict}' contains important work, merge or back it up first"
                )
                logger.error(error_msg)
                return CommandResult(
                    success=False,
                    stdout="",
                    stderr=f"Branch name conflict: '{conflict}' exists",
                    returncode=1,
                )

        # If branch exists (either locally or remotely), switch to it instead of creating
        if branch_exists_locally_after_fetch or branch_exists_remotely:
            logger.info(f"Branch '{branch_name}' already exists, switching to it instead of creating")
            create_new = False

    # Determine checkout command based on current state
    if create_new:
        # Prefer refs/remotes/origin/<base_branch> if it exists; otherwise fall back to local <base_branch>
        origin_ref = f"refs/remotes/origin/{base_branch}"
        origin_check = cmd.run_command(["git", "rev-parse", "--verify", origin_ref], cwd=cwd)
        if origin_check.success:
            resolved_base_ref = origin_ref
        else:
            local_check = cmd.run_command(["git", "rev-parse", "--verify", str(base_branch)], cwd=cwd)
            resolved_base_ref = base_branch if local_check.success else base_branch

        assert resolved_base_ref is not None
        logger.info(f"Creating new branch '{branch_name}' from '{resolved_base_ref}'")
        checkout_cmd.extend(["-B", branch_name, str(resolved_base_ref)])  # type: ignore
    elif branch_exists_remotely and not branch_exists_locally_after_fetch:
        # Branch exists remotely but not locally, create a tracking branch
        logger.info(f"Creating tracking branch for remote branch '{branch_name}'")
        checkout_cmd.extend(["-b", branch_name, f"origin/{branch_name}"])
    else:
        # Just checkout existing branch
        checkout_cmd.append(branch_name)

    # Execute checkout
    result = cmd.run_command(checkout_cmd, cwd=cwd)

    if not result.success:
        # If checkout failed due to uncommitted changes, try to commit and retry
        if "would be overwritten by checkout" in result.stderr:
            logger.warning("Checkout failed due to uncommitted changes, attempting to commit and retry")

            # Add all changes
            add_result = cmd.run_command(["git", "add", "-A"], cwd=cwd)
            if not add_result.success:
                logger.error(f"Failed to add changes: {add_result.stderr}")
                return result

            # Commit changes
            commit_result = git_commit_with_retry(
                commit_message="WIP: Auto-commit before branch checkout (retry)",
                cwd=cwd,
                max_retries=1,
            )
            if not commit_result.success:
                logger.error(f"Failed to commit changes: {commit_result.stderr}")
                return result

            # Retry checkout
            result = cmd.run_command(checkout_cmd, cwd=cwd)
            if not result.success:
                logger.error(f"Failed to checkout branch '{branch_name}' after commit: {result.stderr}")
                return result
        else:
            logger.error(f"Failed to checkout branch '{branch_name}': {result.stderr}")
            return result

    # Verify that we're now on the expected branch
    verify_result = cmd.run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)

    if not verify_result.success:
        logger.error(f"Failed to verify current branch after checkout: {verify_result.stderr}")
        return CommandResult(
            success=False,
            stdout=result.stdout,
            stderr=f"Checkout succeeded but verification failed: {verify_result.stderr}",
            returncode=1,
        )

    # Extract branch name from output - handle cases where git includes tracking info
    # Normal output: "new-feature"
    # With tracking info: "Branch 'new-feature' set up to track remote branch 'new-feature' from 'origin'."
    output = verify_result.stdout.strip()
    if not output:
        current_branch = ""
    elif "branch '" in output.lower():
        # Extract branch name from tracking message like "Branch 'branch-name' set up to track..."
        import re

        match = re.search(r"branch\s+'([^']+)'", output, re.IGNORECASE)
        current_branch = match.group(1) if match else output.split()[0]
    else:
        # Normal case: just the branch name
        current_branch = output.split()[0]

    if current_branch != branch_name:
        error_msg = f"Branch mismatch after checkout: expected '{branch_name}', but currently on '{current_branch}'"
        logger.error(error_msg)
        return CommandResult(success=False, stdout=result.stdout, stderr=error_msg, returncode=1)

    logger.info(f"Successfully checked out branch '{branch_name}'")

    # If creating a new branch, push to remote and set up tracking
    if create_new and publish:
        logger.info(f"Publishing new branch '{branch_name}' to remote...")
        push_result = cmd.run_command(["git", "push", "-u", "origin", branch_name], cwd=cwd)
        if not push_result.success:
            logger.warning(f"Failed to push new branch to remote: {push_result.stderr}")
            # Don't exit on push failure - the branch is still created locally
        else:
            logger.info(f"Successfully published branch '{branch_name}' to remote")

    return result


def get_all_branches(cwd: Optional[str] = None, remote: bool = True) -> List[str]:
    """
    Get all branch names from the repository.

    Args:
        cwd: Optional working directory for git command
        remote: If True, include remote branches; if False, only local branches

    Returns:
        List of branch names (without remote prefix if remote is True)
    """
    cmd = CommandExecutor()
    if remote:
        result = cmd.run_command(["git", "branch", "-r", "--format=%(refname:short)"], cwd=cwd)
    else:
        result = cmd.run_command(["git", "branch", "--format=%(refname:short)"], cwd=cwd)

    if not result.success:
        logger.error(f"Failed to get branches: {result.stderr}")
        return []

    branches = [b.strip() for b in result.stdout.split("\n") if b.strip()]
    return branches


def get_branches_by_pattern(pattern: str, cwd: Optional[str] = None, remote: bool = True) -> List[str]:
    """
    Get all branches matching a specific pattern.

    Args:
        pattern: Branch name pattern to match (e.g., "pr-*", "issue-*")
        cwd: Optional working directory for git command
        remote: If True, search in remote branches; if False, only local branches

    Returns:
        List of branch names matching the pattern
    """
    all_branches = get_all_branches(cwd=cwd, remote=remote)
    matching_branches = []

    for branch in all_branches:
        # Remove remote prefix if present
        branch_name = branch.split("/", 1)[-1] if "/" in branch else branch
        # Check if branch matches the pattern (support wildcards)
        if "*" in pattern:
            # Convert glob pattern to regex
            regex_pattern = "^" + pattern.replace("*", ".*") + "$"
            if re.match(regex_pattern, branch_name, re.IGNORECASE):
                matching_branches.append(branch)
        else:
            # Exact match
            if branch_name.lower() == pattern.lower():
                matching_branches.append(branch)

    return matching_branches


def git_pull(
    remote: str = "origin",
    branch: Optional[str] = None,
    cwd: Optional[str] = None,
) -> CommandResult:
    """
    Perform git pull with comprehensive error handling and conflict resolution.

    This function centralizes git pull operations and handles various scenarios:
    - Standard pull operations
    - Merge conflicts
    - Diverging branches
    - No tracking information (new branches)

    Args:
        remote: Remote name (default: 'origin')
        branch: Optional branch name. If None, uses current branch
        cwd: Optional working directory for the git command

    Returns:
        CommandResult with the result of the pull operation
    """
    cmd = CommandExecutor()

    # Determine which branch to pull
    target_branch = branch
    if not target_branch:
        branch_result = cmd.run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
        if not branch_result.success:
            logger.warning(f"Failed to get current branch: {branch_result.stderr}")
            return CommandResult(
                success=False,
                stdout="",
                stderr=f"Failed to get current branch: {branch_result.stderr}",
                returncode=branch_result.returncode,
            )
        target_branch = branch_result.stdout.strip()

    logger.info(f"Pulling latest changes from {remote}/{target_branch}...")
    pull_result = cmd.run_command(["git", "pull", remote, target_branch], cwd=cwd)

    if pull_result.success:
        logger.info(f"Successfully pulled latest changes from {remote}/{target_branch}")
        return pull_result

    # Handle various error cases
    error_msg = pull_result.stderr.lower()

    # Check if it's a "no tracking information" error (new branch)
    if "no tracking information" in error_msg or "fatal: no such ref was fetched" in error_msg:
        logger.warning(f"No remote tracking information for branch '{target_branch}', skipping pull")
        # This is not a critical error for new branches
        return CommandResult(
            success=True,  # Treat as success for new branches
            stdout=f"No remote tracking information for branch '{target_branch}'",
            stderr=pull_result.stderr,
            returncode=0,
        )

    # Check if it's a "diverging branches" error
    if "diverging branches" in error_msg or "not possible to fast-forward" in error_msg:
        logger.info(f"Detected diverging branches for branch '{target_branch}', attempting to resolve...")

        # Try to resolve pull conflicts using our conflict resolution function
        conflict_result = resolve_pull_conflicts(cwd=cwd, merge_method="merge")

        if conflict_result.success:
            logger.info(f"Successfully resolved pull conflicts for branch '{target_branch}'")
            return CommandResult(
                success=True,
                stdout=f"Pull with conflict resolution: {conflict_result.stdout}",
                stderr=conflict_result.stderr,
                returncode=0,
            )
        else:
            logger.warning(f"Failed to resolve pull conflicts for branch '{target_branch}': {conflict_result.stderr}")
            # Return the conflict resolution result
            return conflict_result

    # Check for merge conflicts during pull
    if "conflict" in error_msg or "merge conflict" in error_msg:
        logger.info(f"Detected merge conflicts during pull, attempting to resolve...")
        conflict_result = resolve_pull_conflicts(cwd=cwd, merge_method="merge")

        if conflict_result.success:
            logger.info(f"Successfully resolved pull conflicts")
            return CommandResult(
                success=True,
                stdout=f"Pull with conflict resolution: {conflict_result.stdout}",
                stderr=conflict_result.stderr,
                returncode=0,
            )
        else:
            logger.warning(f"Failed to resolve pull conflicts: {conflict_result.stderr}")
            return conflict_result

    # Other errors
    logger.error(f"Failed to pull latest changes: {pull_result.stderr}")
    return pull_result


def resolve_pull_conflicts(cwd: Optional[str] = None, merge_method: str = "merge") -> CommandResult:
    """
    Resolve pull conflicts by attempting merge/rebase strategies.

    Args:
        cwd: Optional working directory for the git command
        merge_method: Strategy to resolve conflicts - "merge" or "rebase"

    Returns:
        CommandResult with the result of the conflict resolution
    """
    cmd = CommandExecutor()
    logger.info(f"Attempting to resolve pull conflicts using {merge_method} strategy")

    # First, abort any ongoing merge/rebase to start clean
    abort_result = cmd.run_command(["git", "merge", "--abort"], cwd=cwd)
    if not abort_result.success:
        abort_result = cmd.run_command(["git", "rebase", "--abort"], cwd=cwd)

    try:
        if merge_method == "rebase":
            # Try rebase first
            logger.info("Attempting git rebase to resolve pull conflicts")
            rebase_result = cmd.run_command(["git", "rebase", "origin/HEAD"], cwd=cwd)

            if rebase_result.success:
                logger.info("Successfully resolved pull conflicts using rebase")
                return CommandResult(
                    success=True,
                    stdout="Pull conflicts resolved via rebase",
                    stderr="",
                    returncode=0,
                )
            else:
                # If rebase fails, fall back to merge
                logger.warning(f"Rebase failed: {rebase_result.stderr}, trying merge strategy")
                return resolve_pull_conflicts(cwd, "merge")
        else:
            # Default: try merge strategy
            logger.info("Attempting git merge to resolve pull conflicts")
            merge_result = cmd.run_command(["git", "merge", "--no-ff", "origin/HEAD"], cwd=cwd)

            if merge_result.success:
                logger.info("Successfully resolved pull conflicts using merge")
                return CommandResult(
                    success=True,
                    stdout="Pull conflicts resolved via merge",
                    stderr="",
                    returncode=0,
                )
            else:
                # Check if it's actually a conflict or another error
                if "conflict" in merge_result.stderr.lower():
                    logger.error(f"Merge conflicts detected: {merge_result.stderr}")
                    # For now, return the merge result so the caller can handle conflicts
                    return merge_result
                else:
                    logger.error(f"Merge failed for non-conflict reasons: {merge_result.stderr}")
                    return merge_result

    except Exception as e:
        logger.error(f"Error during pull conflict resolution: {e}")
        return CommandResult(
            success=False,
            stdout="",
            stderr=f"Error resolving pull conflicts: {e}",
            returncode=1,
        )


def switch_to_branch(
    branch_name: str,
    create_new: bool = False,
    base_branch: Optional[str] = None,
    cwd: Optional[str] = None,
    publish: bool = True,
    pull_after_switch: bool = True,
) -> CommandResult:
    """
    Switch to a git branch and automatically pull latest changes.

    This function centralizes branch switching operations with automatic pull.
    It combines git_checkout_branch with automatic pull to ensure the branch
    is synchronized with the remote repository.

    Args:
        branch_name: Name of the branch to switch to
        create_new: If True, creates a new branch with -b flag
        base_branch: If create_new is True and base_branch is specified, creates
                     the new branch from base_branch (using -B flag)
        cwd: Optional working directory for the git command
        publish: If True and create_new is True, push the new branch to remote and set up tracking
        pull_after_switch: If True, automatically pull latest changes after successful checkout

    Returns:
        CommandResult with the result of the checkout operation.
        success=True only if checkout succeeded AND (pull succeeded if requested) AND
        current branch matches expected branch.
    """
    # First, checkout the branch using existing logic
    checkout_result = git_checkout_branch(
        branch_name=branch_name,
        create_new=create_new,
        base_branch=base_branch,
        cwd=cwd,
        publish=publish,
    )

    if not checkout_result.success:
        logger.error(f"Failed to checkout branch '{branch_name}': {checkout_result.stderr}")
        return checkout_result

    # If pull is not requested, return the checkout result
    if not pull_after_switch:
        logger.info(f"Successfully switched to branch '{branch_name}' (skipping pull)")
        return checkout_result

    # Pull the latest changes from remote
    logger.info(f"Pulling latest changes for branch '{branch_name}'...")
    pull_result = git_pull(remote="origin", branch=branch_name, cwd=cwd)

    if not pull_result.success:
        logger.error(f"Failed to pull latest changes for branch '{branch_name}': {pull_result.stderr}")
        # Return a combined result showing both the checkout and pull results
        return CommandResult(
            success=False,
            stdout=f"Checkout: {checkout_result.stdout}\nPull: {pull_result.stdout}",
            stderr=f"Checkout: {checkout_result.stderr}\nPull: {pull_result.stderr}",
            returncode=pull_result.returncode,
        )

    logger.info(f"Successfully switched to branch '{branch_name}' and pulled latest changes")
    return CommandResult(
        success=True,
        stdout=f"Checkout: {checkout_result.stdout}\nPull: {pull_result.stdout}",
        stderr=f"Checkout: {checkout_result.stderr}\nPull: {pull_result.stderr}",
        returncode=0,
    )


def migrate_pr_branches(
    config: AutomationConfig,
    cwd: Optional[str] = None,
    delete_after_merge: bool = True,
    force: bool = False,
    execute: bool = False,
) -> Dict[str, Any]:
    """
    Migrate existing pr-<number> branches to their corresponding issue-<number> branches.

    This function:
    1. Scans for all branches matching the pr-<number> pattern
    2. For each pr-xx branch, checks if an issue-xx branch exists
    3. If issue-xx exists, merges pr-xx into issue-xx
    4. If issue-xx doesn't exist, creates it from pr-xx
    5. Deletes the pr-xx branch after successful merge (if delete_after_merge is True)

    Args:
        config: AutomationConfig instance
        cwd: Optional working directory for git command
        delete_after_merge: If True, delete pr-<number> branch after successful merge
        force: If True, proceed even if there are merge conflicts
        execute: If True, perform actual migration. If False, only preview what would be done.

    Returns:
        Dictionary with migration results:
        - 'success': Overall success status
        - 'migrated': List of successfully migrated branches
        - 'skipped': List of skipped branches with reasons
        - 'failed': List of failed migrations with error messages
        - 'conflicts': List of branches with merge conflicts
    """
    cmd = CommandExecutor()
    results: Dict[str, Any] = {
        "success": True,
        "migrated": [],
        "skipped": [],
        "failed": [],
        "conflicts": [],
    }

    mode = "EXECUTE" if execute else "DRY-RUN"
    logger.info(f"Starting branch migration ({mode} mode, delete_after_merge={delete_after_merge})")

    # Get all pr-<number> branches
    pr_branches = get_branches_by_pattern("pr-*", cwd=cwd, remote=False)

    if not pr_branches:
        logger.info("No pr-<number> branches found")
        return results

    logger.info(f"Found {len(pr_branches)} pr-<number> branch(es): {', '.join(pr_branches)}")

    for pr_branch in pr_branches:
        # Remove local branch prefix
        branch_name = pr_branch.split("/", 1)[-1] if "/" in pr_branch else pr_branch

        # Extract number from pr-<number> branch
        pr_number = extract_number_from_branch(branch_name)
        if pr_number is None:
            logger.warning(f"Could not extract number from branch '{branch_name}', skipping")
            results["skipped"].append({"branch": branch_name, "reason": "Could not extract issue number"})
            continue

        # Determine corresponding issue-<number> branch name
        issue_branch_name = f"issue-{pr_number}"

        logger.info(f"Processing: {branch_name} -> {issue_branch_name}")

        # Actual migration
        try:
            # Check if we're already on the branch we want to migrate
            current_branch = get_current_branch(cwd=cwd)
            if current_branch == branch_name:
                # Switch to a safe branch first
                logger.info(f"Currently on {branch_name}, switching to main before migration")
                if execute:
                    switch_result = cmd.run_command(["git", "checkout", "main"], cwd=cwd)
                    if not switch_result.success:
                        # Try main as fallback
                        switch_result = cmd.run_command(["git", "checkout", "refs/remotes/origin/main"], cwd=cwd)
                else:
                    logger.info(f"[DRY-RUN] Would switch from {branch_name} to main")

            # Check if issue-<number> branch exists
            if branch_exists(issue_branch_name, cwd=cwd):
                # Issue branch exists, perform merge
                logger.info(f"Issue branch '{issue_branch_name}' exists, merging {branch_name}")

                if execute:
                    # Switch to issue branch
                    checkout_result = git_checkout_branch(issue_branch_name, create_new=False, cwd=cwd)
                    if not checkout_result.success:
                        error_msg = f"Failed to checkout issue branch '{issue_branch_name}': {checkout_result.stderr}"
                        logger.error(error_msg)
                        results["failed"].append(
                            {
                                "from": branch_name,
                                "to": issue_branch_name,
                                "error": error_msg,
                            }
                        )
                        results["success"] = False
                        continue

                    # Pull latest changes from issue branch
                    logger.info(f"Pulling latest changes for {issue_branch_name}")
                    pull_result = git_pull(remote="origin", branch=issue_branch_name, cwd=cwd)
                    if not pull_result.success:
                        logger.warning(f"Failed to pull latest changes for {issue_branch_name}: {pull_result.stderr}")
                else:
                    logger.info(f"[DRY-RUN] Would checkout and merge {branch_name} into {issue_branch_name}")

                # Merge pr branch
                logger.info(f"Merging {branch_name} into {issue_branch_name}")
                if execute:
                    merge_result = cmd.run_command(
                        [
                            "git",
                            "merge",
                            (f"origin/{branch_name}" if "/" not in branch_name else branch_name),
                            "--no-ff",
                            "-m",
                            f"Merge {branch_name} into {issue_branch_name}",
                        ],
                        cwd=cwd,
                    )

                    if not merge_result.success:
                        # Check if it's a merge conflict
                        if "conflict" in merge_result.stderr.lower():
                            logger.error(f"Merge conflict detected while merging {branch_name} into {issue_branch_name}")
                            results["conflicts"].append(
                                {
                                    "from": branch_name,
                                    "to": issue_branch_name,
                                    "error": merge_result.stderr,
                                }
                            )

                            if not force:
                                # Abort the merge and skip
                                cmd.run_command(["git", "merge", "--abort"], cwd=cwd)
                                logger.info(f"Aborted merge, skipping {branch_name}")
                                results["skipped"].append(
                                    {
                                        "from": branch_name,
                                        "to": issue_branch_name,
                                        "reason": "Merge conflict (use --force to auto-resolve)",
                                    }
                                )
                                results["success"] = False
                                continue
                            else:
                                # Try to auto-resolve conflicts
                                logger.info(f"Attempting to auto-resolve conflicts for {branch_name}")
                                add_result = cmd.run_command(["git", "add", "-A"], cwd=cwd)
                                if add_result.success:
                                    commit_result = git_commit_with_retry(f"Resolve conflicts from {branch_name}", cwd=cwd)
                                    if not commit_result.success:
                                        error_msg = f"Failed to commit conflict resolution: {commit_result.stderr}"
                                        logger.error(error_msg)
                                        results["failed"].append(
                                            {
                                                "from": branch_name,
                                                "to": issue_branch_name,
                                                "error": error_msg,
                                            }
                                        )
                                        results["success"] = False
                                        continue
                                else:
                                    error_msg = f"Failed to stage conflict resolution: {add_result.stderr}"
                                    logger.error(error_msg)
                                    results["failed"].append(
                                        {
                                            "from": branch_name,
                                            "to": issue_branch_name,
                                            "error": error_msg,
                                        }
                                    )
                                    results["success"] = False
                                    continue
                        else:
                            # Non-conflict error
                            error_msg = f"Merge failed: {merge_result.stderr}"
                            logger.error(error_msg)
                            results["failed"].append(
                                {
                                    "from": branch_name,
                                    "to": issue_branch_name,
                                    "error": error_msg,
                                }
                            )
                            results["success"] = False
                            continue

                    # Push the merged changes
                    push_result = git_push(
                        cwd=cwd,
                        commit_message=f"Merged {branch_name} into {issue_branch_name}",
                    )
                    if not push_result.success:
                        logger.warning(f"Failed to push merged changes: {push_result.stderr}")
                        # Don't fail the entire migration for push issues
                else:
                    if force:
                        logger.info(f"[DRY-RUN] Would merge {branch_name} into {issue_branch_name} (with --force auto-resolve)")
                    else:
                        logger.info(f"[DRY-RUN] Would merge {branch_name} into {issue_branch_name}")
                    logger.info(f"[DRY-RUN] Would push merged changes to origin")
            else:
                # Issue branch doesn't exist, rename pr branch to issue branch
                logger.info(f"Issue branch '{issue_branch_name}' does not exist, creating from {branch_name}")

                if execute:
                    # Get the commit hash of pr branch
                    rev_result = cmd.run_command(["git", "rev-parse", branch_name], cwd=cwd)
                    if not rev_result.success:
                        error_msg = f"Failed to get commit hash for {branch_name}: {rev_result.stderr}"
                        logger.error(error_msg)
                        results["failed"].append(
                            {
                                "from": branch_name,
                                "to": issue_branch_name,
                                "error": error_msg,
                            }
                        )
                        results["success"] = False
                        continue

                    # Create new issue branch from pr branch
                    checkout_result = git_checkout_branch(
                        issue_branch_name,
                        create_new=True,
                        base_branch=branch_name,
                        cwd=cwd,
                    )
                    if not checkout_result.success:
                        error_msg = f"Failed to create issue branch '{issue_branch_name}': {checkout_result.stderr}"
                        logger.error(error_msg)
                        results["failed"].append(
                            {
                                "from": branch_name,
                                "to": issue_branch_name,
                                "error": error_msg,
                            }
                        )
                        results["success"] = False
                        continue

                    # Push the new branch
                    push_result = git_push(
                        cwd=cwd,
                        commit_message=f"Created {issue_branch_name} from {branch_name}",
                    )
                    if not push_result.success:
                        logger.warning(f"Failed to push new branch: {push_result.stderr}")
                        # Don't fail the entire migration for push issues
                else:
                    logger.info(f"[DRY-RUN] Would create new branch '{issue_branch_name}' from {branch_name}")
                    logger.info(f"[DRY-RUN] Would push new branch to origin")

            # Delete pr branch after successful migration
            if delete_after_merge:
                logger.info(f"Deleting pr branch '{branch_name}'")
                if execute:
                    delete_result = cmd.run_command(["git", "branch", "-D", branch_name], cwd=cwd)
                    if delete_result.success:
                        # Also delete from remote
                        cmd.run_command(["git", "push", "origin", "--delete", branch_name], cwd=cwd)
                        logger.info(f"Successfully deleted pr branch '{branch_name}'")
                    else:
                        logger.warning(f"Failed to delete local pr branch '{branch_name}': {delete_result.stderr}")
                else:
                    logger.info(f"[DRY-RUN] Would delete pr branch '{branch_name}' (local and remote)")

            if execute:
                logger.info(f"Successfully migrated {branch_name} -> {issue_branch_name}")
            else:
                logger.info(f"[DRY-RUN] Would mark as migrated: {branch_name} -> {issue_branch_name}")
            results["migrated"].append({"from": branch_name, "to": issue_branch_name})

        except Exception as e:
            error_msg = f"Unexpected error during migration: {e}"
            logger.error(error_msg)
            results["failed"].append({"from": branch_name, "to": issue_branch_name, "error": str(e)})
            results["success"] = False

    logger.info(f"Branch migration completed. Migrated: {len(results['migrated'])}, Skipped: {len(results['skipped'])}, Failed: {len(results['failed'])}")
    return results


@contextmanager
def branch_context(
    branch_name: str,
    create_new: bool = False,
    base_branch: Optional[str] = None,
    cwd: Optional[str] = None,
    check_unpushed: bool = True,
    remote: str = "origin",
) -> Generator[None, None, None]:
    """
    Context manager for Git branch management.

    This context manager automatically switches to the specified branch on entry,
    checks for unpushed commits, and returns to the main branch on exit (even if
    an exception occurs).

    Branch Conflict Handling:
        When create_new=True, this context manager leverages git_checkout_branch()
        which automatically detects and prevents Git ref namespace conflicts.
        If a conflict is detected (e.g., trying to create 'issue-699/attempt-1'
        when 'issue-699' already exists), a RuntimeError is raised with a clear
        error message indicating the conflicting branch and resolution steps.

        The conflict detection prevents the following scenarios:
        - Creating 'branch/name' when 'branch' exists
        - Creating 'branch' when 'branch/*' exists

        See git_checkout_branch() and detect_branch_name_conflict() for details.

    Args:
        branch_name: Name of the branch to switch to
        create_new: If True, creates a new branch with -b flag
        base_branch: If create_new is True and base_branch is specified, creates
                     the new branch from base_branch (using -B flag)
        cwd: Optional working directory for the git command
        check_unpushed: If True, automatically check and push unpushed commits
                       on entry (default: True)
        remote: Remote name to use for unpushed commit checks (default: 'origin')

    Example Usage:
        # Work on a feature branch
        with branch_context("feature/issue-123"):
            # Perform work on feature/issue-123 branch
            # Branch is automatically pulled on entry
            # Unpushed commits are automatically pushed
            perform_work()
        # Automatically back on main branch after exiting context

        # Create and work on new branch (with automatic conflict detection)
        with branch_context("feature/new-feature", create_new=True, base_branch="main"):
            # New branch created from main
            # Automatic pull after switch
            # Unpushed commits are automatically pushed
            # Raises RuntimeError if branch name conflicts detected
            perform_work()
        # Automatically returns to main

    Raises:
        RuntimeError: If branch creation fails due to naming conflicts or other branch operations
        Exception: Propagates any exceptions from branch operations
    """
    from .git_commit import ensure_pushed
    from .git_info import is_git_repository

    # Store the current branch to return to on exit
    original_branch = get_current_branch(cwd=cwd)

    if not original_branch:
        raise RuntimeError("Failed to get current branch before switching")

    # If already on the target branch, just yield without switching
    if original_branch == branch_name and not create_new:
        logger.info(f"Already on branch '{branch_name}', staying on current branch")
        try:
            yield
        finally:
            # Even if we're already on the branch, still need to handle cleanup properly
            pass
        return

    try:
        # On entry: switch to the target branch with automatic pull
        logger.info(f"Switching to branch '{branch_name}'")
        switch_result = switch_to_branch(
            branch_name=branch_name,
            create_new=create_new,
            base_branch=base_branch,
            cwd=cwd,
            publish=True,  # Default to publishing new branches
            pull_after_switch=True,  # Always pull after switch
        )

        if not switch_result.success:
            raise RuntimeError(f"Failed to switch to branch '{branch_name}': {switch_result.stderr}")

        # Check for and push unpushed commits if requested
        if check_unpushed:
            try:
                # Import ProgressStage here to avoid circular imports
                from .progress_footer import ProgressStage

                with ProgressStage("Checking unpushed commits"):
                    logger.info("Checking for unpushed commits before processing...")
                    push_result = ensure_pushed(cwd=cwd, remote=remote)
                    if push_result.success and "No unpushed commits" not in push_result.stdout:
                        logger.info("Successfully pushed unpushed commits")
                    elif not push_result.success:
                        logger.warning(f"Failed to push unpushed commits: {push_result.stderr}")
            except ImportError:
                # ProgressStage not available, just check and push without progress indicator
                logger.info("Checking for unpushed commits before processing...")
                push_result = ensure_pushed(cwd=cwd, remote=remote)
                if push_result.success and "No unpushed commits" not in push_result.stdout:
                    logger.info("Successfully pushed unpushed commits")
                elif not push_result.success:
                    logger.warning(f"Failed to push unpushed commits: {push_result.stderr}")

        # Yield control to the with block
        yield

    finally:
        # On exit: always return to the original branch
        # First, check if we're still in a git repository
        if is_git_repository(cwd):
            # Check if the current branch is different from the original
            current_branch = get_current_branch(cwd=cwd)

            if current_branch != original_branch:
                logger.info(f"Returning to original branch '{original_branch}'")
                return_result = switch_to_branch(
                    branch_name=original_branch,
                    cwd=cwd,
                    pull_after_switch=True,  # Always pull after switch
                )

                if not return_result.success:
                    logger.warning(f"Failed to return to branch '{original_branch}': {return_result.stderr}")
                    # Don't raise here - we're in cleanup mode
            else:
                logger.info(f"Already on branch '{original_branch}', no need to switch back")
        else:
            logger.warning("Not in a git repository during cleanup, cannot return to original branch")
