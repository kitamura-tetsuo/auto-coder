"""
Unit tests for JulesClient CloudTaskClientBase methods.
"""

from unittest.mock import MagicMock, patch

import pytest

from auto_coder.cloud_task_client_base import CloudTaskState
from auto_coder.jules_client import JulesClient
from auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration


class TestJulesClientCloudTask:
    """Test suite for JulesClient cloud-task methods."""

    def test_send_followup_assigns_message_without_paused_state_recovery(self, mock_backend_config):
        client = JulesClient("jules")

        with (
            patch.object(client, "send_message", return_value={"name": "sessions/existing"}) as send_message,
            patch.object(client, "continue_if_paused") as continue_if_paused,
            patch.object(client, "get_task") as get_task,
        ):
            accepted = client.send_followup("existing", "Address the published findings")

        assert accepted is True
        send_message.assert_called_once_with("existing", "Address the published findings")
        continue_if_paused.assert_not_called()
        get_task.assert_not_called()

    @pytest.fixture
    def mock_backend_config(self):
        """Create mock configuration for jules."""
        config = LLMBackendConfiguration()
        jules_config = BackendConfig(
            name="jules",
            backend_type="jules",
            api_key="test-jules-key",
        )
        config.backends["jules"] = jules_config
        return config

    def test_start_task(self, mock_backend_config):
        """Test start_task delegates to start_session."""
        with patch("auto_coder.jules_client.get_llm_config", return_value=mock_backend_config):
            client = JulesClient("jules")

            with patch.object(client, "start_session", return_value="session_abc123") as mock_start:
                tid = client.start_task("Fix bug", repo_name="owner/repo", base_branch="main", title="Task Title")
                assert tid == "session_abc123"
                mock_start.assert_called_once_with(
                    prompt="Fix bug",
                    repo_name="owner/repo",
                    base_branch="main",
                    title="Task Title",
                )

    def test_get_task_normalized(self, mock_backend_config):
        """Test get_task returns normalized CloudTask."""
        with patch("auto_coder.jules_client.get_llm_config", return_value=mock_backend_config):
            client = JulesClient("jules")

            session_data = {
                "name": "projects/p/locations/l/sessions/sess_123",
                "state": "AWAITING_PLAN_APPROVAL",
                "title": "My Task",
                "prompt": "Do work",
                "createTime": "2026-08-19T00:00:00Z",
                "updateTime": "2026-08-19T01:00:00Z",
                "outputs": {"pullRequest": {"number": 42}},
            }

            with patch.object(client, "get_session", return_value=session_data):
                task = client.get_task("sess_123")
                assert task is not None
                assert task.task_id == "sess_123"
                assert task.state == CloudTaskState.PAUSED
                assert task.raw_state == "AWAITING_PLAN_APPROVAL"
                assert task.title == "My Task"
                assert task.pull_request == {"number": 42}
                assert task.url == "https://jules.google.com/session/sess_123"

    def test_continue_if_paused_awaiting_plan_approval(self, mock_backend_config):
        """Test continue_if_paused approves plan when state is AWAITING_PLAN_APPROVAL."""
        with patch("auto_coder.jules_client.get_llm_config", return_value=mock_backend_config):
            client = JulesClient("jules")

            session_data = {
                "name": "projects/p/locations/l/sessions/sess_plan",
                "state": "AWAITING_PLAN_APPROVAL",
            }

            with patch.object(client, "get_session", return_value=session_data):
                with patch.object(client, "approve_plan", return_value=True) as mock_approve:
                    resumed = client.continue_if_paused("sess_plan")
                    assert resumed is True
                    mock_approve.assert_called_once_with("sess_plan")

    def test_continue_if_paused_awaiting_feedback(self, mock_backend_config):
        """Test continue_if_paused sends continuation message when awaiting user feedback."""
        with patch("auto_coder.jules_client.get_llm_config", return_value=mock_backend_config):
            client = JulesClient("jules")

            session_data = {
                "name": "projects/p/locations/l/sessions/sess_feedback",
                "state": "AWAITING_USER_FEEDBACK",
            }

            with patch.object(client, "get_session", return_value=session_data):
                with patch.object(client, "send_message", return_value="ok") as mock_send:
                    resumed = client.continue_if_paused("sess_feedback")
                    assert resumed is True
                    mock_send.assert_called_once_with("sess_feedback", "ok")

    def test_continue_if_paused_completed_with_pr_returns_false(self, mock_backend_config):
        """Test continue_if_paused returns False when task is COMPLETED and has PR."""
        with patch("auto_coder.jules_client.get_llm_config", return_value=mock_backend_config):
            client = JulesClient("jules")

            session_data = {
                "name": "projects/p/locations/l/sessions/sess_done",
                "state": "COMPLETED",
                "outputs": {"pullRequest": {"number": 100}},
            }

            with patch.object(client, "get_session", return_value=session_data):
                resumed = client.continue_if_paused("sess_done")
                assert resumed is False

    def test_stop_task(self, mock_backend_config):
        """Test stop_task sends stop message and ends session."""
        with patch("auto_coder.jules_client.get_llm_config", return_value=mock_backend_config):
            client = JulesClient("jules")

            with patch.object(client, "send_message") as mock_send:
                with patch.object(client, "end_session", return_value=True) as mock_end:
                    stopped = client.stop_task("sess_to_stop")
                    assert stopped is True
                    mock_send.assert_called_once_with("sess_to_stop", "stop")
                    mock_end.assert_called_once_with("sess_to_stop")
