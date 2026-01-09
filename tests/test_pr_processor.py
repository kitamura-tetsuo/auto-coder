"""Tests for PR processor backend switching logic.

This module contains tests to verify that backend switching occurs correctly
during PR processing, specifically in the _fix_pr_issues_with_testing function.
"""

from unittest.mock import MagicMock, Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.pr_processor import _fix_pr_issues_with_testing


class TestPRProcessorBackendSwitching:
    """Test cases for backend switching in PR processor."""

    @patch("src.auto_coder.pr_processor._apply_github_actions_fix")
    @patch("src.auto_coder.pr_processor.run_local_tests")
    @patch("src.auto_coder.pr_processor._apply_local_test_fix")
    @patch("src.auto_coder.pr_processor.create_high_score_backend_manager")
    @patch("src.auto_coder.pr_processor.get_llm_backend_manager")
    @patch("src.auto_coder.pr_processor.commit_and_push_changes")
    def test_backend_switching_on_attempt_2(
        self,
        mock_commit,
        mock_get_default_manager,
        mock_create_high_score_manager,
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

        # Mock managers
        default_manager = Mock(name="default_manager")
        high_score_manager = Mock(name="high_score_manager")
        mock_get_default_manager.return_value = default_manager
        mock_create_high_score_manager.return_value = high_score_manager

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
        actions = _fix_pr_issues_with_testing(repo_name, pr_data, config, "GitHub logs", skip_github_actions_fix=True)

        # Assert
        # Check calls to _apply_local_test_fix
        assert mock_apply_local_fix.call_count == 2

        # First call (attempt 1) should use default manager
        call_args_1 = mock_apply_local_fix.call_args_list[0]
        assert call_args_1.kwargs["backend_manager"] == default_manager

        # Second call (attempt 2) should use high_score manager
        call_args_2 = mock_apply_local_fix.call_args_list[1]
        assert call_args_2.kwargs["backend_manager"] == high_score_manager

    @patch("src.auto_coder.pr_processor._apply_github_actions_fix")
    @patch("src.auto_coder.pr_processor.run_local_tests")
    @patch("src.auto_coder.pr_processor._apply_local_test_fix")
    @patch("src.auto_coder.pr_processor.create_high_score_backend_manager")
    @patch("src.auto_coder.pr_processor.get_llm_backend_manager")
    @patch("src.auto_coder.pr_processor.commit_and_push_changes")
    def test_no_backend_switching_on_attempt_1(
        self,
        mock_commit,
        mock_get_default_manager,
        mock_create_high_score_manager,
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

        # Mock managers
        default_manager = Mock(name="default_manager")
        high_score_manager = Mock(name="high_score_manager")
        mock_get_default_manager.return_value = default_manager
        mock_create_high_score_manager.return_value = high_score_manager

        # Mock test to pass on first attempt
        mock_run_tests.return_value = {"success": True, "output": "Tests passed", "errors": ""}

        # Mock local fix to return empty actions and no response
        mock_apply_local_fix.return_value = ([], "")

        # Mock GitHub Actions fix to return empty actions
        mock_github_actions_fix.return_value = []

        # Execute
        actions = _fix_pr_issues_with_testing(repo_name, pr_data, config, "GitHub logs", skip_github_actions_fix=True)

        # Assert
        # Should NOT have called _apply_local_test_fix since test passed
        mock_apply_local_fix.assert_not_called()

    @patch("src.auto_coder.pr_processor._apply_github_actions_fix")
    @patch("src.auto_coder.pr_processor.run_local_tests")
    @patch("src.auto_coder.pr_processor._apply_local_test_fix")
    @patch("src.auto_coder.pr_processor.create_high_score_backend_manager")
    @patch("src.auto_coder.pr_processor.get_llm_backend_manager")
    @patch("src.auto_coder.pr_processor.commit_and_push_changes")
    def test_backend_switching_on_multiple_attempts(
        self,
        mock_commit,
        mock_get_default_manager,
        mock_create_high_score_manager,
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

        # Mock managers
        default_manager = Mock(name="default_manager")
        high_score_manager = Mock(name="high_score_manager")
        mock_get_default_manager.return_value = default_manager
        mock_create_high_score_manager.return_value = high_score_manager

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
        actions = _fix_pr_issues_with_testing(repo_name, pr_data, config, "GitHub logs", skip_github_actions_fix=True)

        # Assert
        # Check calls to _apply_local_test_fix
        assert mock_apply_local_fix.call_count == 3

        # Attempt 1: default manager
        assert mock_apply_local_fix.call_args_list[0].kwargs["backend_manager"] == default_manager
        # Attempt 2: high_score manager
        assert mock_apply_local_fix.call_args_list[1].kwargs["backend_manager"] == high_score_manager
        # Attempt 3: high_score manager
        assert mock_apply_local_fix.call_args_list[2].kwargs["backend_manager"] == high_score_manager

    @patch("src.auto_coder.pr_processor._apply_github_actions_fix")
    @patch("src.auto_coder.pr_processor.run_local_tests")
    @patch("src.auto_coder.pr_processor._apply_local_test_fix")
    @patch("src.auto_coder.pr_processor.create_high_score_backend_manager")
    @patch("src.auto_coder.pr_processor.get_llm_backend_manager")
    @patch("src.auto_coder.pr_processor.commit_and_push_changes")
    def test_backend_switching_with_finite_attempts_limit(
        self,
        mock_commit,
        mock_get_default_manager,
        mock_create_high_score_manager,
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

        # Mock managers
        default_manager = Mock(name="default_manager")
        high_score_manager = Mock(name="high_score_manager")
        mock_get_default_manager.return_value = default_manager
        mock_create_high_score_manager.return_value = high_score_manager

        # Mock test results - always fail
        mock_run_tests.return_value = {"success": False, "output": "Test failed", "errors": "Errors"}

        # Mock local fix to return empty actions and no response
        mock_apply_local_fix.return_value = ([], "")

        # Mock GitHub Actions fix to return empty actions
        mock_github_actions_fix.return_value = []

        # Execute
        actions = _fix_pr_issues_with_testing(repo_name, pr_data, config, "GitHub logs", skip_github_actions_fix=True)

        # Assert
        # Check calls to _apply_local_test_fix
        assert mock_apply_local_fix.call_count == 2

        # Attempt 1: default manager
        assert mock_apply_local_fix.call_args_list[0].kwargs["backend_manager"] == default_manager
        # Attempt 2: high_score manager
        assert mock_apply_local_fix.call_args_list[1].kwargs["backend_manager"] == high_score_manager


class TestKeepLabelOnPRMerge:
    """Test that keep_label() is called on successful PR merge."""

    def test_process_pr_for_merge_calls_keep_label_on_successful_merge(self):
        """Test that _process_pr_for_merge calls keep_label when PR is successfully merged."""
        from contextlib import contextmanager
        from unittest.mock import Mock, patch

        from src.auto_coder.automation_config import AutomationConfig
        from src.auto_coder.pr_processor import _process_pr_for_merge

        repo_name = "owner/repo"
        pr_data = {"number": 123, "title": "Test PR", "body": "Test body"}
        config = AutomationConfig()

        # Track if keep_label was called
        keep_label_called = []

        # Create a mock LabelManagerContext that tracks keep_label calls
        class MockLabelManagerContext:
            def __init__(self, should_process):
                self._should_process = should_process

            def __bool__(self):
                return self._should_process

            def keep_label(self):
                keep_label_called.append(True)

        @contextmanager
        def fake_label_manager(*_args, **_kwargs):
            yield MockLabelManagerContext(True)

        # Mock _merge_pr to return True (successful merge)
        with patch("src.auto_coder.pr_processor.LabelManager", fake_label_manager):
            with patch("src.auto_coder.pr_processor._merge_pr", return_value=True):
                with patch("src.auto_coder.pr_processor.GitHubClient") as mock_client_class:
                    mock_client = Mock()
                    mock_client_class.get_instance.return_value = mock_client

                    result = _process_pr_for_merge(repo_name, pr_data, config)

        # Verify keep_label was called
        assert len(keep_label_called) == 1, "keep_label should be called once on successful PR merge"
        assert "Successfully merged" in result.actions_taken[0]

    def test_process_pr_for_merge_does_not_call_keep_label_on_failed_merge(self):
        """Test that _process_pr_for_merge does not call keep_label when merge fails."""
        from contextlib import contextmanager
        from unittest.mock import Mock, patch

        from src.auto_coder.automation_config import AutomationConfig
        from src.auto_coder.pr_processor import _process_pr_for_merge

        repo_name = "owner/repo"
        pr_data = {"number": 456, "title": "Test PR", "body": "Test body"}
        config = AutomationConfig()

        # Track if keep_label was called
        keep_label_called = []

        # Create a mock LabelManagerContext that tracks keep_label calls
        class MockLabelManagerContext:
            def __init__(self, should_process):
                self._should_process = should_process

            def __bool__(self):
                return self._should_process

            def keep_label(self):
                keep_label_called.append(True)

        @contextmanager
        def fake_label_manager(*_args, **_kwargs):
            yield MockLabelManagerContext(True)

        # Mock _merge_pr to return False (failed merge)
        with patch("src.auto_coder.pr_processor.LabelManager", fake_label_manager):
            with patch("src.auto_coder.pr_processor._merge_pr", return_value=False):
                with patch("src.auto_coder.pr_processor.GitHubClient") as mock_client_class:
                    mock_client = Mock()
                    mock_client_class.get_instance.return_value = mock_client

                    result = _process_pr_for_merge(repo_name, pr_data, config)

        # Verify keep_label was NOT called
        assert len(keep_label_called) == 0, "keep_label should not be called when merge fails"
        assert "Failed to merge" in result.actions_taken[0]

    def test_process_pr_for_fixes_calls_keep_label_on_successful_merge(self):
        """Test that _process_pr_for_fixes calls keep_label when PR is successfully merged."""
        from contextlib import contextmanager
        from unittest.mock import Mock, patch

        from src.auto_coder.automation_config import AutomationConfig
        from src.auto_coder.pr_processor import _process_pr_for_fixes

        repo_name = "owner/repo"
        pr_data = {"number": 789, "title": "Test PR", "body": "Test body"}
        config = AutomationConfig()
        mock_github_client = Mock()

        # Track if keep_label was called
        keep_label_called = []

        # Create a mock LabelManagerContext that tracks keep_label calls
        class MockLabelManagerContext:
            def __init__(self, should_process):
                self._should_process = should_process

            def __bool__(self):
                return self._should_process

            def keep_label(self):
                keep_label_called.append(True)

        @contextmanager
        def fake_label_manager(*_args, **_kwargs):
            yield MockLabelManagerContext(True)

        # Mock _take_pr_actions to return actions indicating successful merge
        with patch("src.auto_coder.pr_processor.LabelManager", fake_label_manager):
            with patch("src.auto_coder.pr_processor.ProgressStage"):
                with patch("src.auto_coder.pr_processor._take_pr_actions") as mock_take_actions:
                    mock_take_actions.return_value = ["Successfully merged PR #789"]

                    result = _process_pr_for_fixes(mock_github_client, repo_name, pr_data, config)

        # Verify keep_label was called
        assert len(keep_label_called) == 1, "keep_label should be called once on successful PR merge"
        assert "Successfully merged" in result.actions_taken[0]

    def test_process_pr_for_fixes_does_not_call_keep_label_on_no_merge(self):
        """Test that _process_pr_for_fixes does not call keep_label when no merge occurs."""
        from contextlib import contextmanager
        from unittest.mock import Mock, patch

        from src.auto_coder.automation_config import AutomationConfig
        from src.auto_coder.pr_processor import _process_pr_for_fixes

        repo_name = "owner/repo"
        pr_data = {"number": 101, "title": "Test PR", "body": "Test body"}
        config = AutomationConfig()
        mock_github_client = Mock()

        # Track if keep_label was called
        keep_label_called = []

        # Create a mock LabelManagerContext that tracks keep_label calls
        class MockLabelManagerContext:
            def __init__(self, should_process):
                self._should_process = should_process

            def __bool__(self):
                return self._should_process

            def keep_label(self):
                keep_label_called.append(True)

        @contextmanager
        def fake_label_manager(*_args, **_kwargs):
            yield MockLabelManagerContext(True)

        # Mock _take_pr_actions to return actions WITHOUT merge
        with patch("src.auto_coder.pr_processor.LabelManager", fake_label_manager):
            with patch("src.auto_coder.pr_processor.ProgressStage"):
                with patch("src.auto_coder.pr_processor._take_pr_actions") as mock_take_actions:
                    mock_take_actions.return_value = ["GitHub Actions checks are still in progress"]

                    result = _process_pr_for_fixes(mock_github_client, repo_name, pr_data, config)

        # Verify keep_label was NOT called
        assert len(keep_label_called) == 0, "keep_label should not be called when no merge occurs"
