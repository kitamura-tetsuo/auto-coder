"""
End-to-end tests for Auto-Coder.
"""

import json
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.cli import main
from src.auto_coder.gemini_client import GeminiClient
from src.auto_coder.github_client import GitHubClient
from src.auto_coder.utils import CommandResult


class TestE2E:
    """End-to-end test cases for Auto-Coder."""

    @pytest.fixture
    def temp_reports_dir(self):
        """Create temporary reports directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            reports_dir = os.path.join(temp_dir, "reports")
            os.makedirs(reports_dir)
            yield reports_dir

    @pytest.fixture
    def mock_github_responses(self):
        """Mock GitHub API responses."""
        # Mock issue
        mock_issue = Mock()
        mock_issue.number = 1
        mock_issue.title = "Test Bug Report"
        mock_issue.body = "This is a test bug that needs to be fixed"
        mock_issue.state = "open"
        mock_label1 = Mock()
        mock_label1.name = "bug"
        mock_label2 = Mock()
        mock_label2.name = "high-priority"
        mock_issue.labels = [mock_label1, mock_label2]
        mock_assignee = Mock()
        mock_assignee.login = "testuser"
        mock_issue.assignees = [mock_assignee]
        mock_issue.created_at = Mock()
        mock_issue.created_at.isoformat.return_value = "2024-01-01T00:00:00Z"
        mock_issue.updated_at = Mock()
        mock_issue.updated_at.isoformat.return_value = "2024-01-01T00:00:00Z"
        mock_issue.html_url = "https://github.com/test/repo/issues/1"
        mock_issue.user = Mock(login="testuser")
        mock_issue.comments = 0
        mock_issue.pull_request = None

        # Mock PR
        mock_pr = Mock()
        mock_pr.number = 2
        mock_pr.title = "Fix test bug"
        mock_pr.body = "This PR fixes the test bug"
        mock_pr.state = "open"
        mock_pr_label = Mock()
        mock_pr_label.name = "bugfix"
        mock_pr.labels = [mock_pr_label]
        mock_pr_assignee = Mock()
        mock_pr_assignee.login = "testuser"
        mock_pr.assignees = [mock_pr_assignee]
        mock_pr.created_at = Mock()
        mock_pr.created_at.isoformat.return_value = "2024-01-01T01:00:00Z"
        mock_pr.updated_at = Mock()
        mock_pr.updated_at.isoformat.return_value = "2024-01-01T01:00:00Z"
        mock_pr.html_url = "https://github.com/test/repo/pull/2"
        mock_pr.user = Mock(login="testuser")
        mock_pr.head = Mock(ref="fix-bug")
        mock_pr.base = Mock(ref="main")
        mock_pr.mergeable = True
        mock_pr.draft = False
        mock_pr.comments = 0
        mock_pr.review_comments = 0
        mock_pr.commits = 1
        mock_pr.additions = 10
        mock_pr.deletions = 2
        mock_pr.changed_files = 1

        # Mock repository
        mock_repo = Mock()
        mock_repo.name = "test-repo"
        mock_repo.description = "Test repository for e2e testing"
        mock_repo.language = "Python"
        mock_repo.stargazers_count = 50
        mock_repo.forks_count = 10

        return {"issue": mock_issue, "pr": mock_pr, "repo": mock_repo}

    @pytest.fixture
    def mock_gemini_responses(self):
        """Mock Gemini API responses."""
        return {
            "issue_analysis": {
                "category": "bug",
                "priority": "high",
                "complexity": "moderate",
                "estimated_effort": "days",
                "tags": ["backend", "critical"],
                "recommendations": [
                    {
                        "action": "Investigate the root cause of the bug",
                        "rationale": "Understanding the cause will help prevent similar issues",
                    },
                    {
                        "action": "Add unit tests to cover the bug scenario",
                        "rationale": "Tests will prevent regression",
                    },
                ],
                "related_components": ["api", "database"],
                "summary": "Critical bug affecting user authentication flow",
            },
            "pr_analysis": {
                "category": "bugfix",
                "risk_level": "low",
                "review_priority": "high",
                "estimated_review_time": "hours",
                "recommendations": [
                    {
                        "action": "Review the fix implementation carefully",
                        "rationale": "Bug fixes need thorough review",
                    }
                ],
                "potential_issues": ["None identified"],
                "summary": "Bug fix for authentication flow issue",
            },
            "feature_suggestions": [
                {
                    "title": "Add user profile management",
                    "description": "Allow users to manage their profile information including avatar, bio, and preferences",
                    "rationale": "Users need to be able to customize their experience and maintain their profile",
                    "priority": "medium",
                    "complexity": "moderate",
                    "estimated_effort": "weeks",
                    "labels": ["enhancement", "user-experience"],
                    "acceptance_criteria": [
                        "Users can upload and change their avatar",
                        "Users can edit their bio and personal information",
                        "Users can set notification preferences",
                    ],
                }
            ],
            "solution": {
                "solution_type": "code_fix",
                "summary": "Fix authentication flow by updating session validation logic",
                "steps": [
                    {
                        "step": 1,
                        "description": "Update session validation in auth middleware",
                        "commands": ["git checkout -b fix-auth-session"],
                    },
                    {
                        "step": 2,
                        "description": "Add proper error handling for expired sessions",
                        "commands": ["python -m pytest tests/test_auth.py"],
                    },
                ],
                "code_changes": [
                    {
                        "file": "src/auth/middleware.py",
                        "action": "modify",
                        "description": "Update session validation logic",
                        "code": "def validate_session(session_token):\n    if not session_token or is_expired(session_token):\n        raise AuthenticationError('Invalid or expired session')\n    return True",
                    }
                ],
                "testing_strategy": "Add unit tests for session validation and integration tests for auth flow",
                "risks": ["Potential breaking changes to existing auth flow"],
            },
        }

    @patch("src.auto_coder.automation_engine.os.makedirs")
    @patch("src.auto_coder.github_client.Github")
    @patch("src.auto_coder.gemini_client.genai")
    def test_full_automation_workflow_dry_run(
        self,
        mock_genai,
        mock_github_class,
        mock_makedirs,
        mock_github_responses,
        mock_gemini_responses,
        temp_reports_dir,
    ):
        """Test complete automation workflow in dry-run mode."""
        # Setup GitHub mocks
        mock_github = Mock()
        mock_repo = mock_github_responses["repo"]
        mock_issue = mock_github_responses["issue"]
        mock_pr = mock_github_responses["pr"]

        mock_github.get_repo.return_value = mock_repo
        mock_repo.get_issues.return_value = [mock_issue]
        mock_repo.get_pulls.return_value = [mock_pr]
        mock_github_class.return_value = mock_github

        # Setup Gemini mocks
        mock_model = Mock()
        mock_genai.GenerativeModel.return_value = mock_model

        # Create automation engine
        github_client = GitHubClient("test_token")
        gemini_client = GeminiClient("test_key")

        automation_engine = AutomationEngine(github_client, gemini_client, dry_run=True)
        automation_engine.reports_dir = temp_reports_dir

        # Run automation
        result = automation_engine.run("test/repo")

        # Verify results
        assert result["repository"] == "test/repo"
        assert result["dry_run"] is True
        assert len(result["issues_processed"]) == 1
        assert len(result["prs_processed"]) == 1
        assert len(result["errors"]) == 0

        # Verify issue processing (no analysis-only phase)
        issue_result = result["issues_processed"][0]
        assert issue_result["issue_data"]["number"] == 1
        assert issue_result["analysis"] is None
        assert issue_result["solution"] is None
        assert len(issue_result["actions_taken"]) >= 1
        assert any("[DRY RUN]" in action for action in issue_result["actions_taken"])

        # Verify PR processing (no analysis-only phase)
        pr_result = result["prs_processed"][0]
        assert pr_result["pr_data"]["number"] == 2
        assert pr_result["analysis"] is None
        assert len(pr_result["actions_taken"]) >= 1
        assert any("[DRY RUN]" in action for action in pr_result["actions_taken"])

    @patch("src.auto_coder.automation_engine.os.makedirs")
    @patch("src.auto_coder.github_client.Github")
    @patch("src.auto_coder.gemini_client.genai")
    def test_feature_suggestion_workflow(
        self,
        mock_genai,
        mock_github_class,
        mock_makedirs,
        mock_github_responses,
        mock_gemini_responses,
        temp_reports_dir,
    ):
        """Test feature suggestion workflow."""
        # Setup GitHub mocks
        mock_github = Mock()
        mock_repo = mock_github_responses["repo"]
        mock_issue = mock_github_responses["issue"]
        mock_pr = mock_github_responses["pr"]

        mock_github.get_repo.return_value = mock_repo
        mock_repo.get_issues.return_value = [mock_issue]
        mock_repo.get_pulls.return_value = [mock_pr]
        mock_github_class.return_value = mock_github

        # Setup Gemini mocks
        mock_model = Mock()
        mock_response = Mock(
            text=json.dumps(mock_gemini_responses["feature_suggestions"])
        )
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model

        # Create automation engine
        github_client = GitHubClient("test_token")
        gemini_client = GeminiClient("test_key")

        automation_engine = AutomationEngine(github_client, gemini_client, dry_run=True)
        automation_engine.reports_dir = temp_reports_dir

        # Run feature suggestion
        result = automation_engine.create_feature_issues("test/repo")

        # Verify results
        assert len(result) == 1
        assert result[0]["title"] == "Add user profile management"
        assert result[0]["dry_run"] is True

        # Verify that Gemini was called for feature suggestions
        mock_model.generate_content.assert_called_once()

    def test_cli_integration_process_issues(
        self, mock_github_responses, mock_gemini_responses
    ):
        """Test CLI integration for process-issues command."""
        runner = CliRunner()

        with patch(
            "src.auto_coder.cli.GitHubClient"
        ) as mock_github_client_class, patch(
            "src.auto_coder.cli.GeminiClient"
        ) as mock_gemini_client_class, patch(
            "src.auto_coder.cli.AutomationEngine"
        ) as mock_automation_engine_class:
            # Setup mocks
            mock_github_client = Mock()
            mock_gemini_client = Mock()
            mock_automation_engine = Mock()

            mock_github_client_class.return_value = mock_github_client
            mock_gemini_client_class.return_value = mock_gemini_client
            mock_automation_engine_class.return_value = mock_automation_engine

            mock_automation_engine.run.return_value = {
                "repository": "test/repo",
                "issues_processed": 1,
                "prs_processed": 1,
                "errors": [],
            }

            # Execute CLI command
            result = runner.invoke(
                main,
                [
                    "process-issues",
                    "--repo",
                    "test/repo",
                    "--github-token",
                    "test_token",
                    "--backend",
                    "gemini",
                    "--gemini-api-key",
                    "test_key",
                    "--dry-run",
                ],
            )

            # Verify CLI execution
            assert result.exit_code == 0
            assert "Processing repository: test/repo" in result.output
            assert "Dry run mode: True" in result.output

            # Verify that automation engine was called

    def test_cli_process_issues_multi_backend_models(self):
        """Process issues CLI selects correct backend order and model overrides."""
        runner = CliRunner()

        with patch(
            "src.auto_coder.cli.GitHubClient"
        ) as mock_github_client_class, patch(
            "src.auto_coder.cli.AutomationEngine"
        ) as mock_engine_class, patch(
            "src.auto_coder.cli.BackendManager"
        ) as mock_manager_class, patch(
            "src.auto_coder.cli.CodexClient"
        ) as mock_codex_client_class, patch(
            "src.auto_coder.cli.CodexMCPClient"
        ), patch(
            "src.auto_coder.cli.GeminiClient"
        ) as mock_gemini_client_class, patch(
            "src.auto_coder.cli.QwenClient"
        ) as mock_qwen_client_class, patch(
            "src.auto_coder.cli.check_gemini_cli_or_fail"
        ) as mock_check_gemini, patch(
            "src.auto_coder.cli.check_codex_cli_or_fail"
        ) as mock_check_codex, patch(
            "src.auto_coder.cli.check_qwen_cli_or_fail"
        ) as mock_check_qwen:
            mock_manager_class.return_value = SimpleNamespace(close=lambda: None)
            mock_engine = Mock()
            mock_engine.run.return_value = {"repository": "test/repo"}
            mock_engine_class.return_value = mock_engine
            mock_github_client_class.return_value = Mock()

            def _make_stub(kind: str, **attrs):
                base = {"kind": kind, "close": lambda: None}
                base.update(attrs)
                return SimpleNamespace(**base)

            def _codex_side_effect(*args, **kwargs):
                model_name = kwargs.get("model_name", args[0] if args else "codex")
                return _make_stub("codex", model_name=model_name)

            gemini_calls = []

            def _gemini_side_effect(*args, **kwargs):
                if args and "model_name" in kwargs:
                    api_key = args[0]
                    model_name = kwargs["model_name"]
                elif args:
                    api_key = None
                    model_name = args[0]
                else:
                    api_key = kwargs.get("api_key")
                    model_name = kwargs.get("model_name")
                gemini_calls.append({"api_key": api_key, "model_name": model_name})
                return _make_stub("gemini", api_key=api_key, model_name=model_name)

            def _qwen_side_effect(*args, **kwargs):
                model_name = kwargs.get(
                    "model_name", args[0] if args else "qwen3-coder-plus"
                )
                return _make_stub(
                    "qwen",
                    model_name=model_name,
                    openai_api_key=kwargs.get("openai_api_key"),
                    openai_base_url=kwargs.get("openai_base_url"),
                )

            mock_codex_client_class.side_effect = _codex_side_effect
            mock_gemini_client_class.side_effect = _gemini_side_effect
            mock_qwen_client_class.side_effect = _qwen_side_effect
            mock_check_gemini.return_value = None
            mock_check_codex.return_value = None
            mock_check_qwen.return_value = None

            result = runner.invoke(
                main,
                [
                    "process-issues",
                    "--repo",
                    "test/repo",
                    "--github-token",
                    "token",
                    "--backend",
                    "gemini",
                    "--backend",
                    "codex",
                    "--backend",
                    "qwen",
                    "--backend",
                    "gemini",
                    "--gemini-api-key",
                    "gem-key",
                    "--model-gemini",
                    "g-custom",
                    "--model-qwen",
                    "q-custom",
                    "--openai-api-key",
                    "open-key",
                    "--dry-run",
                ],
            )

            assert result.exit_code == 0
            assert (
                "Using backends: gemini, codex, qwen (default: gemini)" in result.output
            )
            assert any(call["model_name"] == "g-custom" for call in gemini_calls)

            backend_kwargs = mock_manager_class.call_args.kwargs
            assert backend_kwargs["default_backend"] == "gemini"
            assert backend_kwargs["order"] == ["gemini", "codex", "qwen"]
            default_client = backend_kwargs["default_client"]
            assert default_client.kind == "gemini"
            assert default_client.api_key == "gem-key"
            assert default_client.model_name == "g-custom"

            factories = backend_kwargs["factories"]
            assert set(factories.keys()) == {"gemini", "codex", "qwen"}
            qwen_instance = factories["qwen"]()
            assert qwen_instance.kind == "qwen"
            assert qwen_instance.model_name == "q-custom"
            assert qwen_instance.openai_api_key == "open-key"

            mock_engine_class.assert_called_once()
            assert mock_engine_class.call_args.kwargs["dry_run"] is True
            mock_engine.run.assert_called_once_with("test/repo")

    def test_cli_process_issues_qwen_prefers_config_providers(self, tmp_path):
        """Ensure CLI wires QwenClient with configured API keys before OAuth."""
        runner = CliRunner()
        config_path = tmp_path / "qwen-providers.toml"
        config_path.write_text(
            """
            [[qwen.providers]]
            name = "modelstudio"
            api_key = "dashscope-xyz"

            [[qwen.providers]]
            name = "openrouter"
            api_key = "openrouter-123"
            model = "qwen/qwen3-coder:free"
            """.strip()
            + "\n",
            encoding="utf-8",
        )

        with patch.dict(
            os.environ, {"AUTO_CODER_QWEN_CONFIG": str(config_path)}
        ), patch("src.auto_coder.cli.GitHubClient") as mock_github_client_class, patch(
            "src.auto_coder.cli.AutomationEngine"
        ) as mock_engine_class, patch(
            "src.auto_coder.cli.BackendManager"
        ) as mock_manager_class, patch(
            "src.auto_coder.cli.CodexClient"
        ) as mock_codex_client_class, patch(
            "src.auto_coder.cli.CodexMCPClient"
        ), patch(
            "src.auto_coder.cli.GeminiClient"
        ), patch(
            "src.auto_coder.cli.check_qwen_cli_or_fail"
        ) as mock_check_qwen, patch(
            "src.auto_coder.qwen_client.CommandExecutor.run_command"
        ) as mock_run_command, patch(
            "src.auto_coder.qwen_client.subprocess.run"
        ) as mock_subprocess_run:
            mock_manager_class.return_value = SimpleNamespace(close=lambda: None)
            mock_engine = Mock()
            mock_engine.run.return_value = {"repository": "test/repo"}
            mock_engine_class.return_value = mock_engine
            mock_github_client_class.return_value = Mock()
            mock_codex_client_class.return_value = SimpleNamespace(
                kind="codex", model_name="codex"
            )
            mock_check_qwen.return_value = None
            mock_subprocess_run.return_value.returncode = 0
            mock_run_command.return_value = CommandResult(True, "", "", 0)

            result = runner.invoke(
                main,
                [
                    "process-issues",
                    "--repo",
                    "test/repo",
                    "--github-token",
                    "token",
                    "--backend",
                    "qwen",
                    "--dry-run",
                ],
            )

            assert result.exit_code == 0
            backend_kwargs = mock_manager_class.call_args.kwargs
            assert "qwen" in backend_kwargs["factories"]

            qwen_factory = backend_kwargs["factories"]["qwen"]
            qwen_instance = qwen_factory()

            output = qwen_instance._run_qwen_cli("hello")

            assert output == ""
            assert mock_run_command.call_count == 1
            first_env = mock_run_command.call_args_list[0].kwargs["env"]
            assert first_env["OPENAI_API_KEY"] == "dashscope-xyz"
            assert (
                first_env["OPENAI_BASE_URL"]
                == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
            )
            assert first_env["OPENAI_MODEL"] == "qwen3-coder-plus"

    def test_cli_process_issues_qwen_exhausts_api_keys_then_oauth(self, tmp_path):
        """Full fallback chain: configured keys are tried before OAuth last."""
        runner = CliRunner()
        config_path = tmp_path / "qwen-providers.toml"
        config_path.write_text(
            """
            [[qwen.providers]]
            name = "modelstudio"
            api_key = "dashscope-xyz"

            [[qwen.providers]]
            name = "openrouter"
            api_key = "openrouter-123"
            model = "qwen/qwen3-coder:free"
            """.strip()
            + "\n",
            encoding="utf-8",
        )

        with patch.dict(
            os.environ, {"AUTO_CODER_QWEN_CONFIG": str(config_path)}
        ), patch("src.auto_coder.cli.GitHubClient") as mock_github_client_class, patch(
            "src.auto_coder.cli.AutomationEngine"
        ) as mock_engine_class, patch(
            "src.auto_coder.cli.BackendManager"
        ) as mock_manager_class, patch(
            "src.auto_coder.cli.CodexClient"
        ) as mock_codex_client_class, patch(
            "src.auto_coder.cli.CodexMCPClient"
        ), patch(
            "src.auto_coder.cli.GeminiClient"
        ), patch(
            "src.auto_coder.cli.check_qwen_cli_or_fail"
        ) as mock_check_qwen, patch(
            "src.auto_coder.qwen_client.CommandExecutor.run_command"
        ) as mock_run_command, patch(
            "src.auto_coder.qwen_client.subprocess.run"
        ) as mock_subprocess_run:
            mock_manager_class.return_value = SimpleNamespace(close=lambda: None)
            mock_engine = Mock()
            mock_engine.run.return_value = {"repository": "test/repo"}
            mock_engine_class.return_value = mock_engine
            mock_github_client_class.return_value = Mock()
            mock_codex_client_class.return_value = SimpleNamespace(
                kind="codex", model_name="codex"
            )
            mock_check_qwen.return_value = None
            mock_subprocess_run.return_value.returncode = 0
            mock_run_command.side_effect = [
                CommandResult(False, "Rate limit", "", 1),
                CommandResult(False, "Rate limit", "", 1),
                CommandResult(True, "OAuth OK", "", 0),
            ]

            result = runner.invoke(
                main,
                [
                    "process-issues",
                    "--repo",
                    "test/repo",
                    "--github-token",
                    "token",
                    "--backend",
                    "qwen",
                    "--dry-run",
                ],
            )

            assert result.exit_code == 0
            backend_kwargs = mock_manager_class.call_args.kwargs
            qwen_factory = backend_kwargs["factories"]["qwen"]
            qwen_instance = qwen_factory()

            output = qwen_instance._run_qwen_cli("hello")

            assert output == "OAuth OK"
            assert mock_run_command.call_count == 3
            first_env = mock_run_command.call_args_list[0].kwargs["env"]
            second_env = mock_run_command.call_args_list[1].kwargs["env"]
            third_env = mock_run_command.call_args_list[2].kwargs["env"]

            assert first_env["OPENAI_API_KEY"] == "dashscope-xyz"
            assert second_env["OPENAI_API_KEY"] == "openrouter-123"
            assert "OPENAI_API_KEY" not in third_env

    def test_cli_integration_process_issues_no_skip_main_update(
        self, mock_github_responses, mock_gemini_responses
    ):
        """Test CLI integration for process-issues with --no-skip-main-update flag."""
        runner = CliRunner()

        with patch(
            "src.auto_coder.cli.GitHubClient"
        ) as mock_github_client_class, patch(
            "src.auto_coder.cli.CodexClient"
        ) as mock_codex_client_class, patch(
            "src.auto_coder.cli.AutomationEngine"
        ) as mock_automation_engine_class, patch(
            "src.auto_coder.cli.check_codex_cli_or_fail"
        ) as mock_check_cli:
            # Setup mocks
            mock_github_client = Mock()
            mock_codex_client = Mock()
            mock_automation_engine = Mock()

            mock_github_client_class.return_value = mock_github_client
            mock_codex_client_class.return_value = mock_codex_client
            mock_automation_engine_class.return_value = mock_automation_engine
            mock_check_cli.return_value = None

            # Execute CLI command with flag
            result = runner.invoke(
                main,
                [
                    "process-issues",
                    "--repo",
                    "test/repo",
                    "--github-token",
                    "test_token",
                    "--dry-run",
                    "--no-skip-main-update",
                ],
            )

            # Verify CLI execution
            assert result.exit_code == 0
            # The human-friendly policy line should be shown
            assert "Main update before fixes when PR checks fail:" in result.output
            assert "ENABLED (--no-skip-main-update)" in result.output

            # Verify that automation engine was called
            mock_automation_engine.run.assert_called_once_with(
                "test/repo", jules_mode=True
            )

    def test_cli_integration_create_feature_issues(
        self, mock_github_responses, mock_gemini_responses
    ):
        """Test CLI integration for create-feature-issues command."""
        runner = CliRunner()

        with patch(
            "src.auto_coder.cli.GitHubClient"
        ) as mock_github_client_class, patch(
            "src.auto_coder.cli.GeminiClient"
        ) as mock_gemini_client_class, patch(
            "src.auto_coder.cli.AutomationEngine"
        ) as mock_automation_engine_class:
            # Setup mocks
            mock_github_client = Mock()
            mock_gemini_client = Mock()
            mock_automation_engine = Mock()

            mock_github_client_class.return_value = mock_github_client
            mock_gemini_client_class.return_value = mock_gemini_client
            mock_automation_engine_class.return_value = mock_automation_engine

            mock_automation_engine.create_feature_issues.return_value = [
                {"title": "New Feature", "dry_run": True}
            ]

            # Execute CLI command
            result = runner.invoke(
                main,
                [
                    "create-feature-issues",
                    "--repo",
                    "test/repo",
                    "--github-token",
                    "test_token",
                    "--gemini-api-key",
                    "test_key",
                ],
            )

            # Verify CLI execution
            assert result.exit_code == 0
            assert (
                "Analyzing repository for feature opportunities: test/repo"
                in result.output
            )

            # Verify that automation engine was called
            mock_automation_engine.create_feature_issues.assert_called_once_with(
                "test/repo"
            )
