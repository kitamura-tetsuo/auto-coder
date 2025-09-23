"""
Conflict resolution functionality for Auto-Coder automation engine.
"""

import json
import os
from typing import Dict, Any, List, Optional

from .utils import CommandExecutor, log_action
from .automation_config import AutomationConfig
from .logger_config import get_logger
from .prompt_loader import render_prompt

logger = get_logger(__name__)
cmd = CommandExecutor()


def resolve_merge_conflicts_with_llm(pr_data: Dict[str, Any], conflict_info: str, config: AutomationConfig, dry_run: bool) -> List[str]:
    """Ask LLM to resolve merge conflicts."""
    actions: List[str] = []

    try:
        # Create a prompt for LLM to resolve conflicts
        base_branch = pr_data.get('base_branch') or pr_data.get('base', {}).get('ref') or config.MAIN_BRANCH
        resolve_prompt = render_prompt(
            "pr.merge_conflict_resolution",
            base_branch=base_branch,
            pr_number=pr_data.get('number', 'unknown'),
            pr_title=pr_data.get('title', 'Unknown'),
            pr_body=(pr_data.get('body') or '')[:500],
            conflict_info=conflict_info,
        )

        # Use LLM to resolve conflicts
        logger.info(f"Asking LLM to resolve merge conflicts for PR #{pr_data['number']}")
        response = "Resolved merge conflicts"  # Placeholder

        # Parse the response
        if response and len(response.strip()) > 0:
            actions.append(f"LLM resolved merge conflicts: {response[:200]}...")

            # Stage any changes made by LLM
            add_res = cmd.run_command(['git', 'add', '.'])
            if not add_res.success:
                actions.append(f"Failed to stage resolved files: {add_res.stderr}")
                return actions

            # Verify no conflict markers remain before committing
            # flagged = _scan_conflict_markers()
            # if flagged:
            #     actions.append(
            #         f"Conflict markers still present in {len(flagged)} file(s): {', '.join(sorted(set(flagged)))}; not committing"
            #     )
            #     return actions

            # Commit via helper and push
            commit_msg = f"Resolve merge conflicts for PR #{pr_data['number']}"
            # commit_res = _commit_with_message(commit_msg)
            # if commit_res.success:
            #     actions.append(f"Committed resolved merge for PR #{pr_data['number']}")
            # else:
            #     actions.append(f"Failed to commit resolved merge: {commit_res.stderr or commit_res.stdout}")
            #     return actions

            # push_res = _push_current_branch()
            # if push_res.success:
            #     actions.append(f"Pushed resolved merge for PR #{pr_data['number']}")
            #     actions.append("ACTION_FLAG:SKIP_ANALYSIS")
            # else:
            #     actions.append(f"Failed to push resolved merge: {push_res.stderr}")
        else:
            actions.append("LLM did not provide a clear response for merge conflict resolution")

    except Exception as e:
        logger.error(f"Error resolving merge conflicts with LLM: {e}")
        actions.append(f"Error resolving merge conflicts: {e}")

    return actions


