"""
Git branch management utilities for Auto-Coder.

This module contains functions for managing git branches, including creating,
checking out, and validating branch names.
"""

import re
from typing import Any, Dict, List, Optional

from auto_coder.backend_manager import run_llm_prompt

from .git_info import check_unpushed_commits, get_current_branch
from .logger_config import get_logger
from .prompt_loader import render_prompt
from .utils import CommandExecutor, CommandResult

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
                    return CommandResult(success=True, stdout="No changes to commit after LLM fix", stderr="", returncode=0)
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

    # Build checkout command (enforce correct base for new branches)
    checkout_cmd: List[str] = ["git", "checkout"]
    resolved_base_ref: Optional[str] = None
    if create_new:
        if base_branch is None:
            # Fail fast to detect incorrect call sites
            raise ValueError("When create_new=True, base_branch must be provided (e.g., 'main').")

        # Always fetch latest refs before creating a new branch
        logger.info("Fetching 'origin' with --prune --tags before creating new branch...")
        cmd.run_command(["git", "fetch", "origin", "--prune", "--tags"], cwd=cwd)

        # Prefer refs/remotes/origin/<base_branch> if it exists; otherwise fall back to local <base_branch>
        origin_ref = f"refs/remotes/origin/{base_branch}"
        origin_check = cmd.run_command(["git", "rev-parse", "--verify", origin_ref], cwd=cwd)
        if origin_check.success:
            resolved_base_ref = origin_ref
        else:
            local_check = cmd.run_command(["git", "rev-parse", "--verify", base_branch], cwd=cwd)
            resolved_base_ref = base_branch if local_check.success else base_branch

        logger.info(f"Creating new branch '{branch_name}' from '{resolved_base_ref}'")
        checkout_cmd.extend(["-B", branch_name, resolved_base_ref])
    else:
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

    current_branch = verify_result.stdout.strip()
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
