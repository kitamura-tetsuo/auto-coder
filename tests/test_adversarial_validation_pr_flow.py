"""Integration tests for adversarial validation in the PR processor flow."""

from unittest.mock import MagicMock, Mock, patch

import pytest

from auto_coder.adversarial_validator import (
    ADVERSARIAL_VALIDATION_COMMENT_LIMIT,
    AdversarialValidationFinding,
    AdversarialValidationResult,
    format_adversarial_validation_comment,
)
from auto_coder.automation_config import AutomationConfig
from auto_coder.pr_processor import (
    _get_codex_review_state,
    _get_published_adversarial_validation_status,
    _handle_pr_merge,
    _publish_adversarial_validation_result,
)
from auto_coder.util.github_action import GitHubActionsStatusResult


def codex_review_summary(status: str, reviewed_sha: str = "original1") -> dict[str, object]:
    return {
        "body": ("<!-- codex-pull-request-review-summary -->\n\n" "| Review | Status | Commit | Review trigger |\n" "| --- | --- | --- | --- |\n" f"| 📝 **Code Review** | {status} | `{reviewed_sha}` | PR opened |"),
        "user": {"login": "chatgpt-codex-connector[bot]"},
    }


class TestCodexReviewState:
    def test_completed_summary_is_authoritative_without_reaction(self):
        client = MagicMock()
        client.get_pr_comments.return_value = [codex_review_summary("✅ **Completed**")]

        state = _get_codex_review_state(client, "owner/repo", 100)

        assert state.present is True
        assert state.completed is True
        assert state.lookup_error is None

    def test_summary_without_completed_status_is_still_running(self):
        client = MagicMock()
        client.get_pr_comments.return_value = [codex_review_summary("👀 **In progress**")]

        state = _get_codex_review_state(client, "owner/repo", 100)

        assert state.present is True
        assert state.completed is False
        assert state.lookup_error is None

    def test_marker_from_non_codex_author_is_ignored(self):
        client = MagicMock()
        comment = codex_review_summary("✅ **Completed**")
        comment["user"] = {"login": "someone-else"}
        client.get_pr_comments.return_value = [comment]

        state = _get_codex_review_state(client, "owner/repo", 100)

        assert state.present is False
        assert state.completed is False


class TestAdversarialValidationPRComment:
    def test_formats_all_structured_fields(self):
        result = AdversarialValidationResult(
            result="NEEDS_FIX",
            summary="Summary",
            findings=[
                AdversarialValidationFinding(
                    violated_requirement="Required behavior",
                    counterexample="Concrete counterexample",
                    test_gap="Missing assertion",
                    suggested_regression_scenario="Add edge-case test",
                )
            ],
            dynamic_check_requested="tests/test_edge.py",
            diagnostic_category="schema",
            diagnostic_reason="Invalid field",
        )

        comment = format_adversarial_validation_comment(result, "abc123")

        assert comment.startswith("<!-- auto-coder-adversarial-validation:abc123 -->")
        assert "adversarial validation: NEEDS_FIX" in comment
        assert "Summary" in comment
        assert "Required behavior" in comment
        assert "Concrete counterexample" in comment
        assert "Missing assertion" in comment
        assert "Add edge-case test" in comment
        assert "tests/test_edge.py" in comment
        assert "schema" in comment
        assert "Invalid field" in comment

    def test_bounds_oversized_comment(self):
        result = AdversarialValidationResult(
            result="BLOCKED",
            summary="A" * (ADVERSARIAL_VALIDATION_COMMENT_LIMIT + 100),
        )

        comment = format_adversarial_validation_comment(result, "abc123")

        assert len(comment) <= ADVERSARIAL_VALIDATION_COMMENT_LIMIT
        assert "Comment truncated by Auto-Coder" in comment

    def test_publish_deduplicates_the_same_head_sha(self):
        client = MagicMock()
        result = AdversarialValidationResult(result="PASS", summary="Verified")
        rendered_comment = format_adversarial_validation_comment(result, "abc123")
        client.get_pr_comments.return_value = [{"id": 1234, "body": rendered_comment}]

        action = _publish_adversarial_validation_result(
            client,
            "owner/repo",
            100,
            "abc123",
            result,
        )

        client.add_comment_to_pr.assert_not_called()
        client.update_comment_for_issue.assert_not_called()
        assert action == "Adversarial validation result for PR #100 at abc123 was already published"

    def test_publish_keeps_first_result_for_the_same_head_sha_immutable(self):
        client = MagicMock()
        client.get_pr_comments.return_value = [
            {
                "id": 1234,
                "body": format_adversarial_validation_comment(
                    AdversarialValidationResult(result="BLOCKED", summary="Temporary failure"),
                    "abc123",
                ),
            }
        ]
        result = AdversarialValidationResult(result="PASS", summary="Verified")

        action = _publish_adversarial_validation_result(
            client,
            "owner/repo",
            100,
            "abc123",
            result,
        )

        client.add_comment_to_pr.assert_not_called()
        client.update_comment_for_issue.assert_not_called()
        assert action == "Adversarial validation result for PR #100 at abc123 was already published"

    @pytest.mark.parametrize("status", ["PASS", "NEEDS_FIX", "BLOCKED", "INCONCLUSIVE", "ERROR"])
    def test_reads_published_status_for_exact_head_sha(self, status):
        client = MagicMock()
        client.get_pr_comments.return_value = [
            {
                "body": format_adversarial_validation_comment(
                    AdversarialValidationResult(result=status, summary="Saved result"),
                    "abc123",
                )
            }
        ]

        saved_status, error = _get_published_adversarial_validation_status(
            client,
            "owner/repo",
            100,
            "abc123",
        )

        assert saved_status == status
        assert error is None

    def test_prior_status_lookup_failure_is_fail_closed(self):
        client = MagicMock()
        client.get_pr_comments.side_effect = RuntimeError("API unavailable")

        saved_status, error = _get_published_adversarial_validation_status(
            client,
            "owner/repo",
            100,
            "abc123",
        )

        assert saved_status is None
        assert error == "API unavailable"

    def test_publish_failure_is_reported_to_caller(self):
        client = MagicMock()
        client.get_pr_comments.return_value = []
        client.add_comment_to_pr.side_effect = RuntimeError("API unavailable")

        action = _publish_adversarial_validation_result(
            client,
            "owner/repo",
            100,
            "abc123",
            AdversarialValidationResult(result="BLOCKED", summary="Could not validate"),
        )

        assert action == "Failed to publish adversarial validation result to PR #100: API unavailable"


