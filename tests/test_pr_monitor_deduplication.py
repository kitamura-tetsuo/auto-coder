import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.pr_processor import _active_monitors, _handle_pr_merge


class TestPRMonitorDeduplication:
    """Test cases for PR monitor deduplication logic."""

    def setup_method(self):
        """Reset active monitors before each test."""
        # We need to access the module-level variable.
        # Since we imported it, we might be looking at a copy if it was just 'from ... import _active_monitors'
        # but sets are mutable so it should be fine if we clear it.
        # Better to patch it or clear it directly.
        _active_monitors.clear()

    @patch("src.auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress")
    @patch("src.auto_coder.pr_processor._get_mergeable_state")
    @patch("src.auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.util.github_action.trigger_workflow_dispatch")
    @patch("src.auto_coder.pr_processor.threading.Thread")
    @patch("src.auto_coder.pr_processor.LabelManager")
    @patch("src.auto_coder.pr_processor.get_detailed_checks_from_history")
    def test_handle_pr_merge_deduplication(
        self,
        mock_get_detailed,
        mock_label_manager,
        mock_thread,
        mock_trigger,
        mock_check_status,
        mock_mergeable,
        mock_check_in_progress,
    ):
        """Test that async monitor is started only once for the same PR."""
        # Setup
        repo_name = "test/repo"
        pr_number = 123
        pr_data = {"number": pr_number, "head": {"sha": "abc1234", "ref": "feature-branch"}, "labels": []}
        config = AutomationConfig()

        # Mocks
        mock_check_in_progress.return_value = True
        mock_mergeable.return_value = {"mergeable": True, "merge_state_status": "clean"}

        # Mock status to return NO checks found (triggering the logic we want to test)
        mock_check_status.return_value = MagicMock(ids=[], error=None)

        mock_trigger.return_value = True

        # LabelManager mock
        mock_lm_instance = MagicMock()
        mock_label_manager.return_value.__enter__.return_value = mock_lm_instance

        # Call 1
        # Call 1
        actions = _handle_pr_merge(MagicMock(), repo_name, pr_data, config, {})

        # Verify first call started thread
        # Verify first call started thread

        assert mock_thread.call_count == 1
        assert mock_trigger.call_count == 1
        assert pr_number in _active_monitors

        # Call 2 (Simulating another thread or quick subsequent call)
        _handle_pr_merge(MagicMock(), repo_name, pr_data, config, {})

        # Verify second call DID NOT start thread or trigger workflow
        assert mock_thread.call_count == 1
        assert mock_trigger.call_count == 1

        # Verify label was kept (for the successful one)
        mock_lm_instance.keep_label.assert_called()

    def test_monitor_cleanup_on_completion(self):
        """Verify that _active_monitors is cleaned up after monitor finishes."""
        # For this test, we need to import _run_async_monitor properly
        from src.auto_coder.pr_processor import _run_async_monitor

        pr_number = 456
        _active_monitors.add(pr_number)
        assert pr_number in _active_monitors

        with patch("src.auto_coder.pr_processor.asyncio.run") as mock_asyncio_run:
            _run_async_monitor("repo", pr_number, "sha", "workflow")

        assert pr_number not in _active_monitors

    def test_monitor_cleanup_on_error(self):
        """Verify that _active_monitors is cleaned up even if monitor crashes."""
        from src.auto_coder.pr_processor import _run_async_monitor

        pr_number = 789
        _active_monitors.add(pr_number)

        with patch("src.auto_coder.pr_processor.asyncio.run") as mock_asyncio_run:
            mock_asyncio_run.side_effect = Exception("Crash!")
            try:
                _run_async_monitor("repo", pr_number, "sha", "workflow")
            except Exception:
                pass

        assert pr_number not in _active_monitors
