"""Comprehensive regression tests for automatic test-failure repair loop kill switch (Issue #1817 / Parent #1811).

Covers:
- REQ-001 / AS-001: When automatic_test_fix is false, production PR processing running local tests
  does not invoke repair LLM/backend, does not start or continue test-fix iterations, and does not
  produce repair commits or pushes; failing test result remains failing.
- REQ-001 / REQ-002 / AS-002: Given authoritative failing GitHub Actions check evidence and
  automatic_test_fix=false, production PR processing reaches the repair path, suppresses repair,
  and preserves the failing check state as blocking merge eligibility.
- REQ-003 / AS-003: Repeated processing of a failing PR while disabled does not increment fix attempt
  counters or consume MAX_FIX_ATTEMPTS retry budget.
- REQ-002 / REQ-004 / AS-004: Tests and CI checks may still execute and be observed for merge eligibility;
  real test results are preserved while repair actions are suppressed.
- REQ-005 / AS-005: Bypassing automatic repair does not reset, revert, or destructively clean existing
  workspace/branch state; it stops cleanly before repair-owned mutation.
- REQ-004 / AS-006: Other PR automation (mergeability remediation, adversarial validation, review-thread
  gating, auto-merge) operates independently according to each feature's own contract.
- REQ-003 / REQ-006 / AS-007: Re-enabling automatic_test_fix resumes ordinary repair policy with the
  full genuine configured attempt budget unaffected by prior disabled encounters.
- REQ-007: Regression tests originate from production PR-processing paths (process_pull_request)
  for both local-test and GitHub Actions failures through the repair decision point.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig, EmptyPRResult, PRProcessingOutcome, StaleJulesPRResult
from auto_coder.automation_engine import AutomationEngine
from auto_coder.fix_to_pass_tests_runner import fix_to_pass_tests
from auto_coder.llm_backend_config import (
    get_automatic_test_fix_from_config,
    get_feature_switch_from_config,
)
from auto_coder.pr_processor import (
    _fix_pr_issues_with_github_actions_testing,
    _fix_pr_issues_with_local_testing,
    _fix_pr_issues_with_testing,
    _is_automatic_test_fix_enabled,
    process_pull_request,
)
from auto_coder.util.gh_cache import GitHubClient
from auto_coder.util.github_action import GitHubActionsStatusResult


def _pr_data(
    number: int = 100,
    head_sha: str = "a" * 40,
    mergeable: bool = True,
    labels: list | None = None,
    linked_issue: int = 42,
    ref: str = "issue-42",
) -> dict:
    return {
        "number": number,
        "title": f"Feature PR #{number}",
        "body": f"<!-- auto-coder:local-llm -->\nFixes #{linked_issue}" if linked_issue else "<!-- auto-coder:local-llm -->",
        "head": {"ref": ref, "sha": head_sha},
        "labels": labels or [],
        "mergeable": mergeable,
        "created_at": "2024-01-01T00:00:00Z",
    }


@pytest.fixture(autouse=True)
def mock_default_github_client():
    with patch("auto_coder.util.gh_cache.GitHubClient") as mock_gh_cls:
        client_inst = MagicMock()
        client_inst.token = "fake-token"
        mock_gh_cls.get_instance.return_value = client_inst
        yield client_inst


# ---------------------------------------------------------------------------
# Configuration & Switch Resolution Tests
# ---------------------------------------------------------------------------


class TestAutomaticTestFixConfig:
    """Canonical switch automatic_test_fix configuration and resolution."""

    def test_canonical_switch_defaults_to_true(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("AUTO_CODER_AUTOMATIC_TEST_FIX", raising=False)
        config = AutomationConfig()
        assert config.automatic_test_fix is True
        assert get_automatic_test_fix_from_config() is True

    def test_canonical_switch_explicit_false(self):
        config = AutomationConfig(automatic_test_fix=False)
        assert config.automatic_test_fix is False

    def test_canonical_env_var_false(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("AUTO_CODER_AUTOMATIC_TEST_FIX", "false")
        config = AutomationConfig()
        assert config.automatic_test_fix is False
        assert get_automatic_test_fix_from_config() is False

    def test_canonical_env_var_true(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("AUTO_CODER_AUTOMATIC_TEST_FIX", "true")
        config = AutomationConfig()
        assert config.automatic_test_fix is True
        assert get_automatic_test_fix_from_config() is True

    def test_config_toml_repo_scoped_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        home_dir = tmp_path / "home"
        auto_coder_dir = home_dir / ".auto-coder"
        auto_coder_dir.mkdir(parents=True)
        base_config = auto_coder_dir / "config.toml"
        base_config.write_text(
            """
