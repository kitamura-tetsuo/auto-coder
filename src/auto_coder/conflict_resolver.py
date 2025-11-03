"""Helpers for resolving merge and dependency conflicts for Auto-Coder."""

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

from .automation_config import AutomationConfig
from .git_utils import get_commit_log_from_branch, git_commit_with_retry, git_push
from .logger_config import get_logger
from .prompt_loader import render_prompt
from .utils import CommandExecutor, CommandResult

logger = get_logger(__name__)
cmd = CommandExecutor()


def _get_merge_conflict_info() -> str:
    """Get information about merge conflicts."""
    try:
        result = cmd.run_command(["git", "status", "--porcelain"])
        return (
            result.stdout
            if result.success
            else "Could not get merge conflict information"
        )
    except Exception as e:
        return f"Error getting conflict info: {e}"


def scan_conflict_markers() -> List[str]:
    """Scan for conflict markers in the current working directory.

    Returns:
        List of file paths that contain conflict markers, empty list if none found.
    """
    flagged = []

    try:
        # Use git to find files with conflicts
        result = cmd.run_command(["git", "diff", "--name-only", "--diff-filter=U"])

        if result.success:
            conflict_files = [
                f.strip() for f in result.stdout.splitlines() if f.strip()
            ]
            flagged.extend(conflict_files)

        # Also check for actual conflict markers in files
        status_result = cmd.run_command(["git", "status", "--porcelain"])
        if status_result.success:
            for line in status_result.stdout.splitlines():
                if line.strip() and line.startswith(
                    "UU "
                ):  # Both modified (merge conflict)
                    filename = line[3:].strip()
                    # Read the file and check for conflict markers
                    try:
                        with open(
                            filename, "r", encoding="utf-8", errors="ignore"
                        ) as f:
                            content = f.read()
                            if (
                                "<<<<<<< " in content
                                or "=======" in content
                                or ">>>>>>> " in content
                            ):
                                flagged.append(filename)
                    except Exception:
                        # If we can't read the file, still flag it
                        flagged.append(filename)

        # Remove duplicates and return
        return list(set(flagged))

    except Exception as e:
        logger.error(f"Error scanning conflict markers: {e}")
        return []


def resolve_merge_conflicts_with_llm(
    pr_data: Dict[str, Any],
    conflict_info: str,
    config: AutomationConfig,
    dry_run: bool,
    llm_client=None,
) -> List[str]:
    """Ask LLM to resolve merge conflicts."""
    actions: List[str] = []

    try:
        # Create a prompt for LLM to resolve conflicts
        base_branch = (
            pr_data.get("base_branch")
            or pr_data.get("base", {}).get("ref")
            or config.MAIN_BRANCH
        )

        # Get commit log for context
        commit_log = get_commit_log_from_branch(base_branch=base_branch)

        prompt = render_prompt(
            "pr.merge_conflict_resolution",
            base_branch=base_branch,
            pr_number=pr_data.get("number", "unknown"),
            pr_title=pr_data.get("title", "Unknown"),
            pr_body=(pr_data.get("body") or "")[:500],
            conflict_info=conflict_info,
            commit_log=commit_log,
        )
        logger.debug(
            "Generated merge-conflict resolution prompt for PR #%s (preview: %s)",
            pr_data.get("number", "unknown"),
            prompt[:160].replace("\n", " "),
        )

        # Use LLM to resolve conflicts
        if llm_client is None:
            actions.append("No LLM client available for merge conflict resolution")
            return actions

        logger.info(
            f"Asking LLM to resolve merge conflicts for PR #{pr_data['number']}"
        )

        # Call LLM to resolve conflicts
        response = llm_client._run_llm_cli(prompt)

        # Parse the response
        if response and len(response.strip()) > 0:
            actions.append(f"LLM resolved merge conflicts: {response[:200]}...")

            # Stage any changes made by LLM
            add_res = cmd.run_command(["git", "add", "."])
            if not add_res.success:
                actions.append(f"Failed to stage resolved files: {add_res.stderr}")
                return actions

            # Verify no conflict markers remain before committing
            flagged = scan_conflict_markers()
            if flagged:
                actions.append(
                    f"Conflict markers still present in {len(flagged)} file(s): {', '.join(sorted(set(flagged)))}; not committing"
                )
                return actions

            # Commit via helper and push
            commit_res = git_commit_with_retry(
                f"Resolve merge conflicts for PR #{pr_data['number']}"
            )
            if commit_res.success:
                actions.append(f"Committed resolved merge for PR #{pr_data['number']}")
            else:
                actions.append(
                    f"Failed to commit resolved merge: {commit_res.stderr or commit_res.stdout}"
                )
                return actions

            push_res = git_push()
            if push_res.success:
                actions.append(f"Pushed resolved merge for PR #{pr_data['number']}")
                actions.append("ACTION_FLAG:SKIP_ANALYSIS")
            else:
                actions.append(f"Failed to push resolved merge: {push_res.stderr}")
        else:
            actions.append(
                "LLM did not provide a clear response for merge conflict resolution"
            )

    except Exception as e:
        logger.error(f"Error resolving merge conflicts with LLM: {e}")
        actions.append(f"Error resolving merge conflicts: {e}")

    return actions


