"""
Unit and integration tests for backend_cloud and non-difficult cloud issue routing.
"""

from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine, Candidate
from auto_coder.cli_helpers import create_cloud_backend_manager
from auto_coder.exceptions import AutoCoderUsageLimitError
from auto_coder.issue_processor import (
    _process_issue_claude_routine_mode,
    _process_issue_cloud_backend,
    _process_issue_codex_cloud_mode,
    _process_issue_jules_mode,
)
from auto_coder.llm_backend_config import (
    BackendConfig,
    LLMBackendConfiguration,
    is_cloud_mode_enabled,
    is_jules_mode_enabled,
)


class TestCloudBackendConfig:
    """Test configuration parsing and helpers for backend_cloud."""

    def test_create_cloud_backend_manager_no_config(self):
        """Test create_cloud_backend_manager when not configured."""
        with patch("auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            mock_get_config.return_value.get_backend_cloud.return_value = None
            mock_get_config.return_value.backend_cloud_order = []
            manager = create_cloud_backend_manager()
            assert manager is None

    def test_create_cloud_backend_manager_with_order(self):
        """Test create_cloud_backend_manager with order list."""
        with patch("auto_coder.cli_helpers.get_llm_config") as mock_get_config, patch("auto_coder.cli_helpers.build_backend_manager") as mock_build:
            mock_config = MagicMock(spec=LLMBackendConfiguration)
            mock_config.backend_cloud_order = ["codex-cloud-luna", "gemini"]
            mock_config.get_backend_cloud.return_value = None
            mock_config.get_model_for_backend.side_effect = lambda b: "gpt-5.6-luna" if b == "codex-cloud-luna" else "gemini-2.5-flash"
            mock_get_config.return_value = mock_config

            mock_manager = MagicMock()
            mock_build.return_value = mock_manager

            manager = create_cloud_backend_manager()
            assert manager == mock_manager
            mock_build.assert_called_once()
            call_args = mock_build.call_args[1]
            assert call_args["selected_backends"] == ["codex-cloud-luna", "gemini"]
            assert call_args["primary_backend"] == "codex-cloud-luna"

    def test_create_cloud_backend_manager_with_single_backend(self):
        """Test create_cloud_backend_manager with single backend_cloud definition."""
        with patch("auto_coder.cli_helpers.get_llm_config") as mock_get_config, patch("auto_coder.cli_helpers.build_backend_manager") as mock_build:
            mock_config = MagicMock(spec=LLMBackendConfiguration)
            mock_config.backend_cloud_order = []
            mock_backend = BackendConfig(name="codex-cloud-luna", model="gpt-5.6-luna")
            mock_config.get_backend_cloud.return_value = mock_backend
            mock_get_config.return_value = mock_config

            mock_manager = MagicMock()
            mock_build.return_value = mock_manager

            manager = create_cloud_backend_manager()
            assert manager == mock_manager
            mock_build.assert_called_once()
            call_args = mock_build.call_args[1]
            assert call_args["selected_backends"] == ["codex-cloud-luna"]
            assert call_args["primary_backend"] == "codex-cloud-luna"

    def test_parse_backend_cloud_from_dict(self):
        """Test parsing [backend_cloud] from dictionary/TOML."""
        data = {
            "backend_cloud": {
                "order": ["codex-cloud-luna"],
            },
            "backends": {
                "codex-cloud-luna": {
                    "backend_type": "codex-cloud",
                    "model": "gpt-5.6-luna",
                    "environment_id": "env_12345",
                    "attempts": 1,
                }
            },
        }

        config = LLMBackendConfiguration.load_from_dict(data)
        assert config.backend_cloud_order == ["codex-cloud-luna"]
        assert "codex-cloud-luna" in config.backends
        backend = config.backends["codex-cloud-luna"]
        assert backend.backend_type == "codex-cloud"
        assert backend.model == "gpt-5.6-luna"
        assert backend.environment_id == "env_12345"
        assert backend.attempts == 1

    def test_is_jules_mode_enabled_with_backend_cloud(self):
        """Test is_jules_mode_enabled returns True when backend_cloud_order is configured."""
        mock_llm_config = MagicMock(spec=LLMBackendConfiguration)
        mock_llm_config.backend_cloud_order = ["codex-cloud-luna"]
        mock_llm_config.backend_cloud = None

        with patch("auto_coder.llm_backend_config.get_llm_config", return_value=mock_llm_config):
            assert is_jules_mode_enabled() is True
            assert is_cloud_mode_enabled() is True


class TestNonDifficultCloudIssueRouting:
    """Test handling and routing of non-difficult issues to backend_cloud."""

    @patch("auto_coder.issue_processor.CloudManager")
    @patch("auto_coder.codex_cloud_client.CodexCloudClient")
    def test_codex_cloud_dispatch_persists_and_comments_task_url(self, mock_client_type, mock_cloud_manager_type, tmp_path, monkeypatch):
        """A successful asynchronous dispatch must publish its task on the issue."""
        monkeypatch.setenv("HOME", str(tmp_path))
        client = mock_client_type.return_value
        client.start_task.return_value = "task_e_123"
        client.task_urls = {"task_e_123": "https://chatgpt.com/codex/tasks/task_e_123"}
        github_client = MagicMock()
        label_context = MagicMock()

        with patch("auto_coder.issue_processor.get_commit_log", return_value=""), patch("auto_coder.issue_processor.get_current_attempt", return_value=0):
            actions = _process_issue_codex_cloud_mode(
                "owner/repo",
                {"number": 10, "title": "Simple fix", "body": "Fix it", "labels": []},
                AutomationConfig(),
                github_client,
                backend_name="codex-cloud-luna",
                label_context=label_context,
            )

        mock_cloud_manager_type.return_value.add_session.assert_called_once_with(10, "task_e_123")
        github_client.add_comment_to_issue.assert_called_once_with(
            "owner/repo",
            10,
            "I started a Codex Cloud task to work on this issue. Task ID: task_e_123\n\n" "https://chatgpt.com/codex/tasks/task_e_123",
        )
        github_client.add_labels.assert_called_once_with("owner/repo", 10, ["@auto-coder"])
        label_context.keep_label.assert_called_once_with()
        assert actions == ["Started Codex Cloud task 'task_e_123' for issue #10"]

        from auto_coder.cloud_run import CloudRunRepository

        persisted = CloudRunRepository("owner/repo").get(issue_number=10, attempt=0)
        assert persisted is not None
        assert persisted.provider == "codex-cloud"
        assert persisted.task_id == "task_e_123"

    @patch("auto_coder.issue_processor._process_issue_codex_cloud_mode")
    def test_process_issue_cloud_backend_delegates_to_codex_cloud(self, mock_codex_cloud_mode):
        """Test _process_issue_cloud_backend delegates to codex-cloud when configured in backend_cloud."""
        mock_codex_cloud_mode.return_value = ["Codex cloud task started"]

        config = AutomationConfig()
        issue_data = {"number": 10, "title": "Simple fix", "labels": []}
        mock_github = MagicMock()

        llm_config = LLMBackendConfiguration(
            backend_cloud_order=["codex-cloud-luna"],
            backends={
                "codex-cloud-luna": BackendConfig(
                    name="codex-cloud-luna",
                    backend_type="codex-cloud",
                    model="gpt-5.6-luna",
                    environment_id="env_123",
                )
            },
        )

        with patch("auto_coder.llm_backend_config.get_llm_config", return_value=llm_config):
            actions = _process_issue_cloud_backend(
                "owner/repo",
                issue_data,
                config,
                mock_github,
            )

        assert actions == ["Codex cloud task started"]
        mock_codex_cloud_mode.assert_called_once_with(
            "owner/repo",
            issue_data,
            config,
            mock_github,
            backend_name="codex-cloud-luna",
            label_context=None,
        )

    @patch("auto_coder.issue_processor._process_issue_claude_routine_mode")
    def test_process_issue_cloud_backend_delegates_to_claude_routine(self, mock_claude_routine_mode):
        """Test _process_issue_cloud_backend delegates to claude-routine when configured."""
        mock_claude_routine_mode.return_value = ["Claude routine session started"]

        config = AutomationConfig()
        issue_data = {"number": 11, "title": "Routine task", "labels": []}
        mock_github = MagicMock()

        llm_config = LLMBackendConfiguration(
            backend_cloud_order=["claude-opus-routine"],
            backends={
                "claude-opus-routine": BackendConfig(
                    name="claude-opus-routine",
                    backend_type="claude-routine",
                    url="https://api.anthropic.com/fire",
                )
            },
        )

        with patch("auto_coder.llm_backend_config.get_llm_config", return_value=llm_config):
            actions = _process_issue_cloud_backend(
                "owner/repo",
                issue_data,
                config,
                mock_github,
            )

        assert actions == ["Claude routine session started"]
        mock_claude_routine_mode.assert_called_once_with(
            "owner/repo",
            issue_data,
            config,
            mock_github,
            backend_name="claude-opus-routine",
            label_context=None,
        )

    @patch("auto_coder.issue_processor._process_issue_jules_mode")
    def test_process_issue_cloud_backend_defaults_to_jules(self, mock_jules_mode):
        """Test _process_issue_cloud_backend defaults to Jules when backend_cloud is empty."""
        mock_jules_mode.return_value = ["Jules session started"]

        config = AutomationConfig()
        issue_data = {"number": 12, "title": "Default task", "labels": []}
        mock_github = MagicMock()

        llm_config = LLMBackendConfiguration()

        with patch("auto_coder.llm_backend_config.get_llm_config", return_value=llm_config):
            actions = _process_issue_cloud_backend(
                "owner/repo",
                issue_data,
                config,
                mock_github,
            )

        assert actions == ["Jules session started"]
        mock_jules_mode.assert_called_once_with(
            "owner/repo",
            issue_data,
            config,
            mock_github,
            label_context=None,
        )

    @patch("auto_coder.issue_processor._process_issue_jules_mode")
    @patch("auto_coder.issue_processor._process_issue_claude_routine_mode")
    def test_process_issue_cloud_backend_failover(self, mock_claude_routine, mock_jules_mode):
        """Test failover in backend_cloud when first backend hits usage limit."""
        mock_claude_routine.side_effect = AutoCoderUsageLimitError("5-hour limit reached")
        mock_jules_mode.return_value = ["Jules session started"]

        config = AutomationConfig()
        issue_data = {"number": 13, "title": "Failover task", "labels": []}
        mock_github = MagicMock()

        llm_config = LLMBackendConfiguration(
            backend_cloud_order=["claude-opus-routine", "jules"],
            backends={
                "claude-opus-routine": BackendConfig(
                    name="claude-opus-routine",
                    backend_type="claude-routine",
                    url="https://api.anthropic.com/fire",
                ),
                "jules": BackendConfig(
                    name="jules",
                    backend_type="jules",
                ),
            },
        )

        with patch("auto_coder.llm_backend_config.get_llm_config", return_value=llm_config):
            actions = _process_issue_cloud_backend(
                "owner/repo",
                issue_data,
                config,
                mock_github,
            )

        assert actions == ["Jules session started"]
        mock_claude_routine.assert_called_once()
        mock_jules_mode.assert_called_once()

    @patch("auto_coder.automation_engine.LabelManager")
    @patch("auto_coder.issue_processor._process_issue_cloud_backend")
    @patch("auto_coder.issue_processor._process_issue_high_score_cloud")
    def test_automation_engine_routes_non_difficult_to_backend_cloud(self, mock_high_score_cloud, mock_cloud_backend, mock_label_manager):
        """Test that candidate without difficult label routes to _process_issue_cloud_backend."""
        mock_cloud_backend.return_value = ["Cloud action"]
        mock_ctx = MagicMock()
        mock_ctx.__bool__.return_value = True
        mock_label_manager.return_value.__enter__.return_value = mock_ctx

        mock_github = MagicMock()
        mock_github.get_all_sub_issues.return_value = []

        config = AutomationConfig()
        engine = AutomationEngine(mock_github, config)

        candidate = Candidate(
            type="issue",
            priority=100,
            data={
                "number": 105,
                "title": "Non difficult issue",
                "labels": [{"name": "enhancement"}],
            },
        )

        result = engine._process_single_candidate_unified(
            "owner/repo",
            candidate,
            config,
            jules_mode=True,
        )

        mock_cloud_backend.assert_called_once()
        mock_high_score_cloud.assert_not_called()
        assert result.success is True
        assert result.actions == ["Cloud action"]
