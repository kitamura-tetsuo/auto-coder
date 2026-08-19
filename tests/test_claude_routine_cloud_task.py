"""
Unit tests for ClaudeRoutineClient CloudTaskClientBase methods.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.claude_routine_client import (
    ClaudeRoutineClient,
    _load_claude_routine_state,
    _save_claude_routine_state,
)
from auto_coder.cloud_task_client_base import CloudTaskState
from auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration


class TestClaudeRoutineCloudTask:
    """Test suite for ClaudeRoutineClient cloud-task methods."""

    @pytest.fixture
    def mock_backend_config(self):
        """Create mock configuration for claude-routine."""
        config = LLMBackendConfiguration()
        routine_config = BackendConfig(
            name="claude-opus-routine",
            backend_type="claude-routine",
            url="https://api.anthropic.com/v1/claude_code/routines/trig_123/fire",
            claude_code_routine_token="test-routine-token",
        )
        config.backends["claude-opus-routine"] = routine_config
        return config

    def test_start_task(self, mock_backend_config):
        """Test start_task delegates to fire_routine."""
        with patch("auto_coder.claude_routine_client.get_llm_config", return_value=mock_backend_config):
            client = ClaudeRoutineClient("claude-opus-routine")

            with patch.object(client, "fire_routine", return_value=("session_12345", "https://claude.ai/code/session_12345")) as mock_fire:
                tid = client.start_task("Do refactor", repo_name="owner/repo", base_branch="main", title="Refactor Title")
                assert tid == "session_12345"
                mock_fire.assert_called_once_with(
                    "Do refactor",
                    repo_name="owner/repo",
                    base_branch="main",
                    title="Refactor Title",
                )

    def test_get_task_without_pr_is_paused(self, mock_backend_config, tmp_path):
        """Test get_task without a PR is considered paused."""
        state_file = str(tmp_path / "claude_routine_state.json")
        with patch("auto_coder.claude_routine_client.STATE_FILE", state_file):
            with patch("auto_coder.claude_routine_client.get_llm_config", return_value=mock_backend_config):
                client = ClaudeRoutineClient("claude-opus-routine")

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "id": "session_12345",
                    "status": "running",
                    "title": "Routine Run 1",
                    "pull_request": None,
                }

                with patch.object(client.session, "get", return_value=mock_response):
                    task = client.get_task("session_12345")
                    assert task is not None
                    assert task.task_id == "session_12345"
                    assert task.state == CloudTaskState.PAUSED
                    assert task.pull_request is None

    def test_get_task_with_pr_is_completed(self, mock_backend_config, tmp_path):
        """Test get_task with a PR is considered completed."""
        state_file = str(tmp_path / "claude_routine_state.json")
        with patch("auto_coder.claude_routine_client.STATE_FILE", state_file):
            with patch("auto_coder.claude_routine_client.get_llm_config", return_value=mock_backend_config):
                client = ClaudeRoutineClient("claude-opus-routine")

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "id": "session_12345",
                    "status": "running",
                    "title": "Routine Run 1",
                    "pull_request": "https://github.com/owner/repo/pull/50",
                }

                with patch.object(client.session, "get", return_value=mock_response):
                    task = client.get_task("session_12345")
                    assert task is not None
                    assert task.state == CloudTaskState.COMPLETED
                    assert task.pull_request == "https://github.com/owner/repo/pull/50"

    def test_continue_if_paused_when_no_pr_and_1_hour_elapsed(self, mock_backend_config, tmp_path):
        """Test continue_if_paused sends continue message via claude CLI when 1 hour elapsed without PR."""
        state_file = str(tmp_path / "claude_routine_state.json")
        now = time.time()
        _save_claude_routine_state(
            {
                "session_due": {
                    "created_at": now - 3700,  # > 1 hour ago
                    "last_continued_at": 0.0,
                    "continue_count": 0,
                    "pull_request": None,
                }
            },
            state_file=state_file,
        )

        with patch("auto_coder.claude_routine_client.STATE_FILE", state_file):
            with patch("auto_coder.claude_routine_client.get_llm_config", return_value=mock_backend_config):
                client = ClaudeRoutineClient("claude-opus-routine")

                mock_result = MagicMock()
                mock_result.returncode = 0
                mock_result.stdout = "Message sent"

                with patch("auto_coder.claude_routine_client.CommandExecutor.run_command", return_value=mock_result) as mock_run:
                    resumed = client.continue_if_paused("session_due")
                    assert resumed is True

                    cmd = mock_run.call_args[0][0]
                    assert cmd == ["claude", "-p", "--cloud=session_due", "continue"]

                    # Check state was updated
                    updated_state = _load_claude_routine_state(state_file)
                    assert updated_state["session_due"]["continue_count"] == 1
                    assert updated_state["session_due"]["last_continued_at"] > 0

    def test_continue_if_paused_when_pr_exists_returns_false(self, mock_backend_config, tmp_path):
        """Test continue_if_paused returns False when a PR has already been created."""
        state_file = str(tmp_path / "claude_routine_state.json")
        now = time.time()
        _save_claude_routine_state(
            {
                "session_with_pr": {
                    "created_at": now - 7200,
                    "last_continued_at": 0.0,
                    "continue_count": 0,
                    "pull_request": "https://github.com/owner/repo/pull/99",
                }
            },
            state_file=state_file,
        )

        with patch("auto_coder.claude_routine_client.STATE_FILE", state_file):
            with patch("auto_coder.claude_routine_client.get_llm_config", return_value=mock_backend_config):
                client = ClaudeRoutineClient("claude-opus-routine")

                with patch("auto_coder.claude_routine_client.CommandExecutor.run_command") as mock_run:
                    resumed = client.continue_if_paused("session_with_pr")
                    assert resumed is False
                    mock_run.assert_not_called()

    def test_continue_if_paused_too_soon_returns_false(self, mock_backend_config, tmp_path):
        """Test continue_if_paused returns False when less than 1 hour elapsed since start/last continuation."""
        state_file = str(tmp_path / "claude_routine_state.json")
        now = time.time()
        _save_claude_routine_state(
            {
                "session_recent": {
                    "created_at": now - 1800,  # 30 minutes ago
                    "last_continued_at": 0.0,
                    "continue_count": 0,
                    "pull_request": None,
                }
            },
            state_file=state_file,
        )

        with patch("auto_coder.claude_routine_client.STATE_FILE", state_file):
            with patch("auto_coder.claude_routine_client.get_llm_config", return_value=mock_backend_config):
                client = ClaudeRoutineClient("claude-opus-routine")

                with patch("auto_coder.claude_routine_client.CommandExecutor.run_command") as mock_run:
                    resumed = client.continue_if_paused("session_recent")
                    assert resumed is False
                    mock_run.assert_not_called()

    def test_continue_if_paused_after_5_attempts_returns_false(self, mock_backend_config, tmp_path):
        """Test continue_if_paused returns False after reaching 5 attempts."""
        state_file = str(tmp_path / "claude_routine_state.json")
        now = time.time()
        _save_claude_routine_state(
            {
                "session_maxed": {
                    "created_at": now - 10000,
                    "last_continued_at": now - 4000,
                    "continue_count": 5,
                    "pull_request": None,
                }
            },
            state_file=state_file,
        )

        with patch("auto_coder.claude_routine_client.STATE_FILE", state_file):
            with patch("auto_coder.claude_routine_client.get_llm_config", return_value=mock_backend_config):
                client = ClaudeRoutineClient("claude-opus-routine")

                with patch("auto_coder.claude_routine_client.CommandExecutor.run_command") as mock_run:
                    resumed = client.continue_if_paused("session_maxed")
                    assert resumed is False
                    mock_run.assert_not_called()

    def test_stop_task(self, mock_backend_config):
        """Test stop_task cancels session."""
        with patch("auto_coder.claude_routine_client.get_llm_config", return_value=mock_backend_config):
            client = ClaudeRoutineClient("claude-opus-routine")
            client.active_sessions["session_to_cancel"] = "some prompt"

            mock_post_response = MagicMock()
            mock_post_response.status_code = 200

            with patch.object(client.session, "post", return_value=mock_post_response):
                stopped = client.stop_task("session_to_cancel")
                assert stopped is True
                assert "session_to_cancel" not in client.active_sessions