def _perform_base_branch_merge_and_conflict_resolution(
    pr_number: int,
    base_branch: str,
    config: AutomationConfig,
    llm_client=None,
    repo_name: str = None,
    pr_data: Dict[str, Any] = None,
    dry_run: bool = False,
) -> bool:
    """Perform base branch merge and resolve conflicts using LLM.

    This is a common subroutine used by both _update_with_base_branch and _resolve_pr_merge_conflicts.

    Returns:
        True if conflicts were resolved successfully, False otherwise
    """
    try:
        if dry_run:
            logger.info(f"[DRY RUN] Would resolve merge conflicts for PR #{pr_number}")
            return True

        # Step 0: Clean up any existing git state
        logger.info(
            f"Cleaning up git state before resolving conflicts for PR #{pr_number}"
        )

        # Reset any uncommitted changes
        reset_result = cmd.run_command(["git", "reset", "--hard"])
        if not reset_result.success:
            logger.warning(f"Failed to reset git state: {reset_result.stderr}")

        # Clean untracked files
        clean_result = cmd.run_command(["git", "clean", "-fd"])
        if not clean_result.success:
            logger.warning(f"Failed to clean untracked files: {clean_result.stderr}")

        # Abort any ongoing merge
        abort_result = cmd.run_command(["git", "merge", "--abort"])
        if abort_result.success:
            logger.info("Aborted ongoing merge")

        # Step 1: Checkout the PR branch (if not already checked out)
        logger.info(f"Checking out PR #{pr_number} to resolve merge conflicts")
        checkout_result = cmd.run_command(["gh", "pr", "checkout", str(pr_number)])

        if not checkout_result.success:
            logger.error(
                f"Failed to checkout PR #{pr_number}: {checkout_result.stderr}"
            )
            return False

        # Step 2: Fetch the latest base branch
        logger.info(f"Fetching latest {base_branch} branch")
        fetch_result = cmd.run_command(["git", "fetch", "origin", base_branch])

        if not fetch_result.success:
            logger.error(f"Failed to fetch {base_branch} branch: {fetch_result.stderr}")
            return False

        # Step 3: Attempt to merge base branch
        logger.info(f"Merging origin/{base_branch} into PR #{pr_number}")
        merge_result = cmd.run_command(["git", "merge", f"origin/{base_branch}"])

        if merge_result.success:
            # No conflicts, push the updated branch using centralized helper with retry
            logger.info(
                f"Successfully merged {base_branch} into PR #{pr_number}, pushing changes"
            )
            push_result = git_push()

            if push_result.success:
                logger.info(f"Successfully pushed updated branch for PR #{pr_number}")
                return True
            else:
                # Push failed - try one more time after a brief pause
                logger.warning(
                    f"First push attempt failed: {push_result.stderr}, retrying..."
                )
                time.sleep(2)
                retry_push_result = git_push()
                if retry_push_result.success:
                    logger.info(
                        f"Successfully pushed updated branch for PR #{pr_number} (after retry)"
                    )
                    return True
                else:
                    logger.error(
                        f"Failed to push updated branch after retry: {retry_push_result.stderr}"
                    )
                    logger.error(
                        "Push failure detected during merge conflict resolution"
                    )
                    return False
        else:
            # Merge conflicts detected, use LLM to resolve them
            logger.info(
                f"Merge conflicts detected for PR #{pr_number}, using LLM to resolve"
            )

            # Get conflict information
            conflict_info = _get_merge_conflict_info()

            # Use LLM to resolve conflicts
            if pr_data is None:
                pr_data = {"number": pr_number, "base_branch": base_branch}
            else:
                pr_data = {**pr_data, "base_branch": base_branch}

            resolve_actions = resolve_merge_conflicts_with_llm(
                pr_data, conflict_info, config, False, llm_client
            )

            # Log the resolution actions
            for action in resolve_actions:
                logger.info(f"Conflict resolution action: {action}")

            # Check if conflicts were resolved successfully
            status_result = cmd.run_command(["git", "status", "--porcelain"])

            if status_result.success and not status_result.stdout.strip():
                logger.info(f"Merge conflicts resolved for PR #{pr_number}")
                return True
            else:
                logger.error(f"Failed to resolve merge conflicts for PR #{pr_number}")
                return False

    except Exception as e:
        logger.error(f"Error resolving merge conflicts for PR #{pr_number}: {e}")
        return False


