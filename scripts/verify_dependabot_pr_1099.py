#!/usr/bin/env python3
"""
Verification script for Dependabot PR handling.

Tests that the logic correctly handles:
https://github.com/kitamura-tetsuo/outliner/pull/1099

This is NOT part of the test suite - it's for manual verification during development.
"""

import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from auto_coder.automation_config import AutomationConfig
from auto_coder.github_client import GitHubClient
from auto_coder.pr_processor import _is_dependabot_pr
from auto_coder.util.github_action import _check_github_actions_status


def verify_pr_1099() -> None:
    """Verify correct handling of PR #1099."""

    # Setup
    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        print("ERROR: GITHUB_TOKEN environment variable not set")
        sys.exit(1)

    repo_name = "kitamura-tetsuo/outliner"
    pr_number = 1099

    # Initialize client and config
    github_client = GitHubClient.get_instance(github_token)
    config = AutomationConfig()

    # Fetch PR data
    print(f"Fetching PR #{pr_number} from {repo_name}...")
    repo = github_client.get_repository(repo_name)
    pr = repo.get_pull(pr_number)
    pr_data = github_client.get_pr_details(pr)

    # Check if it's a Dependabot PR
    is_dependabot = _is_dependabot_pr(pr_data)
    author = pr_data.get("author", "unknown")

    print(f"\nPR #{pr_number} Analysis:")
    print(f"  Author: {author}")
    print(f"  Is Dependabot PR: {is_dependabot}")

    if not is_dependabot:
        print(f"\nERROR: PR #{pr_number} is not detected as a Dependabot PR!")
        print(f"  Expected: dependabot[bot]")
        print(f"  Got: {author}")
        sys.exit(1)

    # Check GitHub Actions status
    checks = _check_github_actions_status(repo_name, pr_data, config)
    mergeable = pr_data.get("mergeable")

    print(f"\nPR Status:")
    print(f"  checks.success: {checks.success}")
    print(f"  checks.in_progress: {checks.in_progress}")
    print(f"  mergeable: {mergeable}")
    print(f"  state: {pr_data.get('state')}")
    print(f"  mergeable_state: {pr_data.get('mergeStateStatus', 'N/A')}")

    # Simulate filtering logic
    print(f"\nConfiguration:")
    print(f"  IGNORE_DEPENDABOT_PRS: {config.IGNORE_DEPENDABOT_PRS}")
    print(f"  AUTO_MERGE_DEPENDABOT_PRS: {config.AUTO_MERGE_DEPENDABOT_PRS}")

    should_process = False
    reason = ""

    if config.IGNORE_DEPENDABOT_PRS:
        should_process = False
        reason = "IGNORE_DEPENDABOT_PRS is enabled - all Dependabot PRs skipped"
    elif config.AUTO_MERGE_DEPENDABOT_PRS:
        if checks.success and bool(mergeable):
            should_process = True
            reason = "AUTO_MERGE_DEPENDABOT_PRS is enabled and PR is ready (checks passed + mergeable)"
        else:
            should_process = False
            reason = f"AUTO_MERGE_DEPENDABOT_PRS is enabled but PR is not ready (checks.success={checks.success}, mergeable={mergeable})"
    else:
        should_process = True
        reason = "Both flags are False - all Dependabot PRs are processed for fixing"

    print(f"\nDecision:")
    print(f"  Should process: {should_process}")
    print(f"  Reason: {reason}")

    # Expected behavior verification
    print(f"\nExpected Behavior:")
    print(f"  PR #1099 should be PROCESSED (auto-merged) because:")
    print(f"    - It's a Dependabot PR")
    print(f"    - Default config has AUTO_MERGE_DEPENDABOT_PRS=True")
    print(f"    - Tests are passing (expected)")
    print(f"    - PR is mergeable (mergeable_state=clean)")

    if should_process:
        print(f"\n✓ SUCCESS: PR #1099 will be correctly processed!")
        sys.exit(0)
    else:
        print(f"\n✗ FAILURE: PR #1099 will be incorrectly skipped!")
        print(f"\nDiagnosis:")
        if not checks.success:
            print(f"  - GitHub Actions checks are not showing as successful")
            print(f"  - Investigate _check_github_actions_status() logic")
        if not mergeable:
            print(f"  - PR is not showing as mergeable")
            print(f"  - Check if mergeability needs time to compute")
        sys.exit(1)


if __name__ == "__main__":
    verify_pr_1099()
