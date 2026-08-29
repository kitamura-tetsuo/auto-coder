"""Integration tests for adversarial validation in the PR processor flow."""

from unittest.mock import MagicMock, Mock, patch

import pytest

from auto_coder.adversarial_validator import AdversarialValidationFinding, AdversarialValidationResult
from auto_coder.automation_config import AutomationConfig
from auto_coder.pr_processor import _handle_pr_merge
from auto_coder.util.github_action import GitHubActionsStatusResult


class TestAdversarialValidationPRFlow:
    """Test adversarial validation integration into _handle_pr_merge."""

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_green_ci_with_adversarial_pass_merges(
        self,
        mock_merge_pr,
        mock_run_validation,
        mock_threads,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        """When CI is green and adversarial validation passes, PR should be merged."""
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        mock_run_validation.return_value = AdversarialValidationResult(
            result="PASS",
            summary="All specifications verified",
            findings=[],
        )

        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True
        pr_data = {"number": 100, "labels": [], "head": {"ref": "feature-branch"}}

        client = MagicMock()
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_run_validation.assert_called_once()
        mock_merge_pr.assert_called_once()
        assert any("Adversarial validation passed" in a for a in actions)
        assert any("Successfully merged PR #100" in a for a in actions)

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor._checkout_pr_branch", return_value=True)
    @patch("auto_coder.pr_processor.BranchManager")
    @patch("auto_coder.pr_processor.apply_adversarial_fix")
    @patch("auto_coder.pr_processor._merge_pr")
    def test_green_ci_with_adversarial_needs_fix_blocks_merge_and_fixes(
        self,
        mock_merge_pr,
        mock_apply_fix,
        mock_branch_mgr,
        mock_checkout,
        mock_run_validation,
        mock_threads,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        """When CI is green but adversarial validation finds a violation, merge is blocked and fix is triggered."""
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        mock_run_validation.return_value = AdversarialValidationResult(
            result="NEEDS_FIX",
            summary="Found specification violation",
            findings=[
                AdversarialValidationFinding(
                    violated_requirement="Spec requires idempotency",
                    counterexample="Given state S, when action A occurs, then R, but produces duplicate X, tests pass because only 1 call is made in test",
                    test_gap="Tests only test single execution",
                    suggested_regression_scenario="Call action twice and verify state",
                )
            ],
        )
        mock_apply_fix.return_value = ["Committed regression test and fix for adversarial violation", "Pushed adversarial fixes to GitHub"]

        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True
        pr_data = {"number": 100, "labels": [], "head": {"ref": "feature-branch"}}

        client = MagicMock()
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_run_validation.assert_called_once()
        mock_merge_pr.assert_not_called()
        mock_apply_fix.assert_called_once()
        assert any("Adversarial validation failed for PR #100" in a for a in actions)
        assert any("Committed regression test and fix" in a for a in actions)

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor._merge_pr")
    def test_green_ci_with_adversarial_blocked_fails_closed_without_merging(
        self,
        mock_merge_pr,
        mock_run_validation,
        mock_threads,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        """When validation produces a BLOCKED or INCONCLUSIVE status, merge must be blocked (fail-closed)."""
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        mock_run_validation.return_value = AdversarialValidationResult(
            result="BLOCKED",
            summary="Oracle acquisition failed",
            findings=[],
        )

        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True
        pr_data = {"number": 100, "labels": [], "head": {"ref": "feature-branch"}}

        client = MagicMock()
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_run_validation.assert_called_once()
        mock_merge_pr.assert_not_called()
        assert any("Adversarial validation blocked PR #100" in a for a in actions)

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_disabled_adversarial_validation_merges_directly(
        self,
        mock_merge_pr,
        mock_run_validation,
        mock_threads,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        """When adversarial validation is disabled via config, merge proceeds without validation."""
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])

        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = False
        pr_data = {"number": 100, "labels": [], "head": {"ref": "feature-branch"}}

        client = MagicMock()
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_run_validation.assert_not_called()
        mock_merge_pr.assert_called_once()
        assert any("Successfully merged PR #100" in a for a in actions)
