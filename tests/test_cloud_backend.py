"""
Unit and integration tests for backend_cloud and non-difficult cloud issue routing.
"""

from unittest.mock import ANY, MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine, Candidate
from auto_coder.cli_helpers import create_cloud_backend_manager
from auto_coder.codex_cloud_client import CodexSubmissionOutcome, CodexSubmissionResult
from auto_coder.exceptions import AutoCoderUsageLimitError
from auto_coder.issue_dispatch import DispatchOutcome, DispatchResult, IssueAttemptIdentity
from auto_coder.issue_processor import (
    IssueDispatchExecution,
    _dispatch_issue_candidates,
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
from auto_coder.quota_selector import BackendQuotaEvaluation


class TestCloudBackendConfig:
    """Test configuration parsing and helpers for backend_cloud."""

    def test_create_cloud_backend_manager_no_config(self):
        """Test create_cloud_backend_manager when not configured."""
        with patch("auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            mock_get_config.return_value.get_backend_cloud.return_value = None
            mock_get_config.return_value.backend_cloud_order = []
            mock_get_config.return_value.backend_cloud_priority_groups = []
            manager = create_cloud_backend_manager()
            assert manager is None

    def test_create_cloud_backend_manager_with_order(self):
        """Test create_cloud_backend_manager with order list."""
        with patch("auto_coder.cli_helpers.get_llm_config") as mock_get_config, patch("auto_coder.cli_helpers.build_backend_manager") as mock_build:
            mock_config = MagicMock(spec=LLMBackendConfiguration)
            mock_config.backend_cloud_order = ["codex-cloud-luna", "gemini"]
            mock_config.backend_cloud_priority_groups = []
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
            mock_config.backend_cloud_priority_groups = []
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

    def test_toml_priority_groups_reach_manager_with_group_boundaries(self, tmp_path):
        """TOML groups survive loading and quota-rank only within each group."""
        config_path = tmp_path / "llm_config.toml"
        config_path.write_text(
            """
[backend_cloud]
priority_groups = [["backend-a", "backend-b"], ["backend-c"]]

[backends.backend-a]
model = "model-a"
[backends.backend-b]
model = "model-b"
[backends.backend-c]
model = "model-c"
""",
            encoding="utf-8",
        )
        config = LLMBackendConfiguration.load_from_file(str(config_path))
        surpluses = {"backend-a": 0.1, "backend-b": 0.5, "backend-c": 0.99}

        with (
            patch("auto_coder.cli_helpers.get_llm_config", return_value=config),
            patch("auto_coder.cli_helpers.build_backend_manager") as build_manager,
            patch(
                "auto_coder.quota_selector.evaluate_backend_quota",
                side_effect=lambda backend_name, **kwargs: BackendQuotaEvaluation(
                    backend_name=backend_name,
                    quota_surplus=surpluses[backend_name],
                ),
            ),
        ):
            create_cloud_backend_manager()

        assert config.backend_cloud_priority_groups == [["backend-a", "backend-b"], ["backend-c"]]
        assert build_manager.call_args.kwargs["selected_backends"] == ["backend-b", "backend-a", "backend-c"]
        assert build_manager.call_args.kwargs["primary_backend"] == "backend-b"

    @pytest.mark.parametrize(
        ("priority_groups", "message"),
        [
            ([[], ["backend-a"]], "non-empty backend-name array"),
            ([["backend-a", 7]], "backend name string"),
            (["backend-a"], "non-empty backend-name array"),
        ],
    )
    def test_invalid_priority_groups_are_rejected(self, priority_groups, message):
        with pytest.raises(ValueError, match=message):
            LLMBackendConfiguration.load_from_dict({"backend_cloud": {"priority_groups": priority_groups}})

    def test_order_and_priority_groups_are_rejected_together(self):
        with pytest.raises(ValueError, match=r"backend_cloud\.order and backend_cloud\.priority_groups"):
            LLMBackendConfiguration.load_from_dict({"backend_cloud": {"order": ["backend-a"], "priority_groups": [["backend-b"]]}})

    def test_repository_override_conflict_is_rejected_after_merge(self, tmp_path, monkeypatch):
        base_path = tmp_path / "llm_config.toml"
        base_path.write_text('[backend_cloud]\norder = ["backend-a"]\n', encoding="utf-8")
        override_path = tmp_path / ".auto-coder" / "owner" / "repo" / "llm_config.toml"
        override_path.parent.mkdir(parents=True)
        override_path.write_text('[backend_cloud]\npriority_groups = [["backend-b"]]\n', encoding="utf-8")
        monkeypatch.setenv("HOME", str(tmp_path))

        with pytest.raises(ValueError, match=r"backend_cloud\.order and backend_cloud\.priority_groups"):
            LLMBackendConfiguration.load_from_file(str(base_path), repo_name="owner/repo")

    def test_priority_groups_translate_backend_alias_and_round_trip(self, tmp_path):
        config = LLMBackendConfiguration.load_from_dict({"backend_cloud": {"priority_groups": [["gemini", "jules"]]}})
        assert config.backend_cloud_priority_groups == [["antigravity", "jules"]]

        config_path = tmp_path / "llm_config.toml"
        config.save_to_file(str(config_path))
        restored = LLMBackendConfiguration.load_from_file(str(config_path))
        assert restored.backend_cloud_priority_groups == [["antigravity", "jules"]]

    def test_is_jules_mode_enabled_with_backend_cloud(self):
        """Test is_jules_mode_enabled returns True when backend_cloud_order is configured."""
        mock_llm_config = MagicMock(spec=LLMBackendConfiguration)
        mock_llm_config.backend_cloud_order = ["codex-cloud-luna"]
        mock_llm_config.backend_cloud_priority_groups = []
        mock_llm_config.backend_cloud = None

        with patch("auto_coder.llm_backend_config.get_llm_config", return_value=mock_llm_config):
            assert is_jules_mode_enabled() is True
            assert is_cloud_mode_enabled() is True


class TestNonDifficultCloudIssueRouting:
    """Test handling and routing of non-difficult issues to backend_cloud."""

    @pytest.mark.parametrize("backend_type", ["jules", "claude-routine"])
    def test_remote_acceptance_survives_binding_failure(self, backend_type, tmp_path, monkeypatch):
        """A genuine provider reference remains accepted when secondary tracking fails."""
        monkeypatch.setenv("HOME", str(tmp_path))
        alias = f"{backend_type}-team"
        llm_config = LLMBackendConfiguration.load_from_dict({"backends": {alias: {"backend_type": backend_type, "api_key": "team-key", "url": "https://routine.test"}}})
        github = MagicMock()
        issue = {"number": 2078, "title": "Remote", "body": "Implement", "labels": []}
        provider_reference = "provider-session-2078"

        with (
            patch("auto_coder.llm_backend_config.get_llm_config", return_value=llm_config),
            patch("auto_coder.jules_client.get_llm_config", return_value=llm_config),
            patch("auto_coder.claude_routine_client.get_llm_config", return_value=llm_config),
            patch("auto_coder.issue_processor.get_current_attempt", return_value=0),
            patch("auto_coder.issue_processor.get_commit_log", return_value=""),
            patch("auto_coder.issue_processor.CloudManager.add_session", return_value=False),
            patch("auto_coder.issue_processor.JulesClient.start_session", return_value=provider_reference),
            patch("auto_coder.claude_routine_client.ClaudeRoutineClient.fire_routine", return_value=(provider_reference, None)),
            patch("auto_coder.cli_helpers.build_backend_manager") as fallback,
        ):
            execution = _dispatch_issue_candidates("owner/repo", issue, AutomationConfig(), github, [alias, "codex"])

        assert execution.result.outcome is DispatchOutcome.REMOTE_ACCEPTED
        assert execution.result.provider_reference == provider_reference
        assert execution.result.tracking_complete is False
        fallback.assert_not_called()

        from auto_coder.issue_dispatch import IssueDispatchGuard

        retained = IssueDispatchGuard().inspect(IssueAttemptIdentity("owner", "repo", 2078, "0"))
        assert retained is not None
        assert retained.outcome is DispatchOutcome.REMOTE_ACCEPTED
        assert retained.provider_reference == provider_reference
        assert retained.tracking_complete is False

    def test_local_runtime_error_is_not_reported_completed(self, tmp_path, monkeypatch):
        """A local side effect followed by an execution error remains indeterminate."""
        monkeypatch.setenv("HOME", str(tmp_path))
        llm_config = LLMBackendConfiguration.load_from_dict({"backends": {"local-team": {"backend_type": "codex", "model": "test"}, "remote-next": {"backend_type": "jules"}}})
        side_effect = tmp_path / "edited.txt"

        def fail_after_edit(*_args, **_kwargs):
            side_effect.write_text("preserved", encoding="utf-8")
            raise RuntimeError("local invocation failed after edit")

        with (
            patch("auto_coder.llm_backend_config.get_llm_config", return_value=llm_config),
            patch("auto_coder.issue_processor.get_current_attempt", return_value=0),
            patch("auto_coder.cli_helpers.build_backend_manager", return_value=MagicMock()),
            patch("auto_coder.issue_processor._apply_issue_actions_directly", side_effect=fail_after_edit),
            patch("auto_coder.issue_processor._process_issue_jules_mode") as remote,
        ):
            execution = _dispatch_issue_candidates(
                "owner/repo",
                {"number": 2079, "title": "Local", "labels": []},
                AutomationConfig(),
                MagicMock(),
                ["local-team", "remote-next"],
            )

        assert side_effect.read_text(encoding="utf-8") == "preserved"
        assert execution.result.outcome is DispatchOutcome.INDETERMINATE
        assert "local invocation failed after edit" in execution.result.diagnostic
        remote.assert_not_called()

    def test_jules_alias_uses_selected_credentials_and_tracking_identity(self, tmp_path, monkeypatch):
        """The selected Jules alias owns both transport credentials and binding attribution."""
        from auto_coder.cloud_manager import CloudManager
        from auto_coder.jules_client import JulesClient

        monkeypatch.setenv("HOME", str(tmp_path))
        alias = "jules-team"
        llm_config = LLMBackendConfiguration.load_from_dict(
            {
                "backends": {
                    "jules": {"backend_type": "jules", "api_key": "default-key"},
                    alias: {"backend_type": "jules", "api_key": "team-key", "options": ["team-option"]},
                }
            }
        )
        response = MagicMock(status_code=200, text="accepted")
        response.json.return_value = {"name": "sessions/provider-alias-session"}

        with (
            patch("auto_coder.llm_backend_config.get_llm_config", return_value=llm_config),
            patch("auto_coder.jules_client.get_llm_config", return_value=llm_config),
            patch("auto_coder.issue_processor.get_current_attempt", return_value=0),
            patch("auto_coder.issue_processor.get_commit_log", return_value=""),
        ):
            client = JulesClient(alias)
            with (
                patch.object(client.session, "post", return_value=response) as post,
                patch("auto_coder.issue_processor.JulesClient", return_value=client),
            ):
                execution = _dispatch_issue_candidates(
                    "owner/repo",
                    {"number": 2080, "title": "Alias", "body": "Implement", "labels": []},
                    AutomationConfig(),
                    MagicMock(),
                    [alias],
                )

        assert post.call_count == 1
        assert client.session.headers["X-Goog-Api-Key"] == "team-key"
        assert client.options == ["team-option"]
        assert execution.result.outcome is DispatchOutcome.REMOTE_ACCEPTED
        binding = CloudManager("owner/repo").get_binding(2080)
        assert binding is not None
        assert binding.backend_name == alias
        assert binding.task_id == "provider-alias-session"

    @patch("auto_coder.codex_cloud_client.CodexCloudClient")
    def test_production_ranked_boundary_reaches_aliased_remote_transport(self, mock_client_type, tmp_path, monkeypatch):
        """The shared production boundary retains alias, branch, prompt, and task identity."""
        monkeypatch.setenv("HOME", str(tmp_path))
        llm_config = LLMBackendConfiguration.load_from_dict(
            {
                "backends": {
                    "remote-primary": {
                        "backend_type": "codex-cloud",
                        "model": "gpt-test",
                        "environment_id": "env-test",
                    },
                    "local-fallback": {"backend_type": "codex", "model": "gpt-local"},
                }
            }
        )
        client = mock_client_type.return_value
        client.environment_id = "env-test"
        client.submit_task.return_value = CodexSubmissionResult(
            CodexSubmissionOutcome.ACCEPTED,
            "task_provider_2078",
            "https://chatgpt.com/codex/tasks/task_provider_2078",
        )
        github = MagicMock()
        issue = {"number": 2078, "title": "Dispatch adapters", "body": "Implement requirements", "labels": []}
        automation_config = AutomationConfig()
        automation_config.MAIN_BRANCH = "release"

        with (
            patch("auto_coder.llm_backend_config.get_llm_config", return_value=llm_config),
            patch("auto_coder.codex_cloud_client.get_llm_config", return_value=llm_config),
            patch("auto_coder.issue_processor.get_current_attempt", return_value=3),
            patch("auto_coder.issue_processor.get_commit_log", return_value=""),
        ):
            execution = _dispatch_issue_candidates(
                "owner/repo",
                issue,
                automation_config,
                github,
                ["remote-primary", "local-fallback"],
            )

        assert execution.result.outcome is DispatchOutcome.REMOTE_ACCEPTED
        assert execution.result.backend_name == "remote-primary"
        assert execution.result.provider == "codex-cloud"
        assert execution.result.provider_reference == "task_provider_2078"
        mock_client_type.assert_called_once_with(backend_name="remote-primary", repo_name="owner/repo")
        prompt = client.submit_task.call_args.args[0]
        assert "Issue #2078" in prompt
        assert client.submit_task.call_args.kwargs["repo_name"] == "owner/repo"
        assert client.submit_task.call_args.kwargs["base_branch"] == "release"

    @patch("auto_coder.issue_processor._process_issue_codex_cloud_mode")
    @patch("auto_coder.quota_selector.evaluate_backend_quota")
    def test_toml_priority_groups_rank_within_group_during_issue_dispatch(
        self,
        mock_evaluate,
        mock_codex_cloud_mode,
        tmp_path,
        monkeypatch,
    ):
        """Repository-aware issue dispatch must retain TOML priority boundaries."""
        config_path = tmp_path / "llm_config.toml"
        config_path.write_text(
            """
[backend_cloud]
priority_groups = [["backend-a", "backend-b"], ["backend-c"]]

[backends.backend-a]
backend_type = "codex-cloud"
[backends.backend-b]
backend_type = "codex-cloud"
[backends.backend-c]
backend_type = "codex-cloud"
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("AUTO_CODER_CONFIG_PATH", str(config_path))
        surpluses = {"backend-a": 0.1, "backend-b": 0.5, "backend-c": 0.99}
        mock_evaluate.side_effect = lambda backend_name, **kwargs: BackendQuotaEvaluation(
            backend_name=backend_name,
            quota_surplus=surpluses[backend_name],
        )
        mock_codex_cloud_mode.return_value = ["backend-b dispatched"]
        config = AutomationConfig()
        issue_data = {"number": 10, "title": "Simple fix", "labels": []}
        github_client = MagicMock()

        actions = _process_issue_cloud_backend(
            "owner/repo",
            issue_data,
            config,
            github_client,
        )

        assert actions == ["backend-b dispatched"]
        mock_codex_cloud_mode.assert_called_once_with(
            "owner/repo",
            issue_data,
            config,
            github_client,
            backend_name="backend-b",
            label_context=None,
        )

    @patch("auto_coder.issue_processor.CloudManager")
    @patch("auto_coder.codex_cloud_client.CodexCloudClient")
    def test_codex_cloud_dispatch_persists_and_comments_task_url(self, mock_client_type, mock_cloud_manager_type, tmp_path, monkeypatch):
        """A successful asynchronous dispatch must publish its task on the issue."""
        monkeypatch.setenv("HOME", str(tmp_path))
        client = mock_client_type.return_value
        client.environment_id = "env-test"
        client.submit_task.return_value = CodexSubmissionResult(CodexSubmissionOutcome.ACCEPTED, "task_e_123", "https://chatgpt.com/codex/tasks/task_e_123")
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

        mock_cloud_manager_type.return_value.ensure_binding.assert_called_once()
        github_client.add_comment_to_issue.assert_called_once_with(
            "owner/repo",
            10,
            "I started a Codex Cloud task to work on this issue. Task ID: task_e_123\n\n" "https://chatgpt.com/codex/tasks/task_e_123",
        )
        github_client.add_labels.assert_not_called()
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
            acceptance_observer=ANY,
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
            implementation_slots=None,
            backend_name="jules",
            acceptance_observer=ANY,
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
    @patch("auto_coder.issue_processor._dispatch_issue_candidates")
    @patch("auto_coder.issue_processor._ordinary_issue_candidates", return_value=["jules-alias"])
    @patch("auto_coder.issue_processor._process_issue_high_score_cloud")
    def test_automation_engine_routes_non_difficult_to_backend_cloud(self, mock_high_score_cloud, mock_candidates, mock_dispatch, mock_label_manager):
        """The normal cloud route exposes the shared boundary's structured result."""
        dispatch_result = DispatchResult(
            IssueAttemptIdentity("owner", "repo", 105, "0"),
            DispatchOutcome.REMOTE_ACCEPTED,
            "jules-alias",
            "jules",
            "sessions/real-provider-id",
        )
        mock_dispatch.return_value = IssueDispatchExecution(dispatch_result, ["Cloud action"])
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

        mock_candidates.assert_called_once_with("owner/repo", True)
        mock_dispatch.assert_called_once()
        mock_high_score_cloud.assert_not_called()
        assert result.success is True
        assert result.actions == ["Cloud action"]
        assert result.dispatch_result == dispatch_result