class TestAdversarialValidationPRFlow:
    """Test adversarial validation integration into _handle_pr_merge."""

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_same_sha_with_published_pass_skips_llm_and_merges(
        self,
        mock_merge_pr,
        mock_worktree,
        mock_run_validation,
        mock_threads,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        head_sha = "abc123456789"
        client = MagicMock()
        client.get_pr_comments.return_value = [
            {
                "body": format_adversarial_validation_comment(
                    AdversarialValidationResult(result="PASS", summary="Previously verified"),
                    head_sha,
                )
            }
        ]
        client.get_pull_request.return_value = {"head": {"sha": head_sha}}
        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True
        pr_data = {"number": 100, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-branch", "sha": head_sha}}

        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_run_validation.assert_not_called()
        mock_worktree.assert_not_called()
        mock_merge_pr.assert_called_once()
        assert any("already validated as PASS" in action for action in actions)

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor._merge_pr")
    def test_same_sha_with_published_needs_fix_skips_llm_and_stays_blocked(
        self,
        mock_merge_pr,
        mock_worktree,
        mock_run_validation,
        mock_threads,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        head_sha = "abc123456789"
        client = MagicMock()
        client.get_pr_comments.return_value = [
            {
                "body": format_adversarial_validation_comment(
                    AdversarialValidationResult(
                        result="NEEDS_FIX",
                        summary="Previously rejected",
                        findings=[AdversarialValidationFinding(violated_requirement="Requirement")],
                    ),
                    head_sha,
                )
            }
        ]
        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True
        pr_data = {"number": 100, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-branch", "sha": head_sha}}

        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_run_validation.assert_not_called()
        mock_worktree.assert_not_called()
        mock_merge_pr.assert_not_called()
        assert any("already validated as NEEDS_FIX" in action for action in actions)
        assert any("remains non-pass" in action for action in actions)

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
        pr_data = {"number": 100, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-branch", "sha": "abc123456789"}}

        client = MagicMock()
        client.get_pull_request.return_value = {"head": {"sha": "abc123456789"}}
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_worktree.assert_called_once_with("owner/repo", 100, "abc123456789")
        mock_run_validation.assert_called_once()
        mock_merge_pr.assert_called_once()
        client.add_comment_to_pr.assert_called_once()
        comment = client.add_comment_to_pr.call_args.args[2]
        assert "adversarial validation: PASS" in comment
        assert "All specifications verified" in comment
        assert "abc123456789" in comment
        assert any("Adversarial validation passed" in a for a in actions)
        assert any("Published adversarial validation result" in a for a in actions)
        assert any("Successfully merged PR #100" in a for a in actions)

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor._checkout_pr_branch")
    @patch("auto_coder.pr_processor._merge_pr")
    def test_green_ci_with_adversarial_needs_fix_comments_and_stops(
        self,
        mock_merge_pr,
        mock_checkout,
        mock_worktree,
        mock_run_validation,
        mock_threads,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        """A violation is reported without checking out or modifying the PR branch."""
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
        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True
        pr_data = {"number": 100, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-branch", "sha": "abc123456789"}}

        client = MagicMock()
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_worktree.assert_called_once_with("owner/repo", 100, "abc123456789")
        mock_run_validation.assert_called_once()
        mock_checkout.assert_not_called()
        mock_merge_pr.assert_not_called()
        client.add_comment_to_pr.assert_called_once()
        comment = client.add_comment_to_pr.call_args.args[2]
        assert "adversarial validation: NEEDS_FIX" in comment
        assert "Spec requires idempotency" in comment
        assert "Tests only test single execution" in comment
        assert "Call action twice and verify state" in comment
        assert any("Adversarial validation failed for PR #100" in a for a in actions)
        assert any("no automatic adversarial fix was attempted" in a for a in actions)

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
        pr_data = {"number": 100, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-branch", "sha": "abc123456789"}}

        client = MagicMock()
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_worktree.assert_called_once_with("owner/repo", 100, "abc123456789")
        mock_run_validation.assert_called_once()
        mock_merge_pr.assert_not_called()
        client.add_comment_to_pr.assert_called_once()
        assert "adversarial validation: BLOCKED" in client.add_comment_to_pr.call_args.args[2]
        assert "Oracle acquisition failed" in client.add_comment_to_pr.call_args.args[2]
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
        pr_data = {"number": 100, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-branch", "sha": "abc123456789"}}

        client = MagicMock()
        client.get_pull_request.return_value = {"head": {"sha": "abc123456789"}}
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_run_validation.assert_not_called()
        mock_merge_pr.assert_called_once()
        client.add_comment_to_pr.assert_not_called()
        assert any("Successfully merged PR #100" in a for a in actions)

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_inconclusive_with_findings_blocks_merge(
        self,
        mock_merge_pr,
        mock_worktree,
        mock_run_validation,
        mock_threads,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        """INCONCLUSIVE with findings must block merge."""
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
        pr_data = {"number": 100, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-branch", "sha": "abc123456789"}}

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
        pr_data = {"number": 100, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-branch", "sha": "abc123456789"}}

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
        pr_data = {"number": 100, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-branch", "sha": "abc123456789"}}

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
        pr_data = {"number": 100, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-branch"}}

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
        pr_data = {"number": 100, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-branch", "sha": "abc123456789"}}

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

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_issue_less_pr_skips_validation_and_continues_merge(
        self,
        mock_merge_pr,
        mock_worktree,
        mock_run_validation,
        mock_threads,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        head_sha = "maintenance123"
        pr_data = {"number": 100, "body": "Periodic documentation refresh", "labels": [], "head": {"ref": "docs-refresh", "sha": head_sha}}
        client = MagicMock()
        client.get_pull_request.return_value = {"head": {"sha": head_sha}}
        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True

        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_run_validation.assert_not_called()
        mock_worktree.assert_not_called()
        client.get_pr_comments.assert_not_called()
        client.add_comment_to_pr.assert_not_called()
        mock_merge_pr.assert_called_once()
        assert any("no linked Issue specification oracle" in action for action in actions)
        assert not any("BLOCKED" in action for action in actions)

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor._merge_pr")
    def test_in_progress_codex_review_waits_without_validation_or_merge(
        self,
        mock_merge_pr,
        mock_worktree,
        mock_run_validation,
        mock_threads,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        pr_data = {"number": 100, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-branch", "sha": "current2"}}
        client = MagicMock()
        client.get_pr_comments.return_value = [codex_review_summary("👀 **In progress**")]
        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True

        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_run_validation.assert_not_called()
        mock_worktree.assert_not_called()
        mock_merge_pr.assert_not_called()
        assert any("Waiting for Codex GitHub review to complete" in action for action in actions)

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_completed_codex_review_of_old_sha_validates_current_head(
        self,
        mock_merge_pr,
        mock_worktree,
        mock_run_validation,
        mock_threads,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        mock_run_validation.return_value = AdversarialValidationResult(result="PASS", summary="Current head satisfies the Issue")
        current_sha = "current-head-h2"
        pr_data = {"number": 100, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-branch", "sha": current_sha}}
        client = MagicMock()
        client.get_pr_comments.return_value = [codex_review_summary("✅ **Completed**", reviewed_sha="old-head-h1")]
        client.get_pull_request.return_value = {"head": {"sha": current_sha}}
        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True

        _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_worktree.assert_called_once_with("owner/repo", 100, current_sha)
        mock_run_validation.assert_called_once_with("owner/repo", pr_data, config, github_client=client)
        mock_merge_pr.assert_called_once()


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
