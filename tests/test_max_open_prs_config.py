"""Tests for configurable open PR threshold for issue processing."""

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from click.testing import CliRunner

from auto_coder.automation_config import AutomationConfig, CandidateProcessingResult
from auto_coder.automation_engine import AutomationEngine
from auto_coder.cli_commands_main import process_issues
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.llm_backend_config import get_process_issues_max_open_prs_for_issues_from_config
from auto_coder.util.github_action import GitHubActionsStatusResult


class TestMaxOpenPRsConfigToml:
    """Tests for reading max_open_prs_for_issues from config.toml."""

    def test_default_value_when_not_in_config(self, tmp_path):
        """Test default value (3) when key is not present in config.toml."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("[process_issues]\nsleep_time = 100\n")

        val = get_process_issues_max_open_prs_for_issues_from_config(config_path=str(config_file))
        assert val == 3

    def test_max_open_prs_for_issues_key(self, tmp_path):
        """Test reading max_open_prs_for_issues from config.toml."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("[process_issues]\nmax_open_prs_for_issues = 5\n")

        val = get_process_issues_max_open_prs_for_issues_from_config(config_path=str(config_file))
        assert val == 5

    def test_max_open_prs_fallback_key(self, tmp_path):
        """Test reading fallback max_open_prs from config.toml."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("[process_issues]\nmax_open_prs = 2\n")

        val = get_process_issues_max_open_prs_for_issues_from_config(config_path=str(config_file))
        assert val == 2

    def test_repo_scoped_override(self, tmp_path, monkeypatch):
        """Test repo-scoped partial override of max_open_prs_for_issues."""
        # Base config in fake home dir
        fake_home = tmp_path / "home"
        base_dir = fake_home / ".auto-coder"
        base_dir.mkdir(parents=True)
        base_config = base_dir / "config.toml"
        base_config.write_text("[process_issues]\nmax_open_prs_for_issues = 3\n")

        # Repo-specific config
        repo_dir = base_dir / "test-owner" / "test-repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / "config.toml").write_text("[process_issues]\nmax_open_prs_for_issues = 1\n")

        monkeypatch.setenv("HOME", str(fake_home))
        # Base repo
        base_val = get_process_issues_max_open_prs_for_issues_from_config(config_path=str(base_config), repo_name=None)
        assert base_val == 3

        # Override repo
        repo_val = get_process_issues_max_open_prs_for_issues_from_config(config_path=str(base_config), repo_name="test-owner/test-repo")
        assert repo_val == 1


class TestAutomationConfigMaxOpenPRs:
    """Tests for AutomationConfig handling of MAX_OPEN_PRS_FOR_ISSUES."""

    def test_default_value(self):
        """Test that default MAX_OPEN_PRS_FOR_ISSUES is 3."""
        config = AutomationConfig(env_override=False)
        assert config.MAX_OPEN_PRS_FOR_ISSUES == 3
        assert config.max_open_prs_for_issues == 3

    def test_explicit_init_param(self):
        """Test passing max_open_prs_for_issues directly to __init__."""
        config = AutomationConfig(env_override=False, max_open_prs_for_issues=7)
        assert config.MAX_OPEN_PRS_FOR_ISSUES == 7
        assert config.max_open_prs_for_issues == 7

    def test_env_var_override(self):
        """Test environment variable AUTO_CODER_MAX_OPEN_PRS_FOR_ISSUES override."""
        with patch.dict(os.environ, {"AUTO_CODER_MAX_OPEN_PRS_FOR_ISSUES": "4"}, clear=False):
            config = AutomationConfig(env_override=True)
            assert config.MAX_OPEN_PRS_FOR_ISSUES == 4
            assert config.max_open_prs_for_issues == 4

    def test_env_var_alias_override(self):
        """Test environment variable AUTO_CODER_MAX_OPEN_PRS alias override."""
        with patch.dict(os.environ, {"AUTO_CODER_MAX_OPEN_PRS": "2"}, clear=False):
            config = AutomationConfig(env_override=True)
            assert config.MAX_OPEN_PRS_FOR_ISSUES == 2
            assert config.max_open_prs_for_issues == 2


class TestAutomationEngineCandidateCollectionWithThreshold:
    """Tests for AutomationEngine._get_candidates behavior with custom threshold."""

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.issue_context.extract_linked_issues_from_pr_body")
    def test_collect_issues_allowed_when_below_threshold(
        self,
        mock_extract_issues,
        mock_check_actions,
    ):
        """When open PR candidates < threshold and all have low priority, issues are collected."""
        mock_extract_issues.return_value = []
        mock_check_actions.return_value = GitHubActionsStatusResult(success=False, ids=[])

        mock_github = MagicMock()
        # 2 open PRs needing fix (priority 1)
        mock_github.get_open_prs_json.return_value = [
            {"number": 1, "title": "PR 1", "head": {"ref": "pr-1"}, "labels": [], "mergeable": True, "created_at": "2024-01-01T00:00:00Z"},
            {"number": 2, "title": "PR 2", "head": {"ref": "pr-2"}, "labels": [], "mergeable": True, "created_at": "2024-01-02T00:00:00Z"},
        ]
        mock_github.get_open_issues_json.return_value = [
            {"number": 10, "title": "Issue 10", "labels": [], "created_at": "2024-01-03T00:00:00Z"},
        ]

        # Config with threshold = 3 (default): 2 < 3, so issues should be collected
        config = AutomationConfig(env_override=False, max_open_prs_for_issues=3)
        engine = AutomationEngine(mock_github, config=config)

        candidates = engine._get_candidates("test/repo")
        # Should contain PR 1, PR 2, and Issue 10
        assert len(candidates) == 3
        candidate_numbers = {c.data["number"] for c in candidates}
        assert candidate_numbers == {1, 2, 10}

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.issue_context.extract_linked_issues_from_pr_body")
    def test_collect_issues_skipped_when_at_or_above_threshold(
        self,
        mock_extract_issues,
        mock_check_actions,
    ):
        """When open PR candidates >= threshold, issues are NOT collected."""
        mock_extract_issues.return_value = []
        mock_check_actions.return_value = GitHubActionsStatusResult(success=False, ids=[])

        mock_github = MagicMock()
        # 2 open PRs needing fix (priority 1)
        mock_github.get_open_prs_json.return_value = [
            {"number": 1, "title": "PR 1", "head": {"ref": "pr-1"}, "labels": [], "mergeable": True, "created_at": "2024-01-01T00:00:00Z"},
            {"number": 2, "title": "PR 2", "head": {"ref": "pr-2"}, "labels": [], "mergeable": True, "created_at": "2024-01-02T00:00:00Z"},
        ]
        # The production REST query returns only matching urgent Issues.
        mock_github.get_open_issues_json.return_value = []

        # Config with threshold = 2: 2 >= 2, so issues should NOT be collected
        config = AutomationConfig(env_override=False, max_open_prs_for_issues=2)
        engine = AutomationEngine(mock_github, config=config)

        candidates = engine._get_candidates("test/repo")
        # Should contain only PR 1 and PR 2 (Issue 10 skipped)
        assert len(candidates) == 2
        candidate_numbers = {c.data["number"] for c in candidates}
        assert candidate_numbers == {1, 2}
        mock_github.get_open_issues_json.assert_called_once_with("test/repo", labels=["urgent"])

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.issue_context.extract_linked_issues_from_pr_body")
    def test_urgent_issue_discovered_when_high_priority_pr_suppresses_full_scan(
        self,
        mock_extract_issues,
        mock_check_actions,
        tmp_path,
    ):
        """PR suppression must use a narrow query and retain its urgent results."""
        mock_extract_issues.return_value = []
        mock_check_actions.return_value = GitHubActionsStatusResult(success=True, ids=[])
        mock_github = MagicMock()
        mock_github.get_open_prs_json.return_value = [
            {
                "number": 1,
                "title": "Ready PR",
                "body": "",
                "head": {"ref": "pr-1"},
                "labels": [],
                "mergeable": True,
                "created_at": "2024-01-01T00:00:00Z",
            }
        ]
        mock_github.get_open_issues_json.return_value = [
            {
                "number": 1680,
                "title": "Emergency repair",
                "body": "",
                "labels": ["urgent"],
                "created_at": "2024-01-02T00:00:00Z",
                "has_open_sub_issues": False,
                "parent_issue_number": None,
                "linked_pr_numbers": [],
            }
        ]
        engine = AutomationEngine(
            mock_github,
            config=AutomationConfig(env_override=False, max_open_prs_for_issues=3),
        )

        candidates = engine._get_candidates("test/repo")

        assert [(candidate.type, candidate.data["number"], candidate.priority) for candidate in candidates] == [
            ("issue", 1680, 3),
            ("pr", 1, 2),
        ]
        mock_github.get_open_issues_json.assert_called_once_with("test/repo", labels=["urgent"])

        slots = ImplementationSlotRepository("test/repo", 1, tmp_path / "slots.json")
        assert slots.start_execution(ImplementationOwner("issue", 99)) is not None
        engine.implementation_slots = slots
        mock_github.get_item_type_strict.return_value = "issue"

        def process_reserved(*_args, **_kwargs):
            assert slots.active_execution_ids(ImplementationOwner("issue", 1680))
            assert slots.start_execution(ImplementationOwner("issue", 1681), allow_urgent_emergency=True) is None
            return CandidateProcessingResult(
                type="issue",
                number=1680,
                title="Emergency repair",
                success=True,
            )

        engine._process_single_candidate_reserved = Mock(side_effect=process_reserved)

        result = engine._process_single_candidate_unified("test/repo", candidates[0], engine.config)

        assert result.success is True
        engine._process_single_candidate_reserved.assert_called_once()

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.issue_context.extract_linked_issues_from_pr_body")
    def test_collect_issues_with_higher_custom_threshold(
        self,
        mock_extract_issues,
        mock_check_actions,
    ):
        """When threshold is raised (e.g. 5), 4 low-priority PRs still allow issue collection."""
        mock_extract_issues.return_value = []
        mock_check_actions.return_value = GitHubActionsStatusResult(success=False, ids=[])

        mock_github = MagicMock()
        # 4 open PRs needing fix (priority 1)
        mock_github.get_open_prs_json.return_value = [{"number": i, "title": f"PR {i}", "head": {"ref": f"pr-{i}"}, "labels": [], "mergeable": True, "created_at": f"2024-01-0{i}T00:00:00Z"} for i in range(1, 5)]
        mock_github.get_open_issues_json.return_value = [
            {"number": 10, "title": "Issue 10", "labels": [], "created_at": "2024-01-05T00:00:00Z"},
        ]

        # Config with threshold = 5: 4 < 5, so issues should be collected
        config = AutomationConfig(env_override=False, max_open_prs_for_issues=5)
        engine = AutomationEngine(mock_github, config=config)

        candidates = engine._get_candidates("test/repo")
        assert len(candidates) == 5
        candidate_numbers = {c.data["number"] for c in candidates}
        assert candidate_numbers == {1, 2, 3, 4, 10}

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.issue_context.extract_linked_issues_from_pr_body")
    def test_collect_issues_zero_threshold(
        self,
        mock_extract_issues,
        mock_check_actions,
    ):
        """When threshold is 0: issues are collected if 0 PRs, skipped if >= 1 PR."""
        mock_extract_issues.return_value = []
        mock_check_actions.return_value = GitHubActionsStatusResult(success=False, ids=[])

        mock_github = MagicMock()
        config = AutomationConfig(env_override=False, max_open_prs_for_issues=0)
        engine = AutomationEngine(mock_github, config=config)

        # Case 1: 0 PRs -> issues collected
        mock_github.get_open_prs_json.return_value = []
        mock_github.get_open_issues_json.return_value = [
            {"number": 10, "title": "Issue 10", "labels": [], "created_at": "2024-01-01T00:00:00Z"},
        ]
        candidates = engine._get_candidates("test/repo")
        assert len(candidates) == 1
        assert candidates[0].data["number"] == 10

        # Case 2: 1 PR -> issues skipped
        mock_github.get_open_prs_json.return_value = [
            {"number": 1, "title": "PR 1", "head": {"ref": "pr-1"}, "labels": [], "mergeable": True, "created_at": "2024-01-01T00:00:00Z"},
        ]
        mock_github.get_open_issues_json.reset_mock()
        mock_github.get_open_issues_json.return_value = []
        candidates = engine._get_candidates("test/repo")
        assert len(candidates) == 1
        assert candidates[0].data["number"] == 1
        mock_github.get_open_issues_json.assert_called_once_with("test/repo", labels=["urgent"])


class TestProcessIssuesCliOption:
    """Tests for CLI option --max-open-prs-for-issues in process_issues."""

    @patch("auto_coder.cli_commands_main.AutomationEngine")
    @patch("auto_coder.cli_commands_main.GitHubClient")
    @patch("auto_coder.cli_commands_main.build_backend_manager_from_config")
    @patch("auto_coder.cli_commands_main.build_message_backend_manager")
    @patch("auto_coder.cli_commands_main.check_backend_prerequisites")
    @patch("auto_coder.cli_commands_main.ensure_test_script_or_fail")
    @patch("auto_coder.cli_commands_main.get_repo_or_detect", return_value="owner/repo")
    @patch("auto_coder.cli_commands_main.get_github_token_or_fail", return_value="fake-token")
    @patch("auto_coder.cli_commands_main.get_current_branch", return_value="main")
    def test_cli_option_passes_threshold(
        self,
        mock_branch,
        mock_token,
        mock_repo,
        mock_test_script,
        mock_prereqs,
        mock_msg_mgr,
        mock_backend_mgr,
        mock_gh_client,
        mock_engine_cls,
    ):
        """Test that --max-open-prs-for-issues CLI flag correctly configures AutomationConfig."""
        mock_mgr_instance = MagicMock()
        mock_mgr_instance._default_backend = "codex"
        mock_mgr_instance._clients = {"codex": MagicMock()}
        mock_mgr_instance._factories = {}
        mock_mgr_instance._all_backends = ["codex"]
        mock_backend_mgr.return_value = mock_mgr_instance
        mock_msg_mgr.return_value = mock_mgr_instance

        mock_engine_instance = MagicMock()
        mock_engine_instance.run.return_value = {"issues_processed": [], "prs_processed": [], "errors": []}
        mock_engine_instance.start_automation = AsyncMock()
        mock_engine_cls.return_value = mock_engine_instance

        runner = CliRunner()
        result = runner.invoke(process_issues, ["--repo", "owner/repo", "--max-open-prs-for-issues", "6", "--disable-webhook"])

        assert result.exit_code == 0
        # Check that AutomationEngine was initialized with config having MAX_OPEN_PRS_FOR_ISSUES == 6
        assert mock_engine_cls.called
        config_passed = mock_engine_cls.call_args[1].get("config") or mock_engine_cls.call_args[0][1]
        assert config_passed.MAX_OPEN_PRS_FOR_ISSUES == 6
        assert config_passed.max_open_prs_for_issues == 6

        # Test alias --max-open-prs
        mock_engine_cls.reset_mock()
        result2 = runner.invoke(process_issues, ["--repo", "owner/repo", "--max-open-prs", "4", "--disable-webhook"])
        assert result2.exit_code == 0
        config_passed2 = mock_engine_cls.call_args[1].get("config") or mock_engine_cls.call_args[0][1]
        assert config_passed2.MAX_OPEN_PRS_FOR_ISSUES == 4
        assert config_passed2.max_open_prs_for_issues == 4