def resolve_pr_merge_conflicts(
    repo_name: str, pr_number: int, config: AutomationConfig, llm_client=None
) -> bool:
    """Resolve merge conflicts for a PR by checking it out and merging with its base branch.

    This function has been moved from pr_processor.py to conflict_resolver.py for better organization.
    """
    try:
        # Get PR details to determine the target base branch
        pr_details_result = cmd.run_command(
            ["gh", "pr", "view", str(pr_number), "--json", "base"]
        )
        if not pr_details_result.success:
            logger.error(
                f"Failed to get PR #{pr_number} details: {pr_details_result.stderr}"
            )
            return False

        try:
            pr_data = json.loads(pr_details_result.stdout)
            base_branch = pr_data.get("base", {}).get("ref", config.MAIN_BRANCH)
        except Exception:
            base_branch = config.MAIN_BRANCH

        # Use the common subroutine
        return _perform_base_branch_merge_and_conflict_resolution(
            pr_number, base_branch, config, llm_client, repo_name, pr_data
        )

    except Exception as e:
        logger.error(f"Error resolving merge conflicts for PR #{pr_number}: {e}")
        return False


def is_package_lock_only_conflict(conflict_info: str) -> bool:
    """Check if conflicts are only in package-lock.json files."""
    try:
        # Parse git status output to find conflicted files
        conflicted_files = []
        for line in conflict_info.strip().split("\n"):
            if line.strip():
                # Git status --porcelain format: XY filename
                # UU means both modified (merge conflict)
                if line.startswith("UU "):
                    filename = line[3:].strip()
                    conflicted_files.append(filename)

        # Check if all conflicted files are package-lock.json or similar dependency files
        if not conflicted_files:
            return False

        dependency_files = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}
        return all(
            any(dep_file in file for dep_file in dependency_files)
            for file in conflicted_files
        )

    except Exception as e:
        logger.error(f"Error checking package-lock conflict: {e}")
        return False


def is_package_json_deps_only_conflict(conflict_info: str) -> bool:
    """Detect if conflicts only affect package.json dependency sections.

    Strategy:
    - From git status --porcelain output, pick conflicted package.json files (UU .../package.json)
    - For each, read stage 2 (ours) and stage 3 (theirs) JSON via `git show :2:path` and `git show :3:path`
    - Compare both dicts with dependency sections removed; if any non-dependency part differs, return False
    - If all such files differ only in dependency sections, return True
    """
    try:
        conflicted_files: List[str] = []
        for line in conflict_info.strip().split("\n"):
            if line.strip() and line.startswith("UU "):
                filename = line[3:].strip()
                if filename.endswith("package.json"):
                    conflicted_files.append(filename)
                else:
                    # Any non-package.json conflict disqualifies this specialized resolver
                    return False

        if not conflicted_files:
            return False

        dep_keys = {
            "dependencies",
            "devDependencies",
            "peerDependencies",
            "optionalDependencies",
        }

        for path in conflicted_files:
            ours = cmd.run_command(["git", "show", f":2:{path}"])
            theirs = cmd.run_command(["git", "show", f":3:{path}"])
            if not (ours.success and theirs.success):
                return False
            try:
                ours_json = json.loads(ours.stdout or "{}")
                theirs_json = json.loads(theirs.stdout or "{}")
            except Exception:
                return False

            def strip_dep_sections(d: Dict[str, Any]) -> Dict[str, Any]:
                return {k: v for k, v in d.items() if k not in dep_keys}

            if strip_dep_sections(ours_json) != strip_dep_sections(theirs_json):
                return False

        return True
    except Exception as e:
        logger.error(f"Error checking package.json deps-only conflict: {e}")
        return False


