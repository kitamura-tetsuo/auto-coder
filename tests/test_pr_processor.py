"""Tests for PR processor backend switching logic.

This module contains tests to verify that backend switching occurs correctly
during PR processing, specifically in the _fix_pr_issues_with_testing function.
"""

from unittest.mock import MagicMock, Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.pr_processor import _fix_pr_issues_with_testing, _switch_to_fallback_backend


class TestPRProcessorBackendSwitching:
    """Test cases for backend switching in PR processor."""

    @patch("src.auto_coder.pr_processor._apply_github_actions_fix")
    @patch("src.auto_coder.pr_processor.run_local_tests")
    @patch("src.auto_coder.pr_processor._apply_local_test_fix")
    @patch("src.auto_coder.pr_processor._switch_to_fallback_backend")
    @patch("src.auto_coder.pr_processor.commit_and_push_changes")
    @patch("src.auto_coder.pr_processor.check_for_updates_and_restart")
    def test_backend_switching_on_attempt_2(
        self,
        mock_check_updates,
        mock_commit,
        mock_switch_backend,
        mock_apply_local_fix,
        mock_run_tests,
        mock_github_actions_fix,
    ):
        """Test that backend switching occurs when attempt >= 2."""
        # Setup
        config = AutomationConfig()
        config.MAX_FIX_ATTEMPTS = 10  # Set a high limit to allow multiple attempts

        pr_data = {
            "number": 123,
            "title": "Test PR",
            "body": "Test description",
            "head": {"ref": "test-branch"},
            "base": {"ref": "main"},
        }

        repo_name = "test/repo"

        # Mock the test results - fail first, pass on third
        mock_run_tests.side_effect = [
            {"success": False, "output": "Test failed", "errors": "Error details"},  # attempt 1
            {"success": False, "output": "Test failed again", "errors": "More errors"},  # attempt 2
            {"success": True, "output": "All tests passed", "errors": ""},  # attempt 3
        ]

        # Mock local fix to return empty actions and no response
        mock_apply_local_fix.return_value = ([], "")

        # Mock GitHub Actions fix to return empty actions
        mock_github_actions_fix.return_value = []

        # Execute
        actions = _fix_pr_issues_with_testing(repo_name, pr_data, config, "GitHub logs")

        # Assert
        # Should have called _switch_to_fallback_backend at least once
        assert mock_switch_backend.called
        # Should have been called at least once with correct arguments
        assert mock_switch_backend.call_count >= 1
        # Verify it was called with attempt 2
        mock_switch_backend.assert_any_call(repo_name, 123)

    @patch("src.auto_coder.pr_processor._apply_github_actions_fix")
    @patch("src.auto_coder.pr_processor.run_local_tests")
    @patch("src.auto_coder.pr_processor._apply_local_test_fix")
    @patch("src.auto_coder.pr_processor._switch_to_fallback_backend")
    @patch("src.auto_coder.pr_processor.commit_and_push_changes")
    @patch("src.auto_coder.pr_processor.check_for_updates_and_restart")
    def test_no_backend_switching_on_attempt_1(
        self,
        mock_check_updates,
        mock_commit,
        mock_switch_backend,
        mock_apply_local_fix,
        mock_run_tests,
        mock_github_actions_fix,
    ):
        """Test that backend switching does NOT occur for attempt 1."""
        # Setup
        config = AutomationConfig()
        config.MAX_FIX_ATTEMPTS = 10

        pr_data = {
            "number": 456,
            "title": "Test PR",
            "body": "Test description",
            "head": {"ref": "test-branch"},
            "base": {"ref": "main"},
        }

        repo_name = "test/repo"

        # Mock test to pass on first attempt
        mock_run_tests.return_value = {"success": True, "output": "Tests passed", "errors": ""}

        # Mock local fix to return empty actions and no response
        mock_apply_local_fix.return_value = ([], "")

        # Mock GitHub Actions fix to return empty actions
        mock_github_actions_fix.return_value = []

        # Execute
        actions = _fix_pr_issues_with_testing(repo_name, pr_data, config, "GitHub logs")

        # Assert
        # Should NOT have called _switch_to_fallback_backend since test passed on first attempt
        mock_switch_backend.assert_not_called()

    @patch("src.auto_coder.pr_processor._apply_github_actions_fix")
    @patch("src.auto_coder.pr_processor.run_local_tests")
    @patch("src.auto_coder.pr_processor._apply_local_test_fix")
    @patch("src.auto_coder.pr_processor._switch_to_fallback_backend")
    @patch("src.auto_coder.pr_processor.commit_and_push_changes")
    @patch("src.auto_coder.pr_processor.check_for_updates_and_restart")
    def test_backend_switching_on_multiple_attempts(
        self,
        mock_check_updates,
        mock_commit,
        mock_switch_backend,
        mock_apply_local_fix,
        mock_run_tests,
        mock_github_actions_fix,
    ):
        """Test that backend switching occurs when attempt >= 2."""
        # Setup
        config = AutomationConfig()
        config.MAX_FIX_ATTEMPTS = 10

        pr_data = {
            "number": 789,
            "title": "Test PR",
            "body": "Test description",
            "head": {"ref": "test-branch"},
            "base": {"ref": "main"},
        }

        repo_name = "test/repo"

        # Mock test results - fail multiple times
        mock_run_tests.side_effect = [
            {"success": False, "output": "Test failed", "errors": "Error 1"},  # attempt 1
            {"success": False, "output": "Test failed", "errors": "Error 2"},  # attempt 2 (switch here)
            {"success": False, "output": "Test failed", "errors": "Error 3"},  # attempt 3 (switch here too)
            {"success": True, "output": "Tests passed", "errors": ""},  # attempt 4 (switch here too)
        ]

        # Mock local fix to return empty actions and no response
        mock_apply_local_fix.return_value = ([], "")

        # Mock GitHub Actions fix to return empty actions
        mock_github_actions_fix.return_value = []

        # Execute
        actions = _fix_pr_issues_with_testing(repo_name, pr_data, config, "GitHub logs")

        # Assert
        # Should have called _switch_to_fallback_backend for attempts 2, 3, and 4
        assert mock_switch_backend.call_count == 3
        # Verify all calls were with correct arguments
        for call_args in mock_switch_backend.call_args_list:
            assert call_args[0] == (repo_name, 789)

    @patch("src.auto_coder.pr_processor._apply_github_actions_fix")
    @patch("src.auto_coder.pr_processor.run_local_tests")
    @patch("src.auto_coder.pr_processor._apply_local_test_fix")
    @patch("src.auto_coder.pr_processor._switch_to_fallback_backend")
    @patch("src.auto_coder.pr_processor.commit_and_push_changes")
    @patch("src.auto_coder.pr_processor.check_for_updates_and_restart")
    def test_backend_switching_with_finite_attempts_limit(
        self,
        mock_check_updates,
        mock_commit,
        mock_switch_backend,
        mock_apply_local_fix,
        mock_run_tests,
        mock_github_actions_fix,
    ):
        """Test that backend switching works correctly with finite attempts limit."""
        # Setup - use finite limit of 3
        config = AutomationConfig()
        config.MAX_FIX_ATTEMPTS = 3

        pr_data = {
            "number": 321,
            "title": "Test PR",
            "body": "Test description",
            "head": {"ref": "test-branch"},
            "base": {"ref": "main"},
        }

        repo_name = "test/repo"

        # Mock test results - always fail
        mock_run_tests.return_value = {"success": False, "output": "Test failed", "errors": "Errors"}

        # Mock local fix to return empty actions and no response
        mock_apply_local_fix.return_value = ([], "")

        # Mock GitHub Actions fix to return empty actions
        mock_github_actions_fix.return_value = []

        # Execute
        actions = _fix_pr_issues_with_testing(repo_name, pr_data, config, "GitHub logs")

        # Assert
        # Should have called _switch_to_fallback_backend for attempts 2 and 3
        assert mock_switch_backend.call_count == 2
        # Verify all calls were with correct arguments
        for call_args in mock_switch_backend.call_args_list:
            assert call_args[0] == (repo_name, 321)
