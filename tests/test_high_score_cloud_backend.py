"""
Unit and integration tests for backend_with_high_score_cloud and difficult label handling.
"""

from unittest.mock import ANY, MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine, Candidate
from auto_coder.cli_helpers import create_high_score_cloud_backend_manager
from auto_coder.issue_processor import _process_issue_claude_routine_mode, _process_issue_high_score_cloud
from auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration


class TestHighScoreCloudBackendConfig:
    """Test configuration parsing and helpers for backend_with_high_score_cloud."""

    def test_create_high_score_cloud_backend_manager_no_config(self):
        """Test create_high_score_cloud_backend_manager when not configured."""
        with patch("auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            mock_get_config.return_value.get_backend_with_high_score_cloud.return_value = None
            mock_get_config.return_value.backend_with_high_score_cloud_order = []
            manager = create_high_score_cloud_backend_manager()
            assert manager is None

    def test_create_high_score_cloud_backend_manager_with_order(self):
        """Test create_high_score_cloud_backend_manager with order list."""
        with patch("auto_coder.cli_helpers.get_llm_config") as mock_get_config, patch("auto_coder.cli_helpers.build_backend_manager") as mock_build:
            mock_config = MagicMock(spec=LLMBackendConfiguration)
            mock_config.backend_with_high_score_cloud_order = ["claude-opus", "codex"]
            mock_config.get_backend_with_high_score_cloud.return_value = None
            mock_config.get_model_for_backend.side_effect = lambda b: "opus" if b == "claude-opus" else "codex"
            mock_get_config.return_value = mock_config

            mock_manager = MagicMock()
            mock_build.return_value = mock_manager

            manager = create_high_score_cloud_backend_manager()
            assert manager == mock_manager
            mock_build.assert_called_once()
            call_args = mock_build.call_args[1]
            assert call_args["selected_backends"] == ["claude-opus", "codex"]
            assert call_args["primary_backend"] == "claude-opus"

    def test_parse_backend_with_high_score_cloud_from_dict(self):
        """Test parsing [backend_with_high_score_cloud] from dictionary/TOML."""
        data = {
            "backend_with_high_score_cloud": {
                "order": ["claude-opus-routine"],
            },
            "backends": {
                "claude-opus-routine": {
                    "backend_type": "claude-routine",
                    "url": "https://api.anthropic.com/v1/claude_code/routines/trig_abc/fire",
                    "claude_code_routine_token": "sk-test",
                }
            },
        }

        config = LLMBackendConfiguration.load_from_dict(data)
        assert config.backend_with_high_score_cloud_order == ["claude-opus-routine"]
        assert "claude-opus-routine" in config.backends
        backend = config.backends["claude-opus-routine"]
        assert backend.backend_type == "claude-routine"
        assert backend.url == "https://api.anthropic.com/v1/claude_code/routines/trig_abc/fire"
        assert backend.claude_code_routine_token == "sk-test"


class TestDifficultIssueHandling:
    """Test handling and routing of issues with the 'difficult' label."""

    @patch("auto_coder.issue_processor.get_commit_log", return_value="feat: initial commit")
    @patch("auto_coder.claude_routine_client.ClaudeRoutineClient")
    @patch("auto_coder.issue_processor.CloudManager")
    def test_process_issue_claude_routine_mode(self, mock_cloud_mgr_cls, mock_routine_client_cls, mock_get_commit_log):
        """Test _process_issue_claude_routine_mode fires routine and records session."""
        mock_routine_client = MagicMock()
        mock_routine_client.fire_routine.return_value = ("session_999", "https://claude.ai/code/session_999")
        mock_routine_client_cls.return_value = mock_routine_client

        mock_cloud_mgr = MagicMock()
        mock_cloud_mgr.add_session.return_value = True
        mock_cloud_mgr_cls.return_value = mock_cloud_mgr

        mock_github = MagicMock()
        mock_github.get_parent_issue_details.return_value = None

        mock_label_ctx = MagicMock()

        config = AutomationConfig()
        issue_data = {
            "number": 42,
            "title": "Complex Algorithm Fix",
            "body": "This is very difficult.",
            "labels": [{"name": "difficult"}, {"name": "bug"}],
            "state": "open",
        }

        actions = _process_issue_claude_routine_mode(
            "owner/repo",
            issue_data,
            config,
            mock_github,
            backend_name="claude-opus-routine",
            label_context=mock_label_ctx,
        )

        mock_routine_client.fire_routine.assert_called_once()
        mock_cloud_mgr.add_session.assert_called_once_with(42, "session_999", provider="claude-routine")
        mock_github.add_comment_to_issue.assert_called_once()
        comment_text = mock_github.add_comment_to_issue.call_args[0][2]
        assert "Session ID: session_999" in comment_text
        assert "https://claude.ai/code/session_999" in comment_text
        mock_label_ctx.keep_label.assert_called_once()
        assert any("Started Claude Routine session" in a for a in actions)

    @patch("auto_coder.issue_processor._process_issue_claude_routine_mode")
    def test_process_issue_high_score_cloud_delegates_to_routine(self, mock_routine_mode):
        """Test _process_issue_high_score_cloud delegates to routine mode when configured."""
        mock_routine_mode.return_value = ["Fired routine"]

        config = AutomationConfig()
        issue_data = {"number": 55, "labels": ["difficult"]}
        mock_github = MagicMock()

        llm_config = LLMBackendConfiguration(
            backend_with_high_score_cloud_order=["claude-opus-routine"],
            backends={
                "claude-opus-routine": BackendConfig(
                    name="claude-opus-routine",
                    backend_type="claude-routine",
                    url="https://api.anthropic.com/fire",
                )
            },
        )

        with patch("auto_coder.llm_backend_config.get_llm_config", return_value=llm_config):
            actions = _process_issue_high_score_cloud(
                "owner/repo",
                issue_data,
                config,
                mock_github,
            )

        assert actions == ["Fired routine"]
        mock_routine_mode.assert_called_once_with(
            "owner/repo",
            issue_data,
            config,
            mock_github,
            backend_name="claude-opus-routine",
            label_context=None,
        )

    @patch("auto_coder.issue_processor._process_issue_codex_cloud_mode")
    def test_process_issue_high_score_cloud_delegates_to_codex_cloud(self, mock_codex_cloud_mode):
        """Codex Cloud uses the asynchronous task path instead of local LLM execution."""
        mock_codex_cloud_mode.return_value = ["Started task"]
        config = AutomationConfig()
        issue_data = {"number": 56, "labels": ["difficult"]}
        mock_github = MagicMock()
        llm_config = LLMBackendConfiguration(
            backend_with_high_score_cloud_order=["codex-cloud"],
            backends={
                "codex-cloud": BackendConfig(
                    name="codex-cloud",
                    backend_type="codex-cloud",
                    environment_id="env_12345",
                )
            },
        )

        with patch("auto_coder.llm_backend_config.get_llm_config", return_value=llm_config):
            actions = _process_issue_high_score_cloud("owner/repo", issue_data, config, mock_github)

        assert actions == ["Started task"]
        mock_codex_cloud_mode.assert_called_once_with(
            "owner/repo",
            issue_data,
            config,
            mock_github,
            backend_name="codex-cloud",
            label_context=None,
        )

    @patch("auto_coder.issue_processor._process_issue_jules_mode")
    @patch("auto_coder.issue_processor._process_issue_claude_routine_mode")
    def test_process_issue_high_score_cloud_failover_on_usage_limit(self, mock_routine_mode, mock_jules_mode):
        """Test that _process_issue_high_score_cloud falls over to next backend when usage limit reached."""
        from auto_coder.exceptions import AutoCoderUsageLimitError

        mock_routine_mode.side_effect = AutoCoderUsageLimitError("5-hour limit reached")
        mock_jules_mode.return_value = ["Jules session started"]

        config = AutomationConfig()
        issue_data = {"number": 55, "labels": ["difficult"]}
        mock_github = MagicMock()

        llm_config = LLMBackendConfiguration(
            backend_with_high_score_cloud_order=["claude-opus-routine", "jules"],
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
            actions = _process_issue_high_score_cloud(
                "owner/repo",
                issue_data,
                config,
                mock_github,
            )

            assert actions == ["Jules session started"]
            mock_routine_mode.assert_called_once()
            mock_jules_mode.assert_called_once()

    @patch("auto_coder.automation_engine.LabelManager")
    @patch("auto_coder.issue_processor._process_issue_high_score_cloud")
    @patch("auto_coder.issue_processor._process_issue_jules_mode")
    def test_automation_engine_routes_difficult_issue_to_high_score_cloud(self, mock_jules_mode, mock_high_score_cloud, mock_label_manager):
        """Test that candidate with difficult label skips Jules mode and routes to high score cloud."""
        mock_high_score_cloud.return_value = ["High score cloud action"]
        mock_ctx = MagicMock()
        mock_ctx.__bool__.return_value = True
        mock_label_manager.return_value.__enter__.return_value = mock_ctx

        mock_github = MagicMock()
        mock_github.get_item_type_strict.return_value = "issue"
        mock_github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: {
            "number": number,
            "body": "",
            "labels": [{"name": "implementation-ready"}, {"name": "difficult"}],
        }
        mock_github.get_all_sub_issues.return_value = []

        config = AutomationConfig()
        engine = AutomationEngine(mock_github, config)

        candidate = Candidate(
            type="issue",
            priority=100,
            data={
                "number": 101,
                "title": "Difficult problem",
                "labels": [{"name": "difficult"}],
            },
        )

        result = engine._process_single_candidate_unified(
            "owner/repo",
            candidate,
            config,
            jules_mode=True,  # Jules mode is ON
        )

        # Should NOT call Jules mode because difficult label is present
        mock_jules_mode.assert_not_called()
        # Should call high score cloud instead
        mock_high_score_cloud.assert_called_once()
        assert result.success is True
        assert result.actions == ["High score cloud action"]

    @patch("auto_coder.automation_engine.LabelManager")
    @patch("auto_coder.issue_processor._process_issue_high_score_cloud")
    @patch("auto_coder.issue_processor._process_issue_jules_mode")
    def test_automation_engine_routes_non_difficult_to_jules(self, mock_jules_mode, mock_high_score_cloud, mock_label_manager):
        """Test that candidate without difficult label routes to Jules when jules_mode is True."""
        mock_jules_mode.return_value = ["Jules action"]
        mock_ctx = MagicMock()
        mock_ctx.__bool__.return_value = True
        mock_label_manager.return_value.__enter__.return_value = mock_ctx

        mock_github = MagicMock()
        mock_github.get_item_type_strict.return_value = "issue"
        mock_github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: {"number": number, "body": "", "labels": [{"name": "implementation-ready"}]}
        mock_github.get_all_sub_issues.return_value = []

        config = AutomationConfig()
        engine = AutomationEngine(mock_github, config)

        candidate = Candidate(
            type="issue",
            priority=100,
            data={
                "number": 102,
                "title": "Simple bug",
                "labels": [{"name": "bug"}],
            },
        )

        result = engine._process_single_candidate_unified(
            "owner/repo",
            candidate,
            config,
            jules_mode=True,
        )

        # Should call Jules mode
        mock_jules_mode.assert_called_once()
        mock_high_score_cloud.assert_not_called()
        assert result.success is True
        assert result.actions == ["Jules action"]

    @patch("auto_coder.automation_engine.LabelManager")
    @patch("auto_coder.issue_processor._process_issue_high_score_cloud")
    @patch("auto_coder.automation_engine.AutomationEngine._take_issue_actions")
    def test_automation_engine_routes_parent_issue_through_cloud_lifecycle(self, mock_take_actions, mock_high_score_cloud, mock_label_manager):
        """Parent verification uses the lifecycle-aware high-score dispatcher."""
        mock_high_score_cloud.return_value = ["Parent verification dispatched"]
        mock_ctx = MagicMock()
        mock_ctx.__bool__.return_value = True
        mock_label_manager.return_value.__enter__.return_value = mock_ctx

        mock_github = MagicMock()
        mock_github.get_item_type_strict.return_value = "issue"
        mock_github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: {"number": number, "body": "", "labels": [{"name": "implementation-ready"}]}
        mock_github.get_all_sub_issues.return_value = [201, 202]

        config = AutomationConfig()
        engine = AutomationEngine(mock_github, config)

        candidate = Candidate(
            type="issue",
            priority=100,
            data={
                "number": 200,
                "title": "Parent Issue",
                "labels": [],
            },
        )

        result = engine._process_single_candidate_unified(
            "owner/repo",
            candidate,
            config,
            jules_mode=True,
        )

        mock_high_score_cloud.assert_called_once_with(
            "owner/repo",
            candidate.data,
            config,
            mock_github,
            label_context=ANY,
            implementation_slots=ANY,
        )
        mock_take_actions.assert_not_called()
        assert result.success is True
        assert result.actions == ["Parent verification dispatched"]