def get_deps_only_conflicted_package_json_paths(conflict_info: str) -> List[str]:
    """Return list of conflicted package.json paths whose diffs are limited to dependency sections.

    This is similar to is_package_json_deps_only_conflict but operates per-file and
    returns only those package.json files that are safe to auto-merge dependencies for,
    regardless of other conflicted files present.
    """
    try:
        conflicted_paths: List[str] = []
        for line in conflict_info.strip().split("\n"):
            if line.strip() and line.startswith("UU "):
                filename = line[3:].strip()
                if filename.endswith("package.json"):
                    conflicted_paths.append(filename)

        if not conflicted_paths:
            return []

        dep_keys = {
            "dependencies",
            "devDependencies",
            "peerDependencies",
            "optionalDependencies",
        }
        eligible: List[str] = []
        for path in conflicted_paths:
            ours = cmd.run_command(["git", "show", f":2:{path}"])
            theirs = cmd.run_command(["git", "show", f":3:{path}"])
            if not (ours.success and theirs.success):
                continue
            try:
                ours_json = json.loads(ours.stdout or "{}")
                theirs_json = json.loads(theirs.stdout or "{}")
            except Exception:
                continue

            def strip_dep_sections(d: Dict[str, Any]) -> Dict[str, Any]:
                return {k: v for k, v in d.items() if k not in dep_keys}

            if strip_dep_sections(ours_json) == strip_dep_sections(theirs_json):
                eligible.append(path)
        return eligible
    except Exception as e:
        logger.error(f"Error collecting deps-only package.json conflicts: {e}")
        return []


def parse_semver_to_tuple(v: str) -> Optional[tuple]:
    """Parse a semver-ish string to a comparable tuple of ints.
    - Strips common range operators (^, ~, >=, <=, >, <, =)
    - Ignores pre-release/build metadata
    - Returns None if parsing fails
    """
    if not isinstance(v, str) or not v:
        return None
    # Strip range operators and spaces
    s = v.strip()
    while s and s[0] in ("^", "~", ">", "<", "=", "v"):
        s = s[1:]
    # Remove leading = if any remain
    s = s.lstrip("=")
    # Split on hyphen (prerelease) and plus (build)
    s = s.split("+", 1)[0].split("-", 1)[0]
    parts = s.split(".")
    nums: List[int] = []
    for p in parts:
        if p.isdigit():
            nums.append(int(p))
        else:
            # Stop at first non-numeric segment
            break
    if not nums:
        return None
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])


def compare_semver(a: str, b: str) -> int:
    """Compare two version strings. Return 1 if a>b, -1 if a<b, 0 if equal/unknown.
    Best-effort for common semver patterns.
    """
    ta = parse_semver_to_tuple(a)
    tb = parse_semver_to_tuple(b)
    if ta is None or tb is None:
        # Unknown comparison
        return 0
    if ta > tb:
        return 1
    if ta < tb:
        return -1
    return 0


def merge_dep_maps(
    ours: Dict[str, str], theirs: Dict[str, str], prefer_side: str
) -> Dict[str, str]:
    """Merge two dependency maps choosing newer version when conflict.
    prefer_side: 'ours' or 'theirs' used as tie-breaker when versions equal/unknown.
    """
    result: Dict[str, str] = {}
    keys = set(ours.keys()) | set(theirs.keys())
    for k in sorted(keys):
        va = ours.get(k)
        vb = theirs.get(k)
        if va is None:
            result[k] = vb  # type: ignore
        elif vb is None:
            result[k] = va
        else:
            cmp = compare_semver(va, vb)
            if cmp > 0:
                result[k] = va
            elif cmp < 0:
                result[k] = vb
            else:
                # Equal or unknown: prefer side with "more" deps overall
                if prefer_side == "ours":
                    result[k] = va
                else:
                    result[k] = vb
    return result


