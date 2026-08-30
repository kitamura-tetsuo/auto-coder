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
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_green_ci_with_adversarial_pass_merges(
        self,
        mock_merge_pr,
        mock_worktree,
        mock_run_validation,
        mock_threads,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        """When CI is green and adversarial validation passes, PR should be merged."""
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        mock_worktree.return_value.__enter__.return_value = "/tmp/worktree"
        mock_run_validation.return_value = AdversarialValidationResult(
            result="PASS",
            summary="All specifications verified",
            findings=[],
        )

        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True
        pr_data = {"number": 100, "labels": [], "head": {"ref": "feature-branch", "sha": "abc123456789"}}

        client = MagicMock()
        client.get_pull_request.return_value = {"head": {"sha": "abc123456789"}}
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_worktree.assert_called_once_with("owner/repo", 100, "abc123456789")
        mock_run_validation.assert_called_once()
        mock_merge_pr.assert_called_once()
        assert any("Adversarial validation passed" in a for a in actions)
        assert any("Successfully merged PR #100" in a for a in actions)

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
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
        mock_worktree,
        mock_run_validation,
        mock_threads,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        """When CI is green but adversarial validation finds a violation, merge is blocked and fix is triggered."""
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        mock_worktree.return_value.__enter__.return_value = "/tmp/worktree"
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
        pr_data = {"number": 100, "labels": [], "head": {"ref": "feature-branch", "sha": "abc123456789"}}

        client = MagicMock()
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_worktree.assert_called_once_with("owner/repo", 100, "abc123456789")
        mock_run_validation.assert_called_once()
        mock_branch_mgr.assert_called_once_with("feature-branch")
        mock_merge_pr.assert_not_called()
        mock_apply_fix.assert_called_once()
        assert any("Adversarial validation failed for PR #100" in a for a in actions)
        assert any("Committed regression test and fix" in a for a in actions)

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor._merge_pr")
    def test_green_ci_with_adversarial_blocked_fails_closed_without_merging(
        self,
        mock_merge_pr,
        mock_worktree,
        mock_run_validation,
        mock_threads,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        """When validation produces a BLOCKED or INCONCLUSIVE status, merge must be blocked (fail-closed)."""
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        mock_worktree.return_value.__enter__.return_value = "/tmp/worktree"
        mock_run_validation.return_value = AdversarialValidationResult(
            result="BLOCKED",
            summary="Oracle acquisition failed",
            findings=[],
        )

        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True
        pr_data = {"number": 100, "labels": [], "head": {"ref": "feature-branch", "sha": "abc123456789"}}

        client = MagicMock()
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_worktree.assert_called_once_with("owner/repo", 100, "abc123456789")
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
        pr_data = {"number": 100, "labels": [], "head": {"ref": "feature-branch", "sha": "abc123456789"}}

        client = MagicMock()
        client.get_pull_request.return_value = {"head": {"sha": "abc123456789"}}
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_run_validation.assert_not_called()
        mock_merge_pr.assert_called_once()
        assert any("Successfully merged PR #100" in a for a in actions)

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor.apply_adversarial_fix")
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_inconclusive_with_findings_blocks_merge_without_triggering_fix(
        self,
        mock_merge_pr,
        mock_apply_fix,
        mock_worktree,
        mock_run_validation,
        mock_threads,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        """INCONCLUSIVE with findings must block merge but MUST NOT trigger apply_adversarial_fix."""
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        mock_worktree.return_value.__enter__.return_value = "/tmp/worktree"
        mock_run_validation.return_value = AdversarialValidationResult(
            result="INCONCLUSIVE",
            summary="Uncertain about behavior",
            findings=[
                AdversarialValidationFinding(
                    violated_requirement="Suspected invariant",
                    counterexample="Given state S, might fail",
                )
            ],
        )

        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True
        pr_data = {"number": 100, "labels": [], "head": {"ref": "feature-branch", "sha": "abc123456789"}}

        client = MagicMock()
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_worktree.assert_called_once_with("owner/repo", 100, "abc123456789")
        mock_run_validation.assert_called_once()
        mock_apply_fix.assert_not_called()
        mock_merge_pr.assert_not_called()
        assert any("Adversarial validation blocked PR #100" in a for a in actions)

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor._merge_pr")
    def test_remote_head_sha_change_aborts_merge(
        self,
        mock_merge_pr,
        mock_worktree,
        mock_run_validation,
        mock_threads,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        """When remote head SHA changes between CI/validation and merge, merge must be aborted."""
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        mock_worktree.return_value.__enter__.return_value = "/tmp/worktree"
        mock_run_validation.return_value = AdversarialValidationResult(
            result="PASS",
            summary="All specifications verified",
            findings=[],
        )

        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True
        pr_data = {"number": 100, "labels": [], "head": {"ref": "feature-branch", "sha": "abc123456789"}}

        # Remote PR now has a newer commit "def987654321" pushed
        client = MagicMock()
        client.get_pull_request.return_value = {"head": {"sha": "def987654321"}}

        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_worktree.assert_called_once_with("owner/repo", 100, "abc123456789")
        mock_run_validation.assert_called_once()
        mock_merge_pr.assert_not_called()
        assert any("head SHA changed" in a for a in actions)

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor._merge_pr")
    def test_remote_head_sha_verification_failure_fails_closed(
        self,
        mock_merge_pr,
        mock_worktree,
        mock_run_validation,
        mock_threads,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        """When get_pull_request fails during post-validation remote head check, merge must fail closed (aborted)."""
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        mock_worktree.return_value.__enter__.return_value = "/tmp/worktree"
        mock_run_validation.return_value = AdversarialValidationResult(
            result="PASS",
            summary="All specifications verified",
            findings=[],
        )

        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True
        pr_data = {"number": 100, "labels": [], "head": {"ref": "feature-branch", "sha": "abc123456789"}}

        # Remote verification API throws an exception
        client = MagicMock()
        client.get_pull_request.side_effect = RuntimeError("GitHub API rate limited")

        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_worktree.assert_called_once_with("owner/repo", 100, "abc123456789")
        mock_run_validation.assert_called_once()
        mock_merge_pr.assert_not_called()
        assert any("Failed to verify remote head SHA" in a for a in actions)

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor._merge_pr")
    def test_missing_head_sha_blocks_adversarial_validation_fail_closed(
        self,
        mock_merge_pr,
        mock_worktree,
        mock_run_validation,
        mock_threads,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        """When pr_data is missing head.sha, validation and merge must fail closed without running against ambient workspace."""
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])

        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True
        # Missing sha in head dictionary
        pr_data = {"number": 100, "labels": [], "head": {"ref": "feature-branch"}}

        client = MagicMock()
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_worktree.assert_not_called()
        mock_run_validation.assert_not_called()
        mock_merge_pr.assert_not_called()
        assert any("Missing head.sha in PR data" in a for a in actions)

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_handle_pr_merge_passes_expected_head_sha_to_merge_pr(
        self,
        mock_merge_pr,
        mock_worktree,
        mock_run_validation,
        mock_threads,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        """_handle_pr_merge must forward expected_head_sha to _merge_pr for atomic precondition enforcement."""
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        mock_worktree.return_value.__enter__.return_value = "/tmp/worktree"
        mock_run_validation.return_value = AdversarialValidationResult(
            result="PASS",
            summary="All specifications verified",
            findings=[],
        )

        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True
        pr_data = {"number": 100, "labels": [], "head": {"ref": "feature-branch", "sha": "abc123456789"}}

        client = MagicMock()
        client.get_pull_request.return_value = {"head": {"sha": "abc123456789"}}

        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_merge_pr.assert_called_once_with(
            "owner/repo",
            100,
            {},
            config,
            github_client=client,
            expected_head_sha="abc123456789",
        )
        assert any("Successfully merged PR #100" in a for a in actions)


class TestAtomicMergeSHAPrecondition:
    """Test atomic SHA precondition enforcement in _merge_pr."""

    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.util.gh_cache.get_ghapi_client")
    @patch("auto_coder.pr_processor._close_linked_issues")
    @patch("auto_coder.pr_processor._archive_jules_session")
    def test_merge_pr_supplies_sha_precondition_to_github_api(
        self,
        mock_archive,
        mock_close_issues,
        mock_get_ghapi,
        mock_threads,
    ):
        """When expected_head_sha is provided, pulls.merge must receive sha=<expected_head_sha>."""
        from auto_coder.pr_processor import _merge_pr

        mock_api = MagicMock()
        mock_api.pulls.get.return_value = {"number": 100, "user": {"login": "developer"}}
        mock_api.pulls.merge.return_value = {"merged": True}
        mock_get_ghapi.return_value = mock_api

        client = MagicMock()
        client.token = "fake-token"

        config = AutomationConfig()
        config.MERGE_METHOD = "--squash"

        result = _merge_pr(
            repo_name="owner/repo",
            pr_number=100,
            analysis={},
            config=config,
            github_client=client,
            expected_head_sha="abc123456789",
        )

        assert result is True
        mock_api.pulls.merge.assert_called_once_with(
            "owner",
            "repo",
            100,
            merge_method="squash",
            sha="abc123456789",
        )
        mock_close_issues.assert_called_once_with("owner/repo", 100)

    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.util.gh_cache.get_ghapi_client")
    @patch("auto_coder.pr_processor._get_allowed_merge_methods", return_value=["--squash", "--merge"])
    @patch("auto_coder.pr_processor._close_linked_issues")
    @patch("auto_coder.pr_processor._archive_jules_session")
    def test_merge_pr_fails_when_github_api_rejects_sha_precondition(
        self,
        mock_archive,
        mock_close_issues,
        mock_get_allowed,
        mock_get_ghapi,
        mock_threads,
    ):
        """When GitHub API rejects merge due to SHA mismatch (409 Conflict), merge returns False."""
        from auto_coder.pr_processor import _merge_pr

        mock_api = MagicMock()
        mock_api.pulls.get.return_value = {"number": 100, "user": {"login": "developer"}, "mergeable": True}
        # GitHub returns HTTP 409 Conflict when head branch was modified
        mock_api.pulls.merge.side_effect = RuntimeError("409 Conflict: Head branch was modified. Review and try the merge again.")
        mock_get_ghapi.return_value = mock_api

        client = MagicMock()
        client.token = "fake-token"

        config = AutomationConfig()
        config.MERGE_METHOD = "--squash"

        result = _merge_pr(
            repo_name="owner/repo",
            pr_number=100,
            analysis={},
            config=config,
            github_client=client,
            expected_head_sha="abc123456789",
        )

        assert result is False
        mock_close_issues.assert_not_called()
        mock_archive.assert_not_called()