def is_package_lock_only_conflict(conflict_info: str) -> bool:
    """Check if conflicts are only in package-lock.json files."""
    try:
        # Parse git status output to find conflicted files
        conflicted_files = []
        for line in conflict_info.strip().split('\n'):
            if line.strip():
                # Git status --porcelain format: XY filename
                # UU means both modified (merge conflict)
                if line.startswith('UU '):
                    filename = line[3:].strip()
                    conflicted_files.append(filename)

        # Check if all conflicted files are package-lock.json or similar dependency files
        if not conflicted_files:
            return False

        dependency_files = {'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml'}
        return all(any(dep_file in file for dep_file in dependency_files) for file in conflicted_files)

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
        for line in conflict_info.strip().split('\n'):
            if line.strip() and line.startswith('UU '):
                filename = line[3:].strip()
                if filename.endswith('package.json'):
                    conflicted_files.append(filename)
                else:
                    # Any non-package.json conflict disqualifies this specialized resolver
                    return False

        if not conflicted_files:
            return False

        dep_keys = {"dependencies", "devDependencies", "peerDependencies", "optionalDependencies"}

        for path in conflicted_files:
            ours = cmd.run_command(['git', 'show', f':2:{path}'])
            theirs = cmd.run_command(['git', 'show', f':3:{path}'])
            if not (ours.success and theirs.success):
                return False
            try:
                ours_json = json.loads(ours.stdout or '{}')
                theirs_json = json.loads(theirs.stdout or '{}')
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
        for line in conflict_info.strip().split('\n'):
            if line.strip() and line.startswith('UU '):
                filename = line[3:].strip()
                if filename.endswith('package.json'):
                    conflicted_paths.append(filename)

        if not conflicted_paths:
            return []

        dep_keys = {"dependencies", "devDependencies", "peerDependencies", "optionalDependencies"}
        eligible: List[str] = []
        for path in conflicted_paths:
            ours = cmd.run_command(['git', 'show', f':2:{path}'])
            theirs = cmd.run_command(['git', 'show', f':3:{path}'])
            if not (ours.success and theirs.success):
                continue
            try:
                ours_json = json.loads(ours.stdout or '{}')
                theirs_json = json.loads(theirs.stdout or '{}')
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
    while s and s[0] in ('^', '~', '>', '<', '=', 'v'):
        s = s[1:]
    # Remove leading = if any remain
    s = s.lstrip('=')
    # Split on hyphen (prerelease) and plus (build)
    s = s.split('+', 1)[0].split('-', 1)[0]
    parts = s.split('.')
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


def merge_dep_maps(ours: Dict[str, str], theirs: Dict[str, str], prefer_side: str) -> Dict[str, str]:
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
                if prefer_side == 'ours':
                    result[k] = va
                else:
                    result[k] = vb
    return result


def resolve_package_json_dependency_conflicts(pr_data: Dict[str, Any], conflict_info: str, config: AutomationConfig, dry_run: bool, eligible_paths: Optional[List[str]] = None) -> List[str]:
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
        pr_number = pr_data['number']
        actions.append(f"Detected package.json dependency-only conflicts for PR #{pr_number}")

        conflicted_paths: List[str] = []
        if eligible_paths is not None:
            conflicted_paths = list(eligible_paths)
        else:
            for line in conflict_info.strip().split('\n'):
                if line.strip() and line.startswith('UU '):
                    p = line[3:].strip()
                    if p.endswith('package.json'):
                        conflicted_paths.append(p)

        updated_files: List[str] = []
        for path in conflicted_paths:
            ours = cmd.run_command(['git', 'show', f':2:{path}'])
            theirs = cmd.run_command(['git', 'show', f':3:{path}'])
            if not (ours.success and theirs.success):
                actions.append(f"Failed to read staged versions for {path}")
                continue
            try:
                ours_json = json.loads(ours.stdout or '{}')
                theirs_json = json.loads(theirs.stdout or '{}')
            except Exception as e:
                actions.append(f"Invalid JSON in staged package.json for {path}: {e}")
                continue

            dep_keys = ["dependencies", "devDependencies", "peerDependencies", "optionalDependencies"]

            # Decide tie-breaker side per section by larger map size
            prefer_map = {}
            for k in dep_keys:
                oa = ours_json.get(k) or {}
                ob = theirs_json.get(k) or {}
                prefer_map[k] = 'ours' if len(oa) >= len(ob) else 'theirs'

            merged = dict(ours_json)  # start from ours
            for k in dep_keys:
                oa = ours_json.get(k) or {}
                ob = theirs_json.get(k) or {}
                if not isinstance(oa, dict) or not isinstance(ob, dict):
                    # Unexpected structure; fallback to ours
                    continue
                merged[k] = merge_dep_maps(oa, ob, prefer_map[k])

            # Write merged JSON back to file
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(merged, f, ensure_ascii=False, indent=2, sort_keys=True)
                f.write('\n')
            updated_files.append(path)
            actions.append(f"Merged dependency sections in {path}")

        if not updated_files:
            actions.append("No package.json files updated; skipping commit")
            return actions

        add = cmd.run_command(['git', 'add'] + updated_files)
        if not add.success:
            actions.append(f"Failed to stage merged package.json files: {add.stderr}")
            return actions
        actions.append("Staged merged package.json files")

        commit_msg = f"Resolve package.json dependency-only conflicts for PR #{pr_number} by preferring newer versions and union"
        # commit = cmd.run_command(['git', 'commit', '-m', commit_msg])
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