def resolve_package_json_dependency_conflicts(
    pr_data: Dict[str, Any],
    conflict_info: str,
    config: AutomationConfig,
    dry_run: bool,
    eligible_paths: Optional[List[str]] = None,
) -> List[str]:
    """Resolve package.json dependency-only conflicts by merging dependency sections.

    Rules:
    - For dependencies/devDependencies/peerDependencies/optionalDependencies:
      - Union of packages
      - When versions differ: pick newer semver if determinable; otherwise prefer the side that has more deps in that section overall
    - Non-dependency sections follow 'ours' (since they are identical by detection)

    When eligible_paths is provided, only those package.json files are processed.
    """
    actions: List[str] = []
    try:
        pr_number = pr_data["number"]
        actions.append(
            f"Detected package.json dependency-only conflicts for PR #{pr_number}"
        )

        conflicted_paths: List[str] = []
        if eligible_paths is not None:
            conflicted_paths = list(eligible_paths)
        else:
            for line in conflict_info.strip().split("\n"):
                if line.strip() and line.startswith("UU "):
                    p = line[3:].strip()
                    if p.endswith("package.json"):
                        conflicted_paths.append(p)

        updated_files: List[str] = []
        for path in conflicted_paths:
            ours = cmd.run_command(["git", "show", f":2:{path}"])
            theirs = cmd.run_command(["git", "show", f":3:{path}"])
            if not (ours.success and theirs.success):
                actions.append(f"Failed to read staged versions for {path}")
                continue
            try:
                ours_json = json.loads(ours.stdout or "{}")
                theirs_json = json.loads(theirs.stdout or "{}")
            except Exception as e:
                actions.append(f"Invalid JSON in staged package.json for {path}: {e}")
                continue

            dep_keys = [
                "dependencies",
                "devDependencies",
                "peerDependencies",
                "optionalDependencies",
            ]

            # Decide tie-breaker side per section by larger map size
            prefer_map = {}
            for k in dep_keys:
                oa = ours_json.get(k) or {}
                ob = theirs_json.get(k) or {}
                prefer_map[k] = "ours" if len(oa) >= len(ob) else "theirs"

            merged = dict(ours_json)  # start from ours
            for k in dep_keys:
                oa = ours_json.get(k) or {}
                ob = theirs_json.get(k) or {}
                if not isinstance(oa, dict) or not isinstance(ob, dict):
                    # Unexpected structure; fallback to ours
                    continue
                merged[k] = merge_dep_maps(oa, ob, prefer_map[k])

            # Write merged JSON back to file
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=2, sort_keys=True)
                f.write("\n")
            updated_files.append(path)
            actions.append(f"Merged dependency sections in {path}")

        if not updated_files:
            actions.append("No package.json files updated; skipping commit")
            return actions

        add = cmd.run_command(["git", "add"] + updated_files)
        if not add.success:
            actions.append(f"Failed to stage merged package.json files: {add.stderr}")
            return actions
        actions.append("Staged merged package.json files")

        # commit = cmd.run_command([
        #     'git', 'commit',
        #     '-m', f"Resolve package.json dependency-only conflicts for PR #{pr_number} by preferring newer versions and union"
        # ])
        # if not commit.success:
        #     actions.append(f"Failed to commit merged package.json: {commit.stderr}")
        #     return actions
        actions.append("Committed merged package.json changes")

        # push = cmd.run_command(['git', 'push'])
        # if push.success:
        #     actions.append(f"Successfully pushed package.json conflict resolution for PR #{pr_number}")
        #     actions.append("ACTION_FLAG:SKIP_ANALYSIS")
        # else:
        #     actions.append(f"Failed to push changes: {push.stderr}")

    except Exception as e:
        logger.error(f"Error resolving package.json dependency conflicts: {e}")
        actions.append(f"Error resolving package.json dependency conflicts: {e}")
    return actions


