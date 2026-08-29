"""
Unit tests for CodexCloudClient.
"""

from unittest.mock import MagicMock, patch

import pytest

from auto_coder.cloud_task_client_base import CloudTaskState
from auto_coder.codex_cloud_client import CodexCloudClient
from auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration


class TestCodexCloudClient:
    """Test suite for CodexCloudClient."""

    @pytest.fixture
    def allow_cloud_task(self):
        with patch("auto_coder.codex_cloud_client.codex_cloud_quota_allows_task", return_value=True):
            yield

    @pytest.fixture(autouse=True)
    def _allow_cloud_task(self, allow_cloud_task):
        yield

    @pytest.fixture
    def mock_backend_config(self):
        """Create mock configuration for codex-cloud."""
        config = LLMBackendConfiguration()
        cloud_config = BackendConfig(
            name="codex-cloud",
            backend_type="codex-cloud",
            model="gpt-5.6-terra",
            api_key="test-codex-key",
            environment_id="env_12345",
            attempts=2,
            options=["--dangerously-bypass-approvals-and-sandbox"],
        )
        config.backends["codex-cloud"] = cloud_config
        return config

    def test_init_loads_config(self, mock_backend_config):
        """Test initialization loads configuration."""
        with patch("auto_coder.codex_cloud_client.get_llm_config", return_value=mock_backend_config):
            client = CodexCloudClient("codex-cloud")
            assert client.backend_name == "codex-cloud"
            assert client.model_name == "gpt-5.6-terra"
            assert client.api_key == "test-codex-key"
            assert client.environment_id == "env_12345"
            assert client.attempts == 2
            assert client.options == ["--dangerously-bypass-approvals-and-sandbox"]

    def test_start_task(self, mock_backend_config):
        """Test starting a Codex Cloud task with codex cloud exec."""
        with patch("auto_coder.codex_cloud_client.get_llm_config", return_value=mock_backend_config):
            client = CodexCloudClient("codex-cloud")
            client.environment_id = "env_12345"

            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "Task submitted successfully: https://chatgpt.com/codex/tasks/task_e_6a26c19ac8a88326af83ebfb44b89fe2"
            mock_result.stderr = ""

            with patch("auto_coder.codex_cloud_client.CommandExecutor.run_command", return_value=mock_result) as mock_run:
                tid = client.start_task(
                    "Implement GitHub issue #123",
                    repo_name="owner/repo",
                    base_branch="main",
                    title="Feature X",
                )

                assert tid == "task_e_6a26c19ac8a88326af83ebfb44b89fe2"
                assert tid in client.active_tasks
                assert client.task_urls[tid] == "https://chatgpt.com/codex/tasks/task_e_6a26c19ac8a88326af83ebfb44b89fe2"

                cmd_args = mock_run.call_args[0][0]
                assert cmd_args == [
                    "codex",
                    "cloud",
                    "exec",
                    "--env",
                    "env_12345",
                    "--config",
                    'model="gpt-5.6-terra"',
                    "--attempts",
                    "2",
                    "--branch",
                    "main",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "Implement GitHub issue #123",
                ]

    def test_start_task_requires_environment_id(self, mock_backend_config):
        """Codex Cloud submissions must identify their configured environment."""
        mock_backend_config.backends["codex-cloud"].environment_id = None
        with patch("auto_coder.codex_cloud_client.get_llm_config", return_value=mock_backend_config):
            client = CodexCloudClient("codex-cloud")
            client.environment_id = None
            with pytest.raises(ValueError, match="environment_id"):
                client.start_task("Implement the issue")

    def test_start_task_rejects_failed_submission(self, mock_backend_config):
        """A CLI failure must not be represented by a fabricated task ID."""
        with patch("auto_coder.codex_cloud_client.get_llm_config", return_value=mock_backend_config):
            client = CodexCloudClient("codex-cloud")
            result = MagicMock(returncode=1, stdout="", stderr="environment not found")
            with patch("auto_coder.codex_cloud_client.CommandExecutor.run_command", return_value=result):
                with pytest.raises(RuntimeError, match="environment not found"):
                    client.start_task("Implement the issue")

    def test_start_task_skips_cli_when_weekly_quota_disallows_it(self, mock_backend_config):
        with (
            patch("auto_coder.codex_cloud_client.get_llm_config", return_value=mock_backend_config),
            patch("auto_coder.codex_cloud_client.codex_cloud_quota_allows_task", return_value=False),
            patch("auto_coder.codex_cloud_client.CommandExecutor.run_command") as mock_run,
        ):
            client = CodexCloudClient("codex-cloud")
            with pytest.raises(RuntimeError, match="weekly quota"):
                client.start_task("Implement the issue")
            mock_run.assert_not_called()

    def test_list_tasks(self, mock_backend_config):
        """Test listing tasks with codex cloud list --json."""
        with patch("auto_coder.codex_cloud_client.get_llm_config", return_value=mock_backend_config):
            client = CodexCloudClient("codex-cloud")
            client.environment_id = "env_12345"

            list_result = MagicMock()
            list_result.returncode = 0
            list_result.stdout = '[{"task_id": "task_e_1", "status": "RUNNING", "title": "Task 1"}, {"task_id": "task_e_2", "status": "READY", "title": "Task 2"}]'

            with patch("auto_coder.codex_cloud_client.CommandExecutor.run_command", return_value=list_result) as mock_run:
                tasks = client.list_tasks(repo_name="owner/repo")
                assert len(tasks) == 2
                assert tasks[0].task_id == "task_e_1"
                assert tasks[0].state == CloudTaskState.RUNNING
                assert tasks[1].task_id == "task_e_2"
                # READY is normalized to COMPLETED
                assert tasks[1].state == CloudTaskState.COMPLETED

                cmd_args = mock_run.call_args[0][0]
                assert cmd_args == ["codex", "cloud", "list", "--json", "--env", "env_12345"]

    def test_get_task_status(self, mock_backend_config):
        """Test get_task executing codex cloud status <TASK_ID>."""
        with patch("auto_coder.codex_cloud_client.get_llm_config", return_value=mock_backend_config):
            client = CodexCloudClient("codex-cloud")

            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = '{"task_id": "task_e_123", "status": "READY", "title": "Test Task"}'

            with patch("auto_coder.codex_cloud_client.CommandExecutor.run_command", return_value=mock_result) as mock_run:
                task = client.get_task("task_e_123")
                assert task is not None
                assert task.task_id == "task_e_123"
                assert task.state == CloudTaskState.COMPLETED
                assert task.title == "Test Task"

                cmd_args = mock_run.call_args[0][0]
                assert cmd_args == ["codex", "cloud", "status", "task_e_123"]

    def test_get_diff(self, mock_backend_config):
        """Test get_diff executing codex cloud diff <TASK_ID>."""
        with patch("auto_coder.codex_cloud_client.get_llm_config", return_value=mock_backend_config):
            client = CodexCloudClient("codex-cloud")

            diff_result = MagicMock()
            diff_result.returncode = 0
            diff_result.stdout = "diff --git a/file.py b/file.py\n+new line"

            with patch("auto_coder.codex_cloud_client.CommandExecutor.run_command", return_value=diff_result) as mock_run:
                diff = client.get_diff("task_e_123")
                assert "diff --git" in diff
                cmd_args = mock_run.call_args[0][0]
                assert cmd_args == ["codex", "cloud", "diff", "task_e_123"]

    def test_apply_changes(self, mock_backend_config):
        """Test apply_changes executing codex cloud apply <TASK_ID>."""
        with patch("auto_coder.codex_cloud_client.get_llm_config", return_value=mock_backend_config):
            client = CodexCloudClient("codex-cloud")

            apply_result = MagicMock()
            apply_result.returncode = 0

            with patch("auto_coder.codex_cloud_client.CommandExecutor.run_command", return_value=apply_result) as mock_run:
                success = client.apply_changes("task_e_123")
                assert success is True
                cmd_args = mock_run.call_args[0][0]
                assert cmd_args == ["codex", "cloud", "apply", "task_e_123"]

    def test_continue_if_paused_success(self, mock_backend_config):
        """Test continue_if_paused resolves latest turn and sends default continuation prompt."""
        with patch("auto_coder.codex_cloud_client.get_llm_config", return_value=mock_backend_config):
            client = CodexCloudClient("codex-cloud")
            mock_wham = MagicMock()
            mock_wham.resolve_latest_assistant_turn.return_value = "task_e_123~assttrn_1"
            mock_wham.send_follow_up.return_value = True
            client.wham_client = mock_wham

            success = client.continue_if_paused("task_e_123")
            assert success is True
            mock_wham.resolve_latest_assistant_turn.assert_called_once_with("task_e_123")
            mock_wham.send_follow_up.assert_called_once()
            call_kwargs = mock_wham.send_follow_up.call_args[1]
            assert call_kwargs["task_id"] == "task_e_123"
            assert call_kwargs["turn_id"] == "task_e_123~assttrn_1"
            assert "Review the current pull request status and continue working on it" in call_kwargs["prompt"]

    def test_continue_if_paused_custom_prompt(self, mock_backend_config):
        """Test continue_if_paused with custom prompt."""
        with patch("auto_coder.codex_cloud_client.get_llm_config", return_value=mock_backend_config):
            client = CodexCloudClient("codex-cloud")
            mock_wham = MagicMock()
            mock_wham.resolve_latest_assistant_turn.return_value = "task_e_123~assttrn_1"
            mock_wham.send_follow_up.return_value = True
            client.wham_client = mock_wham

            success = client.continue_if_paused("task_e_123", prompt="Custom fix request")
            assert success is True
            call_kwargs = mock_wham.send_follow_up.call_args[1]
            assert call_kwargs["prompt"] == "Custom fix request"

    def test_continue_if_paused_missing_turn_returns_false(self, mock_backend_config):
        """Test continue_if_paused fails safely when no usable assistant turn exists."""
        with patch("auto_coder.codex_cloud_client.get_llm_config", return_value=mock_backend_config):
            client = CodexCloudClient("codex-cloud")
            mock_wham = MagicMock()
            mock_wham.resolve_latest_assistant_turn.return_value = None
            client.wham_client = mock_wham

            assert client.continue_if_paused("task_e_123") is False
            mock_wham.send_follow_up.assert_not_called()

    def test_continue_if_paused_wham_failure_returns_false(self, mock_backend_config):
        """Test continue_if_paused returns False when WHAM follow-up send fails."""
        with patch("auto_coder.codex_cloud_client.get_llm_config", return_value=mock_backend_config):
            client = CodexCloudClient("codex-cloud")
            mock_wham = MagicMock()
            mock_wham.resolve_latest_assistant_turn.return_value = "task_e_123~assttrn_1"
            mock_wham.send_follow_up.return_value = False
            client.wham_client = mock_wham

            assert client.continue_if_paused("task_e_123") is False

    def test_continue_if_paused_cooldown_prevents_tight_loop(self, mock_backend_config):
        """Test anti-tight-loop cooldown prevents sending multiple follow-ups in quick succession."""
        with patch("auto_coder.codex_cloud_client.get_llm_config", return_value=mock_backend_config):
            client = CodexCloudClient("codex-cloud")
            mock_wham = MagicMock()
            mock_wham.resolve_latest_assistant_turn.return_value = "task_e_123~assttrn_1"
            mock_wham.send_follow_up.return_value = True
            client.wham_client = mock_wham

            # First continuation succeeds
            assert client.continue_if_paused("task_e_123") is True
            assert mock_wham.send_follow_up.call_count == 1

            # Second continuation immediately afterwards is blocked by cooldown
            assert client.continue_if_paused("task_e_123") is False
            assert mock_wham.send_follow_up.call_count == 1

    def test_stop_task(self, mock_backend_config):
        """Test stop_task removes task from active tasks."""
        with patch("auto_coder.codex_cloud_client.get_llm_config", return_value=mock_backend_config):
            client = CodexCloudClient("codex-cloud")
            client.active_tasks["task_to_stop"] = "some prompt"

            stopped = client.stop_task("task_to_stop")
            assert stopped is True
            assert "task_to_stop" not in client.active_tasks
