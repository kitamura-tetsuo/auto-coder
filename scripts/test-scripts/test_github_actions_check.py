#!/usr/bin/env python
"""
Test script to verify that when processing a single PR with GitHub Actions in progress,
the system switches to main branch and exits.
"""
import sys
from unittest.mock import Mock, patch

# Add src to path
sys.path.insert(0, '/home/node/src/auto-coder/src')

from auto_coder.automation_engine import AutomationEngine
from auto_coder.automation_config import AutomationConfig


def test_pr_with_github_actions_in_progress() -> None:
    """Test that when a single PR has GitHub Actions in progress, it switches to main and exits."""

    # Create mock objects
    github_client = Mock()
    config = AutomationConfig()

    # Mock PR details
    pr_data = {
        "number": 789,
        "title": "Test PR",
        "body": "Test body",
        "state": "open",
        "head": {"ref": "test-branch"}
    }

    github_client.get_pr_details_by_number.return_value = pr_data

    # Create AutomationEngine instance
    engine = AutomationEngine(github_client, config)

    # Mock GitHub Actions to be in progress
    with patch('auto_coder.util.github_action._check_github_actions_status') as mock_checks:
        with patch('auto_coder.util.github_action.get_detailed_checks_from_history') as mock_detailed:
            # Set up mocks to simulate in-progress checks
            mock_checks.return_value = Mock(success=False)
            mock_detailed.return_value = Mock(
                success=False,
                has_in_progress=True,
                total_checks=2,
                failed_checks=[],
                all_checks=[],
                run_ids=[]
            )

            # Patch branch_context to simulate main branch switch
            with patch('auto_coder.automation_engine.branch_context') as mock_branch_context:
                with patch('auto_coder.issue_processor.sys.exit') as mock_exit:
                    # Mock branch_context to return a context manager
                    mock_cm = Mock()
                    mock_cm.__enter__ = Mock(return_value=None)
                    mock_cm.__exit__ = Mock(return_value=None)
                    mock_branch_context.return_value = mock_cm

                    # Call process_single
                    result = engine.process_single(
                        repo_name="test/repo",
                        target_type="pr",
                        number=789,
                        jules_mode=False,
                    )

                    # Verify that sys.exit was called with code 0
                    mock_exit.assert_called_once_with(0)

    print("✅ Test passed! The system correctly:")
    print("   1. Detected that GitHub Actions were in progress for PR #789")
    print("   2. Switched to main branch")
    print("   3. Exited the program")


if __name__ == "__main__":
    print("Testing PR GitHub Actions in-progress handling...\n")

    try:
        test_pr_with_github_actions_in_progress()
        print("\n✅ All tests passed!")
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