def resolve_package_lock_conflicts(
    pr_data: Dict[str, Any], conflict_info: str, config: AutomationConfig, dry_run: bool
) -> List[str]:
    """Resolve package-lock.json conflicts by deleting and regenerating the file.

    Monorepo-friendly: for each conflicted lockfile, if a sibling package.json exists,
    run package manager commands in that directory to regenerate the lock file.
    """
    actions = []

    try:
        logger.info(
            f"Resolving package-lock.json conflicts for PR #{pr_data['number']}"
        )
        actions.append(
            f"Detected package-lock.json only conflicts for PR #{pr_data['number']}"
        )

        # Parse conflicted files
        conflicted_files = []
        for line in conflict_info.strip().split("\n"):
            if line.strip() and line.startswith("UU "):
                filename = line[3:].strip()
                conflicted_files.append(filename)

        # Remove conflicted dependency files
        lockfile_names = ["package-lock.json", "yarn.lock", "pnpm-lock.yaml"]
        lockfile_dirs: List[str] = []
        for file in conflicted_files:
            if any(dep in file for dep in lockfile_names):
                remove_result = cmd.run_command(["rm", "-f", file])
                if remove_result.success:
                    actions.append(f"Removed conflicted file: {file}")
                else:
                    actions.append(f"Failed to remove {file}: {remove_result.stderr}")
                # Track directory for regeneration attempts
                lockfile_dirs.append(os.path.dirname(file) or ".")

        # Deduplicate directories while preserving order
        seen = set()
        unique_dirs = []
        for d in lockfile_dirs:
            if d not in seen:
                seen.add(d)
                unique_dirs.append(d)

        # For each directory, if package.json exists there, try to regenerate lock files
        any_regenerated = False
        for d in unique_dirs:
            pkg_path = (
                os.path.join(d, "package.json")
                if d not in ("", ".")
                else "package.json"
            )
            if os.path.exists(pkg_path):
                # Try npm install first in that directory
                if d in ("", "."):
                    npm_result = cmd.run_command(["npm", "install"], timeout=300)
                else:
                    npm_result = cmd.run_command(["npm", "install"], timeout=300, cwd=d)
                if npm_result.success:
                    actions.append(
                        f"Successfully ran npm install in {d or '.'} to regenerate lock file"
                    )
                    any_regenerated = True
                else:
                    # Try yarn if npm fails
                    if d in ("", "."):
                        yarn_result = cmd.run_command(["yarn", "install"], timeout=300)
                    else:
                        yarn_result = cmd.run_command(
                            ["yarn", "install"], timeout=300, cwd=d
                        )
                    if yarn_result.success:
                        actions.append(
                            f"Successfully ran yarn install in {d or '.'} to regenerate lock file"
                        )
                        any_regenerated = True
                    else:
                        actions.append(
                            f"Failed to regenerate lock file in {d or '.'} with npm or yarn: {npm_result.stderr}"
                        )
            else:
                if d in ("", "."):
                    actions.append(
                        "No package.json found, skipping dependency installation"
                    )
                else:
                    actions.append(
                        f"No package.json found in {d or '.'}, skipping dependency installation for this path"
                    )

        if not any_regenerated and not unique_dirs:
            # Fallback message when no lockfile dirs were identified (shouldn't happen)
            actions.append(
                "No lockfile directories identified, skipping dependency installation"
            )

        # Stage the regenerated files
        add_result = cmd.run_command(["git", "add", "."])
        if add_result.success:
            actions.append("Staged regenerated dependency files")
        else:
            actions.append(f"Failed to stage files: {add_result.stderr}")
            return actions

        # Commit the changes (via common helper which auto-runs dprint fmt)
        # commit_result = _commit_with_message(
        #     f"Resolve package-lock.json conflicts for PR #{pr_data['number']}"
        # )
        # if commit_result.success:
        #     actions.append("Committed resolved dependency conflicts")
        # else:
        #     actions.append(f"Failed to commit changes: {commit_result.stderr or commit_result.stdout}")
        #     return actions

        # Push the changes (via common helper)
        # push_result = _push_current_branch()
        # if push_result.success:
        #     actions.append(f"Successfully pushed resolved package-lock.json conflicts for PR #{pr_data['number']}")
        #     # Signal to skip further LLM analysis for this PR in this run
        #     actions.append("ACTION_FLAG:SKIP_ANALYSIS")
        # else:
        #     actions.append(f"Failed to push changes: {push_result.stderr}")

    except Exception as e:
        logger.error(f"Error resolving package-lock conflicts: {e}")
        actions.append(f"Error resolving package-lock conflicts: {e}")

    return actions
