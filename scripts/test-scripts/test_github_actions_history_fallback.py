#!/usr/bin/env python3
"""
Test script for the new GitHub Actions history fallback functionality.
Tests the enhanced _check_github_actions_status function with historical fallback.
"""

import json
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

# Add src to path to import auto_coder modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from auto_coder.automation_config import AutomationConfig
from auto_coder.util.github_action import (
    _check_github_actions_status,
    _check_github_actions_status_from_history,
)


def test_github_actions_history_fallback_enabled():
    """Test that historical fallback works when enabled in config."""
    print("Testing GitHub Actions history fallback (enabled)...")

    # Create config with fallback enabled
    config = AutomationConfig()
    config.ENABLE_ACTIONS_HISTORY_FALLBACK = True

    # Mock PR data
    pr_data = {"number": 123, "head": {"ref": "feature-branch"}}

    # Mock the gh command to simulate failed current checks
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "Failed to get checks: some other error"

    with patch(
        "auto_coder.util.github_action.cmd.run_command", return_value=mock_result
    ):
        with patch(
            "auto_coder.util.github_action._check_github_actions_status_from_history"
        ) as mock_fallback:
            # Mock the fallback function to return a successful result
            mock_fallback.return_value = MagicMock(success=True, ids=[])

            result = _check_github_actions_status("test/repo", pr_data, config)

            # Verify that fallback was called
            mock_fallback.assert_called_once_with("test/repo", pr_data, config)

            # Verify the result structure
            assert result.success == True
            print("✓ Historical fallback was called when enabled")


def test_github_actions_history_fallback_disabled():
    """Test that historical fallback is not used when disabled in config."""
    print("Testing GitHub Actions history fallback (disabled)...")

    # Create config with fallback disabled
    config = AutomationConfig()
    config.ENABLE_ACTIONS_HISTORY_FALLBACK = False

    # Mock PR data
    pr_data = {"number": 123, "head": {"ref": "feature-branch"}}

    # Mock the gh command to simulate failed current checks
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "Failed to get checks: some other error"

    with patch(
        "auto_coder.util.github_action.cmd.run_command", return_value=mock_result
    ):
        with patch(
            "auto_coder.util.github_action._check_github_actions_status_from_history"
        ) as mock_fallback:
            # Mock the fallback function (should not be called)
            mock_fallback.return_value = MagicMock(success=True, ids=[])

            result = _check_github_actions_status("test/repo", pr_data, config)

            # Verify that fallback was NOT called
            mock_fallback.assert_not_called()

            # Verify the result indicates failure (no fallback)
            assert result.success == False
            print("✓ Historical fallback was NOT called when disabled")


def test_check_github_actions_status_from_history():
    """Test the new _check_github_actions_status_from_history function."""
    print("Testing _check_github_actions_status_from_history function...")

    config = AutomationConfig()

    # Mock PR data
    pr_data = {
        "number": 123,
        "head_branch": "feature-branch",
        "head": {"ref": "feature-branch", "sha": "abc123"},
    }

    # Mock successful API responses
    run_list_result = MagicMock()
    run_list_result.success = True
    run_list_result.stdout = json.dumps(
        [
            {
                "databaseId": 12345,
                "headBranch": "feature-branch",
                "headSha": "abc1234567890abcdef",
                "conclusion": "success",
                "createdAt": "2025-11-03T04:30:00Z",
                "status": "completed",
            }
        ]
    )

    # Mock PR commits response
    pr_view_result = MagicMock()
    pr_view_result.success = True
    pr_view_result.stdout = json.dumps(
        {
            "commits": [
                {
                    "oid": "abc123",
                }
            ]
        }
    )

    # Mock successful API responses for second test
    run_list_result2 = MagicMock()
    run_list_result2.success = True
    run_list_result2.stdout = json.dumps(
        [
            {
                "databaseId": 12345,
                "headBranch": "feature-branch",
                "headSha": "abc1234567890abcdef",
                "conclusion": "success",
                "createdAt": "2025-11-03T04:30:00Z",
                "status": "completed",
            }
        ]
    )

    jobs_result = MagicMock()
    jobs_result.returncode = 0
    jobs_result.stdout = json.dumps(
        {
            "jobs": [
                {
                    "databaseId": 67890,
                    "name": "CI",
                    "conclusion": "success",
                    "status": "completed",
                }
            ]
        }
    )

    with patch("auto_coder.util.github_action.cmd.run_command") as mock_cmd:
        # First call for PR view, second call for run list, third call for jobs
        mock_cmd.side_effect = [pr_view_result, run_list_result, jobs_result]

        result = _check_github_actions_status_from_history("test/repo", pr_data, config)

        # Verify the result structure
        assert result.success == True
        assert 12345 in result.ids

        print("✓ Historical status check returned correct structure")


def test_check_github_actions_status_from_history_failure():
    """Test the new _check_github_actions_status_from_history function with failures."""
    print(
        "Testing _check_github_actions_status_from_history function (with failures)..."
    )

    config = AutomationConfig()

    # Mock PR data
    pr_data = {"number": 123, "head": {"ref": "feature-branch"}}

    # Mock API responses with failures
    run_list_result = MagicMock()
    run_list_result.success = True
    run_list_result.stdout = json.dumps(
        [
            {
                "databaseId": 12345,
                "headBranch": "feature-branch",
                "conclusion": "failure",
                "createdAt": "2025-11-03T04:30:00Z",
                "status": "completed",
            }
        ]
    )

    jobs_result = MagicMock()
    jobs_result.returncode = 0
    jobs_result.stdout = json.dumps(
        {
            "jobs": [
                {
                    "databaseId": 67890,
                    "name": "CI",
                    "conclusion": "failure",
                    "status": "completed",
                }
            ]
        }
    )

    with patch("auto_coder.util.github_action.cmd.run_command") as mock_cmd:
        # First call for run list, second call for jobs
        mock_cmd.side_effect = [run_list_result, jobs_result]

        result = _check_github_actions_status_from_history("test/repo", pr_data, config)

        # Verify the result indicates failure
        assert result.success == False
        assert 12345 in result.ids

        print("✓ Historical status check correctly identified failures")


def test_config_default_values():
    """Test that the new config option has the correct default value."""
    print("Testing config default values...")

    config = AutomationConfig()

    # Verify default values
    assert config.ENABLE_ACTIONS_HISTORY_FALLBACK == True
    assert config.SEARCH_GITHUB_ACTIONS_HISTORY == True

    print("✓ Configuration defaults are correct")


def main():
    """Run all tests."""
    print("Running GitHub Actions History Fallback Tests...")
    print("=" * 60)

    try:
        test_config_default_values()
        test_github_actions_history_fallback_enabled()
        test_github_actions_history_fallback_disabled()
        test_check_github_actions_status_from_history()
        test_check_github_actions_status_from_history_failure()

        print("=" * 60)
        print(
            "✅ All tests passed! GitHub Actions history fallback is working correctly."
        )
        return True

    except Exception as e:
        print("=" * 60)
        print(f"❌ Test failed: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