[features]
automatic_test_fix = true
""",
            encoding="utf-8",
        )

        repo_dir = auto_coder_dir / "custom" / "repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / "config.toml").write_text(
            """
[features]
automatic_test_fix = false
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.delenv("AUTO_CODER_AUTOMATIC_TEST_FIX", raising=False)

        assert get_feature_switch_from_config("automatic_test_fix", repo_name="other/repo") is True
        assert get_feature_switch_from_config("automatic_test_fix", repo_name="custom/repo") is False
        assert get_automatic_test_fix_from_config(repo_name="custom/repo") is False

    def test_is_automatic_test_fix_enabled_respects_repo_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        home_dir = tmp_path / "home"
        auto_coder_dir = home_dir / ".auto-coder"
        auto_coder_dir.mkdir(parents=True)
        base_config = auto_coder_dir / "config.toml"
        base_config.write_text(
            """
[features]
automatic_test_fix = true
""",
            encoding="utf-8",
        )

        repo_disabled_dir = auto_coder_dir / "disabled" / "repo"
        repo_disabled_dir.mkdir(parents=True)
        (repo_disabled_dir / "config.toml").write_text(
            """
[features]
automatic_test_fix = false
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.delenv("AUTO_CODER_AUTOMATIC_TEST_FIX", raising=False)

        config = AutomationConfig(automatic_test_fix=True)
        assert _is_automatic_test_fix_enabled(config, "enabled/repo") is True
        assert _is_automatic_test_fix_enabled(config, "disabled/repo") is False

    def test_engine_is_automatic_test_fix_enabled(self):
        mock_client = MagicMock(spec=GitHubClient)
        engine = AutomationEngine(mock_client)
        engine.config = AutomationConfig(automatic_test_fix=False)
        assert engine._is_automatic_test_fix_enabled("any/repo") is False


# ---------------------------------------------------------------------------
# AS-001 / REQ-001 / REQ-002 / REQ-007: Local test failure with repair disabled
# ---------------------------------------------------------------------------


class TestAS001LocalTestFailureWithRepairDisabled:
    """AS-001: Local tests fail, automatic_test_fix=false, no repair LLM invoked, no commit/push, remains failing."""

    @patch("auto_coder.pr_processor.BranchManager")
    @patch("auto_coder.pr_processor._create_github_action_log_summary", return_value=("FAILED test_feature.py", ["test_feature.py"]))
    @patch("auto_coder.pr_processor.get_detailed_checks_from_history")
    @patch("auto_coder.pr_processor.cmd")
    @patch("auto_coder.pr_processor.run_local_tests")
    @patch("auto_coder.pr_processor._apply_local_test_fix")
    @patch("auto_coder.pr_processor.git_commit_with_retry")
    @patch("auto_coder.pr_processor.git_push")
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._close_empty_pr", return_value=EmptyPRResult(closed=False))
    @patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult(closed=False))
    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    def test_production_pr_processing_local_test_failure_suppresses_repair(
        self,
        mock_exit_in_progress,
        mock_jules_close,
        mock_empty_close,
        mock_checks,
        mock_git_push,
        mock_git_commit,
        mock_apply_local_fix,
        mock_run_local_tests,
        mock_cmd,
        mock_detailed_checks,
        mock_log_summary,
        mock_branch_manager,
    ):
        head_sha = "a" * 40
        pr_data = _pr_data(100, head_sha, ref="issue-42")
        client = MagicMock(spec=GitHubClient)

        # CI check fails
        mock_checks.return_value = GitHubActionsStatusResult(success=False, ids=[1])
        mock_detailed = MagicMock()
        mock_detailed.failed_checks = [{"id": 1, "name": "test", "conclusion": "failure"}]
        mock_detailed_checks.return_value = mock_detailed

        # Current branch is already the PR branch
        mock_cmd.run_command.side_effect = lambda cmd_list, **kwargs: (MagicMock(success=True, stdout="issue-42\n") if "branch" in cmd_list else MagicMock(success=True, stdout=""))

        # Local tests fail
        mock_run_local_tests.return_value = {
            "success": False,
            "output": "FAILED test_feature.py::test_case",
            "errors": "AssertionError: expected 1 got 2",
            "returncode": 1,
            "command": "pytest",
            "test_file": "test_feature.py",
        }

        config = AutomationConfig(automatic_test_fix=False)
        result = process_pull_request(client, config, "owner/repo", pr_data)

        # Local tests WERE executed (REQ-004 / AS-004)
        mock_run_local_tests.assert_called()

        # Repair LLM was NOT invoked (REQ-001 / AS-001)
        mock_apply_local_fix.assert_not_called()

        # No repair commit or push was produced (REQ-001 / AS-001)
        mock_git_commit.assert_not_called()
        mock_git_push.assert_not_called()

        # Failing test result remains failing (REQ-002): not merged, outcome not success
        assert result.outcome != PRProcessingOutcome.SUCCESS
        assert not any("Successfully merged" in a for a in result.actions_taken)
        assert any("Automatic test fix is disabled" in a for a in result.actions_taken)


# ---------------------------------------------------------------------------
# AS-002 / REQ-001 / REQ-002 / REQ-007: GitHub Actions failure with repair disabled
# ---------------------------------------------------------------------------


class TestAS002GitHubActionsFailureWithRepairDisabled:
    """AS-002: PR has failing GHA evidence, automatic_test_fix=false, no repair execution started, remains blocking."""

    @patch("auto_coder.pr_processor.cmd")
    @patch("auto_coder.pr_processor._apply_github_actions_fix")
    @patch("auto_coder.pr_processor._apply_local_test_fix")
    @patch("auto_coder.pr_processor.run_llm_prompt")
    @patch("auto_coder.pr_processor.git_commit_with_retry")
    @patch("auto_coder.pr_processor.git_push")
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.get_detailed_checks_from_history")
    @patch("auto_coder.pr_processor._close_empty_pr", return_value=EmptyPRResult(closed=False))
    @patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult(closed=False))
    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    def test_production_pr_processing_gha_failure_suppresses_repair(
        self,
        mock_exit_in_progress,
        mock_jules_close,
        mock_empty_close,
        mock_detailed_checks,
        mock_checks,
        mock_git_push,
        mock_git_commit,
        mock_run_prompt,
        mock_apply_local_fix,
        mock_apply_gha_fix,
        mock_cmd,
    ):
        head_sha = "b" * 40
        pr_data = _pr_data(101, head_sha, ref="issue-43")
        client = MagicMock(spec=GitHubClient)

        # CI check fails
        mock_checks.return_value = GitHubActionsStatusResult(success=False, ids=[2])
        mock_detailed = MagicMock()
        mock_detailed.failed_checks = [{"id": 2, "name": "test", "conclusion": "failure"}]
        mock_detailed_checks.return_value = mock_detailed

        # Not on the PR branch
        mock_cmd.run_command.return_value = MagicMock(success=True, stdout="main\n")

        config = AutomationConfig(automatic_test_fix=False)
        result = process_pull_request(client, config, "owner/repo", pr_data)

        # No repair LLM/backend invoked (REQ-001 / AS-002)
        mock_apply_gha_fix.assert_not_called()
        mock_apply_local_fix.assert_not_called()
        mock_run_prompt.assert_not_called()

        # No repair commit/push produced (REQ-001)
        mock_git_commit.assert_not_called()
        mock_git_push.assert_not_called()

        # Failing check continues to block processing (REQ-002): not merged
        assert result.outcome != PRProcessingOutcome.SUCCESS
        assert not any("Successfully merged" in a for a in result.actions_taken)
        assert any("Automatic test fix is disabled" in a for a in result.actions_taken)


# ---------------------------------------------------------------------------
# AS-003 / REQ-003: Repeated disabled processing does not spend retry budget
# ---------------------------------------------------------------------------


class TestAS003RepeatedDisabledProcessingBudget:
    """AS-003: Processing failing PR repeatedly with automatic_test_fix=false does not consume MAX_FIX_ATTEMPTS."""

    @patch("auto_coder.pr_processor.BranchManager")
    @patch("auto_coder.pr_processor._create_github_action_log_summary", return_value=("FAILED test_foo.py", ["test_foo.py"]))
    @patch("auto_coder.pr_processor.get_detailed_checks_from_history")
    @patch("auto_coder.pr_processor.cmd")
    @patch("auto_coder.pr_processor.run_local_tests")
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._close_empty_pr", return_value=EmptyPRResult(closed=False))
    @patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult(closed=False))
    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    def test_repeated_processing_does_not_spend_budget(
        self,
        mock_exit_in_progress,
        mock_jules_close,
        mock_empty_close,
        mock_checks,
        mock_run_local_tests,
        mock_cmd,
        mock_detailed_checks,
        mock_log_summary,
        mock_branch_manager,
    ):
        head_sha = "c" * 40
        pr_data = _pr_data(102, head_sha, ref="issue-44")
        client = MagicMock(spec=GitHubClient)

        mock_checks.return_value = GitHubActionsStatusResult(success=False, ids=[3])
        mock_detailed = MagicMock()
        mock_detailed.failed_checks = [{"id": 3, "name": "test", "conclusion": "failure"}]
        mock_detailed_checks.return_value = mock_detailed

        mock_cmd.run_command.side_effect = lambda cmd_list, **kwargs: (MagicMock(success=True, stdout="issue-44\n") if "branch" in cmd_list else MagicMock(success=True, stdout=""))
        mock_run_local_tests.return_value = {
            "success": False,
            "output": "FAILED test_foo.py",
            "errors": "AssertionError",
            "returncode": 1,
            "command": "pytest",
            "test_file": "test_foo.py",
        }

        config = AutomationConfig(automatic_test_fix=False)
        initial_budget = config.MAX_FIX_ATTEMPTS

        # Process PR multiple times
        for _ in range(5):
            result = process_pull_request(client, config, "owner/repo", pr_data)
            assert any("Automatic test fix is disabled" in a for a in result.actions_taken)

        # Budget remains completely untouched
        assert config.MAX_FIX_ATTEMPTS == initial_budget


# ---------------------------------------------------------------------------
# AS-004 / REQ-002 / REQ-004: Tests may still run
# ---------------------------------------------------------------------------


class TestAS004TestsMayStillRun:
    """AS-004: Local tests and CI status checks may still run/be observed, preserving real results."""

    @patch("auto_coder.pr_processor.BranchManager")
    @patch("auto_coder.pr_processor._create_github_action_log_summary", return_value=("FAILED test_bar.py", ["test_bar.py"]))
    @patch("auto_coder.pr_processor.get_detailed_checks_from_history")
    @patch("auto_coder.pr_processor.cmd")
    @patch("auto_coder.pr_processor.run_local_tests")
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._close_empty_pr", return_value=EmptyPRResult(closed=False))
    @patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult(closed=False))
    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    def test_local_tests_run_and_failure_is_preserved(
        self,
        mock_exit_in_progress,
        mock_jules_close,
        mock_empty_close,
        mock_checks,
        mock_run_local_tests,
        mock_cmd,
        mock_detailed_checks,
        mock_log_summary,
        mock_branch_manager,
    ):
        head_sha = "d" * 40
        pr_data = _pr_data(103, head_sha, ref="issue-45")
        client = MagicMock(spec=GitHubClient)

        mock_checks.return_value = GitHubActionsStatusResult(success=False, ids=[4])
        mock_detailed = MagicMock()
        mock_detailed.failed_checks = [{"id": 4, "name": "test", "conclusion": "failure"}]
        mock_detailed_checks.return_value = mock_detailed

        mock_cmd.run_command.side_effect = lambda cmd_list, **kwargs: (MagicMock(success=True, stdout="issue-45\n") if "branch" in cmd_list else MagicMock(success=True, stdout=""))
        mock_run_local_tests.return_value = {
            "success": False,
            "output": "FAILED test_bar.py",
            "errors": "NameError: name 'x' is not defined",
            "returncode": 1,
            "command": "pytest",
            "test_file": "test_bar.py",
        }

        config = AutomationConfig(automatic_test_fix=False)
        result = process_pull_request(client, config, "owner/repo", pr_data)

        # Real test execution was performed
        mock_run_local_tests.assert_called_once()
        # Real failure was recorded
        assert any("Local tests failed" in a for a in result.actions_taken)
        # Failure is not converted to pass
        assert result.outcome != PRProcessingOutcome.SUCCESS


# ---------------------------------------------------------------------------
# AS-005 / REQ-005: Existing workspace is not destructively cleaned by bypass
# ---------------------------------------------------------------------------


class TestAS005WorkspaceNotDestructivelyCleaned:
    """AS-005: Existing workspace or branch state is not reset or reverted by the bypass."""

    @patch("auto_coder.pr_processor.get_detailed_checks_from_history")
    @patch("auto_coder.pr_processor.cmd")
    @patch("auto_coder.pr_processor._checkout_pr_branch")
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._close_empty_pr", return_value=EmptyPRResult(closed=False))
    @patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult(closed=False))
    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    def test_disabled_repair_does_not_checkout_or_clean_workspace_when_not_on_branch(
        self,
        mock_exit_in_progress,
        mock_jules_close,
        mock_empty_close,
        mock_checks,
        mock_checkout,
        mock_cmd,
        mock_detailed_checks,
    ):
        head_sha = "e" * 40
        pr_data = _pr_data(104, head_sha, ref="issue-46")
        client = MagicMock(spec=GitHubClient)

        mock_checks.return_value = GitHubActionsStatusResult(success=False, ids=[5])
        mock_detailed = MagicMock()
        mock_detailed.failed_checks = [{"id": 5, "name": "test", "conclusion": "failure"}]
        mock_detailed_checks.return_value = mock_detailed

        # Current branch is different (main)
        mock_cmd.run_command.return_value = MagicMock(success=True, stdout="main\n")

        config = AutomationConfig(automatic_test_fix=False)
        config.FORCE_CLEAN_BEFORE_CHECKOUT = True  # Even with force clean set

        result = process_pull_request(client, config, "owner/repo", pr_data)

        # Checkout (which would have forcefully reset/cleaned workspace) was completely bypassed
        mock_checkout.assert_not_called()
        assert any("Automatic test fix is disabled" in a for a in result.actions_taken)

    @patch("auto_coder.pr_processor.BranchManager")
    @patch("auto_coder.pr_processor._create_github_action_log_summary", return_value=("FAILED", ["test_x.py"]))
    @patch("auto_coder.pr_processor.get_detailed_checks_from_history")
    @patch("auto_coder.pr_processor.cmd")
    @patch("auto_coder.pr_processor.run_local_tests")
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._close_empty_pr", return_value=EmptyPRResult(closed=False))
    @patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult(closed=False))
    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    def test_disabled_repair_preserves_local_changes_when_on_branch(
        self,
        mock_exit_in_progress,
        mock_jules_close,
        mock_empty_close,
        mock_checks,
        mock_run_local_tests,
        mock_cmd,
        mock_detailed_checks,
        mock_log_summary,
        mock_branch_manager,
    ):
        head_sha = "e" * 40
        pr_data = _pr_data(105, head_sha, ref="issue-47")
        client = MagicMock(spec=GitHubClient)

        mock_checks.return_value = GitHubActionsStatusResult(success=False, ids=[6])
        mock_detailed = MagicMock()
        mock_detailed.failed_checks = [{"id": 6, "name": "test", "conclusion": "failure"}]
        mock_detailed_checks.return_value = mock_detailed

        # Already on branch
        mock_cmd.run_command.side_effect = lambda cmd_list, **kwargs: (MagicMock(success=True, stdout="issue-47\n") if "branch" in cmd_list else MagicMock(success=True, stdout=""))
        mock_run_local_tests.return_value = {
            "success": False,
            "output": "FAILED",
            "errors": "Error",
            "returncode": 1,
            "command": "pytest",
            "test_file": "test_x.py",
        }

        config = AutomationConfig(automatic_test_fix=False)
        process_pull_request(client, config, "owner/repo", pr_data)

        # Verify no git reset --hard or git clean was invoked
        for call_args in mock_cmd.run_command.call_args_list:
            cmd_args = call_args[0][0] if call_args[0] else []
            assert not ("reset" in cmd_args and "--hard" in cmd_args)
            assert not ("clean" in cmd_args and "-fd" in cmd_args)


# ---------------------------------------------------------------------------
# AS-006 / REQ-004: Other PR automation remains independent
# ---------------------------------------------------------------------------


class TestAS006OtherPRAutomationRemainsIndependent:
    """AS-006: Mergeability remediation, adversarial validation, auto-merge unaffected by automatic_test_fix=false."""

    @patch("auto_coder.pr_processor._start_mergeability_remediation", return_value=["Remediation initiated"])
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": False, "merge_state_status": "dirty"})
    @patch("auto_coder.pr_processor._close_empty_pr", return_value=EmptyPRResult(closed=False))
    @patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult(closed=False))
    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    def test_mergeability_remediation_remains_active_when_test_fix_disabled(
        self,
        mock_exit_in_progress,
        mock_jules_close,
        mock_empty_close,
        mock_mergeable_state,
        mock_start_remediation,
    ):
        head_sha = "f" * 40
        pr_data = _pr_data(106, head_sha, mergeable=False)
        client = MagicMock(spec=GitHubClient)

        config = AutomationConfig(automatic_test_fix=False)
        object.__setattr__(config, "ENABLE_MERGEABILITY_REMEDIATION", True)
        result = process_pull_request(client, config, "owner/repo", pr_data)

        mock_start_remediation.assert_called_once_with(106, "dirty", "owner/repo")
        assert any("Remediation initiated" in a for a in result.actions_taken)

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._close_empty_pr", return_value=EmptyPRResult(closed=False))
    @patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult(closed=False))
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_passing_checks_merge_normally_when_test_fix_disabled(
        self,
        mock_merge_pr,
        mock_jules_close,
        mock_empty_close,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        head_sha = "0" * 40
        pr_data = _pr_data(107, head_sha)
        client = MagicMock(spec=GitHubClient)

        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[7])
        client.get_pull_request.return_value = {"head": {"sha": head_sha}}
        client.get_issue.return_value = {"number": 42, "title": "Spec", "body": "Body"}
        client.get_pr_comments.return_value = []
        client.get_pr_reviews_strict.return_value = []
        client.get_pr_review_threads_strict.return_value = []

        config = AutomationConfig(
            automatic_test_fix=False,
            pr_adversarial_validation=False,
            pr_review_thread_gate=False,
        )
        result = process_pull_request(client, config, "owner/repo", pr_data)

        mock_merge_pr.assert_called_once()
        assert result.outcome == PRProcessingOutcome.SUCCESS

    @patch("auto_coder.util.github_action._check_github_actions_status")
    def test_candidate_priority_preserves_priority_1_for_failing_checks(self, mock_check_actions):
        """Failing checks retain candidate priority 1 (REQ-002, REQ-004)."""
        head_sha = "1" * 40
        pr_data = _pr_data(108, head_sha)
        mock_client = MagicMock(spec=GitHubClient)
        mock_client.get_open_prs_json.return_value = [pr_data]
        mock_client.get_open_issues_json.return_value = []
        mock_client.has_linked_pr.return_value = False
        mock_check_actions.return_value = GitHubActionsStatusResult(success=False, ids=[8])

        engine = AutomationEngine(mock_client)
        engine.config = AutomationConfig(automatic_test_fix=False)

        candidates = engine._get_candidates("owner/repo")
        assert len(candidates) == 1
        assert candidates[0].priority == 1


# ---------------------------------------------------------------------------
# AS-007 / REQ-003 / REQ-006: Re-enable after disabled failures
# ---------------------------------------------------------------------------


class TestAS007ReEnableAfterDisabledFailures:
    """AS-007: Re-enabling automatic_test_fix resumes repair with configured budget unaffected."""

    @patch("auto_coder.pr_processor.BranchManager")
    @patch("auto_coder.pr_processor._create_github_action_log_summary", return_value=("FAILED", ["test_y.py"]))
    @patch("auto_coder.pr_processor.get_detailed_checks_from_history")
    @patch("auto_coder.pr_processor.cmd")
    @patch("auto_coder.pr_processor.run_local_tests")
    @patch("auto_coder.pr_processor._apply_local_test_fix")
    @patch("auto_coder.pr_processor.git_commit_with_retry")
    @patch("auto_coder.pr_processor.git_push")
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._close_empty_pr", return_value=EmptyPRResult(closed=False))
    @patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult(closed=False))
    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    def test_re_enable_starts_repair_with_full_budget(
        self,
        mock_exit_in_progress,
        mock_jules_close,
        mock_empty_close,
        mock_checks,
        mock_git_push,
        mock_git_commit,
        mock_apply_local_fix,
        mock_run_local_tests,
        mock_cmd,
        mock_detailed_checks,
        mock_log_summary,
        mock_branch_manager,
    ):
        head_sha = "2" * 40
        pr_data = _pr_data(109, head_sha, ref="issue-49")
        client = MagicMock(spec=GitHubClient)

        mock_checks.return_value = GitHubActionsStatusResult(success=False, ids=[9])
        mock_detailed = MagicMock()
        mock_detailed.failed_checks = [{"id": 9, "name": "test", "conclusion": "failure"}]
        mock_detailed_checks.return_value = mock_detailed

        mock_cmd.run_command.side_effect = lambda cmd_list, **kwargs: (MagicMock(success=True, stdout="issue-49\n") if "branch" in cmd_list else MagicMock(success=True, stdout=""))

        fail_result = {
            "success": False,
            "output": "FAILED",
            "errors": "Error",
            "returncode": 1,
            "command": "pytest",
            "test_file": "test_y.py",
        }
        pass_result = {
            "success": True,
            "output": "PASSED",
            "errors": "",
            "returncode": 0,
            "command": "pytest",
            "test_file": "test_y.py",
        }

        # Step 1: Process 3 times while disabled
        disabled_config = AutomationConfig(automatic_test_fix=False)
        mock_run_local_tests.return_value = fail_result

        for _ in range(3):
            result = process_pull_request(client, disabled_config, "owner/repo", pr_data)
            assert any("Automatic test fix is disabled" in a for a in result.actions_taken)

        mock_apply_local_fix.assert_not_called()

        # Step 2: Re-enable feature
        enabled_config = AutomationConfig(automatic_test_fix=True)
        enabled_config.MAX_FIX_ATTEMPTS = 5

        mock_run_local_tests.side_effect = [fail_result, pass_result]
        mock_apply_local_fix.return_value = (["Applied fix"], "Fixed code")

        result = process_pull_request(client, enabled_config, "owner/repo", pr_data)

        # Repair LLM was invoked now that feature is re-enabled
        mock_apply_local_fix.assert_called_once()
        assert any("Local tests passed on attempt 2" in a for a in result.actions_taken)


# ---------------------------------------------------------------------------
# Direct helper and fix_to_pass_tests runner tests
# ---------------------------------------------------------------------------


class TestFixToPassTestsRunnerKillSwitch:
    """Test fix_to_pass_tests runner honor of automatic_test_fix switch."""

    @patch("auto_coder.fix_to_pass_tests_runner.run_local_tests")
    @patch("auto_coder.fix_to_pass_tests_runner.apply_workspace_test_fix")
    @patch("auto_coder.fix_to_pass_tests_runner.apply_test_stability_fix")
    def test_runner_skips_repair_when_disabled(
        self,
        mock_stability_fix,
        mock_workspace_fix,
        mock_run_tests,
    ):
        mock_run_tests.return_value = {
            "success": False,
            "output": "FAILED",
            "errors": "Error",
            "returncode": 1,
            "command": "pytest",
            "test_file": "test_z.py",
        }

        config = AutomationConfig(automatic_test_fix=False)
        mock_manager = MagicMock()

        summary = fix_to_pass_tests(config, mock_manager)

        # Tests ran once
        mock_run_tests.assert_called_once()
        # No fix applied
        mock_workspace_fix.assert_not_called()
        mock_stability_fix.assert_not_called()
        # Summary reflects disabled repair and 0 attempts consumed
        assert summary["success"] is False
        assert summary["attempts"] == 0
        assert any("Automatic test fix is disabled" in m for m in summary["messages"])


class TestDirectHelperBypass:
    """Test that direct helper functions honor automatic_test_fix=False."""

    @patch("auto_coder.pr_processor.run_llm_prompt")
    def test_apply_github_actions_fix_bypassed_when_disabled(self, mock_run_prompt):
        config = AutomationConfig(automatic_test_fix=False)
        pr_data = {"number": 200, "title": "PR"}
        from auto_coder.pr_processor import _apply_github_actions_fix

        actions = _apply_github_actions_fix("owner/repo", pr_data, config, "errors")
        mock_run_prompt.assert_not_called()
        assert any("Automatic test fix is disabled" in a for a in actions)

    @patch("auto_coder.pr_processor.run_llm_prompt")
    def test_apply_local_test_fix_bypassed_when_disabled(self, mock_run_prompt):
        config = AutomationConfig(automatic_test_fix=False)
        pr_data = {"number": 201, "title": "PR"}
        from auto_coder.pr_processor import _apply_local_test_fix

        actions, response = _apply_local_test_fix("owner/repo", pr_data, config, {"errors": "err"}, [])
        mock_run_prompt.assert_not_called()
        assert response == ""
        assert any("Automatic test fix is disabled" in a for a in actions)