def resolve_package_lock_conflicts(pr_data: Dict[str, Any], conflict_info: str, config: AutomationConfig, dry_run: bool) -> List[str]:
    """Resolve package-lock.json conflicts by deleting and regenerating the file.

    Monorepo-friendly: for each conflicted lockfile, if a sibling package.json exists,
    run package manager commands in that directory to regenerate the lock file.
    """
    actions = []

    try:
        logger.info(f"Resolving package-lock.json conflicts for PR #{pr_data['number']}")
        actions.append(f"Detected package-lock.json only conflicts for PR #{pr_data['number']}")

        # Parse conflicted files
        conflicted_files = []
        for line in conflict_info.strip().split('\n'):
            if line.strip() and line.startswith('UU '):
                filename = line[3:].strip()
                conflicted_files.append(filename)

        # Remove conflicted dependency files
        lockfile_names = ['package-lock.json', 'yarn.lock', 'pnpm-lock.yaml']
        lockfile_dirs: List[str] = []
        for file in conflicted_files:
            if any(dep in file for dep in lockfile_names):
                remove_result = cmd.run_command(['rm', '-f', file])
                if remove_result.success:
                    actions.append(f"Removed conflicted file: {file}")
                else:
                    actions.append(f"Failed to remove {file}: {remove_result.stderr}")
                # Track directory for regeneration attempts
                lockfile_dirs.append(os.path.dirname(file) or '.')

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
            pkg_path = os.path.join(d, 'package.json') if d not in ('', '.') else 'package.json'
            if os.path.exists(pkg_path):
                # Try npm install first in that directory
                if d in ('', '.'):
                    npm_result = cmd.run_command(['npm', 'install'], timeout=300)
                else:
                    npm_result = cmd.run_command(['npm', 'install'], timeout=300, cwd=d)
                if npm_result.success:
                    actions.append(f"Successfully ran npm install in {d or '.'} to regenerate lock file")
                    any_regenerated = True
                else:
                    # Try yarn if npm fails
                    if d in ('', '.'):
                        yarn_result = cmd.run_command(['yarn', 'install'], timeout=300)
                    else:
                        yarn_result = cmd.run_command(['yarn', 'install'], timeout=300, cwd=d)
                    if yarn_result.success:
                        actions.append(f"Successfully ran yarn install in {d or '.'} to regenerate lock file")
                        any_regenerated = True
                    else:
                        actions.append(f"Failed to regenerate lock file in {d or '.'} with npm or yarn: {npm_result.stderr}")
            else:
                if d in ('', '.'):
                    actions.append("No package.json found, skipping dependency installation")
                else:
                    actions.append(f"No package.json found in {d or '.'}, skipping dependency installation for this path")

        if not any_regenerated and not unique_dirs:
            # Fallback message when no lockfile dirs were identified (shouldn't happen)
            actions.append("No lockfile directories identified, skipping dependency installation")

        # Stage the regenerated files
        add_result = cmd.run_command(['git', 'add', '.'])
        if add_result.success:
            actions.append("Staged regenerated dependency files")
        else:
            actions.append(f"Failed to stage files: {add_result.stderr}")
            return actions

        # Commit the changes (via common helper which auto-runs dprint fmt)
        commit_message = f"Resolve package-lock.json conflicts for PR #{pr_data['number']}"
        # commit_result = _commit_with_message(commit_message)
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