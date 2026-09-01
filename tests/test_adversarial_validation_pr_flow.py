"""Integration tests for adversarial validation in the PR processor flow."""

from unittest.mock import MagicMock, Mock, patch

import pytest

from auto_coder.adversarial_validator import (
    ADVERSARIAL_VALIDATION_COMMENT_LIMIT,
    AdversarialValidationFinding,
    AdversarialValidationResult,
    RequirementCoverageEntry,
    adversarial_validation_codex_feedback_marker,
    format_adversarial_validation_comment,
)
from auto_coder.automation_config import AutomationConfig
from auto_coder.github_app_reviewer import ReviewPublicationResult
from auto_coder.pr_processor import (
    _find_authoritative_adversarial_review,
    _get_adversarial_validation_eligibility,
    _get_codex_review_state,
    _get_published_adversarial_validation_status,
    _handle_pr_merge,
    _send_adversarial_validation_feedback_to_codex_cloud,
)
from auto_coder.util.gh_cache import GitHubClient, ReviewThread, ReviewThreadComment
from auto_coder.util.github_action import GitHubActionsStatusResult


def codex_review_summary(status: str, reviewed_sha: str = "original1") -> dict[str, object]:
    return {
        "body": ("<!-- codex-pull-request-review-summary -->\n\n" "| Review | Status | Commit | Review trigger |\n" "| --- | --- | --- | --- |\n" f"| 📝 **Code Review** | {status} | `{reviewed_sha}` | PR opened |"),
        "user": {"login": "chatgpt-codex-connector[bot]"},
    }


class TestCodexReviewState:
    def test_completed_summary_is_authoritative_without_reaction(self):
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.get_pr_comments.return_value = [codex_review_summary("✅ **Completed**")]

        state = _get_codex_review_state(client, "owner/repo", 100)

        assert state.present is True
        assert state.completed is True
        assert state.lookup_error is None

    def test_summary_without_completed_status_is_still_running(self):
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.get_pr_comments.return_value = [codex_review_summary("👀 **In progress**")]

        state = _get_codex_review_state(client, "owner/repo", 100)

        assert state.present is True
        assert state.completed is False
        assert state.lookup_error is None

    def test_marker_from_non_codex_author_is_ignored(self):
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        comment = codex_review_summary("✅ **Completed**")
        comment["user"] = {"login": "someone-else"}
        client.get_pr_comments.return_value = [comment]

        state = _get_codex_review_state(client, "owner/repo", 100)

        assert state.present is False
        assert state.completed is False

    @patch("auto_coder.util.gh_cache.get_ghapi_client")
    def test_real_client_comment_api_failure_is_not_treated_as_codex_absence(self, mock_get_api):
        mock_api = Mock()
        mock_api.issues.list_comments.side_effect = RuntimeError("comment API unavailable")
        mock_api.side_effect = RuntimeError("comment API unavailable")
        mock_get_api.return_value = mock_api
        client = GitHubClient("test-token")

        assert client.get_pr_comments("owner/repo", 100) == []

        state = _get_codex_review_state(client, "owner/repo", 100)

        assert state.present is False
        assert state.completed is False
        assert state.lookup_error == "comment API unavailable"


class TestAdversarialValidationEligibility:
    def test_issue_less_pr_mentioning_another_pr_is_not_eligible(self):
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        pr_data = {"number": 100, "body": "Maintenance update; see PR #42 for background"}

        eligibility = _get_adversarial_validation_eligibility(client, "owner/repo", pr_data)

        assert eligibility.is_applicable is False
        assert eligibility.issue_numbers == ()
        assert eligibility.lookup_error is None
        client.get_issue.assert_not_called()

    @pytest.mark.parametrize(
        "candidate",
        [None, {"number": 42, "pull_request": {"url": "https://api.github.test/pulls/42"}}],
    )
    def test_nonexistent_or_pull_request_candidate_is_not_an_oracle(self, candidate):
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.get_issue.return_value = candidate
        pr_data = {"number": 100, "body": "Fixes #42"}

        eligibility = _get_adversarial_validation_eligibility(client, "owner/repo", pr_data)

        assert eligibility.is_applicable is False
        assert eligibility.issue_numbers == ()
        assert eligibility.lookup_error is not None
        client.get_issue.assert_called_once_with("owner/repo", 42)

    def test_verified_linked_issue_makes_validation_applicable(self):
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.get_issue.return_value = {"number": 42, "title": "Behavioral contract"}
        pr_data = {"number": 100, "body": "Fixes #42"}

        eligibility = _get_adversarial_validation_eligibility(client, "owner/repo", pr_data)

        assert eligibility.is_applicable is True
        assert eligibility.issue_numbers == (42,)
        assert eligibility.lookup_error is None

    def test_title_inferred_issue_uses_same_verified_oracle_resolution(self):
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.get_issue.return_value = {"number": 42, "title": "Behavioral contract", "body": "Required behavior"}
        pr_data = {"number": 100, "title": "Implement issue #42", "body": "Implementation details"}

        eligibility = _get_adversarial_validation_eligibility(client, "owner/repo", pr_data)

        assert eligibility.is_applicable is True
        assert eligibility.issue_numbers == (42,)


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

        assert comment.startswith("<!-- auto-coder-adversarial-validation:v7:abc123 -->")
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
        assert "characters omitted" in comment

    def test_summarizes_large_requirement_coverage_without_evidence_noise(self):
        result = AdversarialValidationResult(
            result="NEEDS_FIX",
            summary="Concrete failure",
            requirement_coverage=[RequirementCoverageEntry(requirement_id=f"REQ-{index:03d}", status="UNVERIFIED", evidence="verbose evidence") for index in range(25)] + [RequirementCoverageEntry(requirement_id="REQ-999", status="VERIFIED", evidence="verified evidence")],
        )

        comment = format_adversarial_validation_comment(result, "abc123")

        assert "Issue requirement coverage (26 total): UNVERIFIED: 25, VERIFIED: 1." in comment
        assert "`REQ-000`" in comment
        assert "`REQ-019`" in comment
        assert "`REQ-020`" not in comment
        assert "(+5 more)" in comment
        assert "verbose evidence" not in comment
        assert "verified evidence" not in comment

    def test_omits_raw_codex_event_stream_from_comment_fields(self):
        raw_stream = "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-1"}',
                '{"type":"turn.started"}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"large payload"}}',
            ]
        )
        result = AdversarialValidationResult(
            result="BLOCKED",
            summary=f"Adversarial validation execution failed: {raw_stream}",
            diagnostic_category="validation_execution_error",
            diagnostic_reason=raw_stream,
        )

        comment = format_adversarial_validation_comment(result, "abc123")

        assert '"type":"thread.started"' not in comment
        assert '"type":"item.completed"' not in comment
        assert comment.count("Codex CLI event details omitted") == 2

    def test_finds_native_review_from_dedicated_reviewer_app_for_exact_sha(self):
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        result = AdversarialValidationResult(result="PASS", summary="Verified")
        rendered_body = format_adversarial_validation_comment(result, "abc123")
        client.get_pr_reviews_strict.return_value = [{"id": 1, "body": rendered_body, "user": {"login": "auto-coder-reviewer[bot]"}}]

        body, error = _find_authoritative_adversarial_review(client, "owner/repo", 100, "abc123")

        assert error is None
        assert body == rendered_body

    def test_latest_native_review_for_same_sha_is_authoritative(self):
        client = MagicMock()
        first = format_adversarial_validation_comment(AdversarialValidationResult(result="INCONCLUSIVE"), "abc123")
        latest = format_adversarial_validation_comment(AdversarialValidationResult(result="PASS"), "abc123")
        client.get_pr_reviews_strict.return_value = [
            {"id": 1, "body": first, "user": {"login": "auto-coder-reviewer[bot]"}},
            {"id": 2, "body": latest, "user": {"login": "auto-coder-reviewer[bot]"}},
        ]

        body, error = _find_authoritative_adversarial_review(client, "owner/repo", 100, "abc123")

        assert error is None
        assert body == latest

    def test_lookalike_review_from_another_author_is_not_authoritative(self):
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        result = AdversarialValidationResult(result="PASS", summary="Verified")
        rendered_body = format_adversarial_validation_comment(result, "abc123")
        client.get_pr_reviews_strict.return_value = [{"id": 1, "body": rendered_body, "user": {"login": "someone-else[bot]"}}]
        client.get_pr_comments.return_value = []

        body, error = _find_authoritative_adversarial_review(client, "owner/repo", 100, "abc123")

        assert error is None
        assert body is None

    def test_native_review_lookup_fails_closed_on_reviews_api_error(self):
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.get_pr_reviews_strict.side_effect = RuntimeError("API unavailable")

        body, error = _find_authoritative_adversarial_review(client, "owner/repo", 100, "abc123")

        assert body is None
        assert error == "API unavailable"

    def test_native_review_lookup_fails_closed_on_identity_resolution_error(self):
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        with patch("auto_coder.pr_processor.resolve_reviewer_app_identity", side_effect=RuntimeError("no reviewer credentials")):
            body, error = _find_authoritative_adversarial_review(client, "owner/repo", 100, "abc123")

        assert body is None
        assert error == "no reviewer credentials"
        client.get_pr_reviews_strict.assert_not_called()

    @pytest.mark.parametrize("status", ["PASS", "NEEDS_FIX", "BLOCKED", "INCONCLUSIVE", "ERROR"])
    def test_reads_published_status_for_exact_head_sha(self, status):
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
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

    def test_legacy_unversioned_result_does_not_suppress_revalidation(self):
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.get_pr_comments.return_value = [
            {
                "body": "\n".join(
                    [
                        "<!-- auto-coder-adversarial-validation:abc123 -->",
                        "## ⚠️ Auto-Coder adversarial validation: ERROR",
                        "",
                        "Invalid Codex validator event stream",
                    ]
                )
            }
        ]

        saved_status, error = _get_published_adversarial_validation_status(
            client,
            "owner/repo",
            100,
            "abc123",
        )

        assert saved_status is None
        assert error is None

    def test_prior_status_lookup_failure_is_fail_closed(self):
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.get_pr_comments.side_effect = RuntimeError("API unavailable")

        saved_status, error = _get_published_adversarial_validation_status(
            client,
            "owner/repo",
            100,
            "abc123",
        )

        assert saved_status is None
        assert error == "API unavailable"

    def test_reads_published_status_from_native_review_over_legacy_comment(self):
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.get_pr_reviews_strict.return_value = [
            {
                "body": format_adversarial_validation_comment(AdversarialValidationResult(result="PASS", summary="Native verdict"), "abc123"),
                "user": {"login": "auto-coder-reviewer[bot]"},
            }
        ]
        # A stale/legacy comment for the same SHA must not be consulted once a native review exists.
        client.get_pr_comments.return_value = [{"body": format_adversarial_validation_comment(AdversarialValidationResult(result="NEEDS_FIX", summary="Stale"), "abc123")}]

        saved_status, error = _get_published_adversarial_validation_status(client, "owner/repo", 100, "abc123")

        assert saved_status == "PASS"
        assert error is None
        client.get_pr_comments.assert_not_called()


class TestAdversarialValidationCodexFeedback:
    def test_sends_complete_report_as_custom_codex_cloud_followup(self):
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.get_pr_comments.return_value = []
        cloud_client = MagicMock()
        cloud_client.continue_if_paused.return_value = True
        pr_data = {
            "number": 100,
            "body": "Fixes #99\n\nhttps://chatgpt.com/codex/tasks/task_e_abc123",
            "head": {"ref": "codex/issue-99", "sha": "stale_sha"},
            "base": {"ref": "main"},
        }
        report = "## Auto-Coder adversarial validation: NEEDS_FIX\n\nConcrete counterexample"

        with patch("auto_coder.codex_cloud_client.CodexCloudClient", return_value=cloud_client):
            actions = _send_adversarial_validation_feedback_to_codex_cloud(
                "owner/repo",
                pr_data,
                "head123",
                report,
                client,
            )

        cloud_client.continue_if_paused.assert_called_once()
        (task_id,) = cloud_client.continue_if_paused.call_args.args
        prompt = cloud_client.continue_if_paused.call_args.kwargs["prompt"]
        assert task_id == "task_e_abc123"
        assert "PR #100" in prompt
        assert "head123" in prompt
        assert report in prompt
        assert "add strict regression tests" in prompt
        assert "codex/issue-99" in prompt
        assert "Do not create a new branch." in prompt
        assert "Do not create a new pull request." in prompt
        assert "Do not replace or close the existing pull request." in prompt
        assert any("Sent adversarial NEEDS_FIX report" in action for action in actions)
        delivery_comment = client.add_comment_to_pr.call_args.args[2]
        assert delivery_comment.startswith(adversarial_validation_codex_feedback_marker("head123"))

    def test_missing_branch_metadata_blocks_feedback_instead_of_weak_prompt(self):
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.get_pr_comments.return_value = []
        pr_data = {
            "number": 100,
            "body": "Fixes #99\n\nhttps://chatgpt.com/codex/tasks/task_e_abc123",
        }

        with patch("auto_coder.codex_cloud_client.CodexCloudClient") as cloud_client_type:
            actions = _send_adversarial_validation_feedback_to_codex_cloud(
                "owner/repo",
                pr_data,
                "head123",
                "report",
                client,
            )

        cloud_client_type.assert_not_called()
        assert any("PR head/base branch metadata is unavailable" in action for action in actions)

    def test_delivery_marker_prevents_duplicate_codex_cloud_followup(self):
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.get_pr_comments.return_value = [{"body": adversarial_validation_codex_feedback_marker("head123") + "\nDelivered"}]
        pr_data = {
            "number": 100,
            "body": "https://chatgpt.com/codex/tasks/task_e_abc123",
        }

        with patch("auto_coder.codex_cloud_client.CodexCloudClient") as cloud_client_type:
            actions = _send_adversarial_validation_feedback_to_codex_cloud(
                "owner/repo",
                pr_data,
                "head123",
                "report",
                client,
            )

        cloud_client_type.assert_not_called()
        client.add_comment_to_pr.assert_not_called()
        assert actions == ["Skipped duplicate adversarial feedback to Codex Cloud for PR #100 at head123"]


class TestAdversarialValidationPRFlow:
    """Test adversarial validation integration into _handle_pr_merge."""

    @pytest.fixture(autouse=True)
    def dedicated_reviewer_publication(self):
        """Keep flow tests focused while requiring successful App publication."""
        with patch(
            "auto_coder.pr_processor.publish_adversarial_review",
            return_value=ReviewPublicationResult(True, "APPROVE", ""),
        ) as publisher:
            yield publisher

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
        client.get_pr_review_threads_strict.return_value = []
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
        client.get_pr_review_threads_strict.return_value = []
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

        with patch(
            "auto_coder.pr_processor._send_adversarial_validation_feedback_to_codex_cloud",
            return_value=["Sent saved report"],
        ) as mock_feedback:
            actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_run_validation.assert_not_called()
        mock_worktree.assert_not_called()
        mock_merge_pr.assert_not_called()
        mock_feedback.assert_called_once()
        assert "Previously rejected" in mock_feedback.call_args.args[3]
        assert any("already validated as NEEDS_FIX" in action for action in actions)
        assert any("remains non-pass" in action for action in actions)
        assert "Sent saved report" in actions

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
        dedicated_reviewer_publication,
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
        client.get_pr_review_threads_strict.return_value = []
        client.get_pull_request.return_value = {"head": {"sha": "abc123456789"}}
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_worktree.assert_called_once_with("owner/repo", 100, "abc123456789")
        mock_run_validation.assert_called_once()
        mock_merge_pr.assert_called_once()
        dedicated_reviewer_publication.assert_called_once_with("owner/repo", 100, "abc123456789", mock_run_validation.return_value)
        client.add_comment_to_pr.assert_not_called()
        assert any("Adversarial validation passed" in a for a in actions)
        assert any("Published APPROVE adversarial review" in a for a in actions)
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
        dedicated_reviewer_publication,
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
        client.get_pr_review_threads_strict.return_value = []
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_worktree.assert_called_once_with("owner/repo", 100, "abc123456789")
        mock_run_validation.assert_called_once()
        mock_checkout.assert_not_called()
        mock_merge_pr.assert_not_called()
        client.add_comment_to_pr.assert_not_called()
        dedicated_reviewer_publication.assert_called_once_with("owner/repo", 100, "abc123456789", mock_run_validation.return_value)
        assert any("Adversarial validation failed for PR #100" in a for a in actions)
        assert any("no local automatic adversarial fix was attempted" in a for a in actions)

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
        dedicated_reviewer_publication,
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
        client.get_pr_review_threads_strict.return_value = []
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_worktree.assert_called_once_with("owner/repo", 100, "abc123456789")
        mock_run_validation.assert_called_once()
        mock_merge_pr.assert_not_called()
        client.add_comment_to_pr.assert_not_called()
        dedicated_reviewer_publication.assert_called_once_with("owner/repo", 100, "abc123456789", mock_run_validation.return_value)
        assert any("Adversarial validation blocked PR #100" in a for a in actions)

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor._merge_pr")
    def test_validation_execution_error_comment_excludes_exception_event_stream(
        self,
        mock_merge_pr,
        mock_worktree,
        mock_run_validation,
        mock_threads,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
        dedicated_reviewer_publication,
    ):
        raw_stream = '{"type":"thread.started"}\n{"type":"item.completed","item":{"text":"noise"}}'
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        mock_worktree.return_value.__enter__.side_effect = RuntimeError(raw_stream)
        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True
        pr_data = {"number": 100, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-branch", "sha": "abc123456789"}}
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []

        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_run_validation.assert_not_called()
        mock_merge_pr.assert_not_called()
        client.add_comment_to_pr.assert_not_called()
        published_result = dedicated_reviewer_publication.call_args.args[3]
        comment = format_adversarial_validation_comment(published_result, "abc123456789")
        assert "adversarial validation: BLOCKED" in comment
        assert "validation_execution_error" in comment
        assert "RuntimeError" in comment
        assert '"type":"thread.started"' not in comment
        assert any("Adversarial validation blocked PR #100" in action for action in actions)

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
        client.get_pr_review_threads_strict.return_value = []
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
        client.get_pr_review_threads_strict.return_value = []
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
        client.get_pr_review_threads_strict.return_value = []
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
        client.get_pr_review_threads_strict.return_value = []
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
        client.get_pr_review_threads_strict.return_value = []
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
        client.get_pr_review_threads_strict.return_value = []
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
        client.get_pr_review_threads_strict.return_value = []
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
    def test_unresolved_explicit_issue_reference_fails_closed(
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
        pr_data = {"number": 100, "body": "Fixes #42", "labels": [], "head": {"ref": "feature-branch", "sha": "current-head"}}
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.get_issue.return_value = None
        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True

        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_run_validation.assert_not_called()
        mock_worktree.assert_not_called()
        mock_merge_pr.assert_not_called()
        assert any("Could not verify adversarial-validation eligibility" in action for action in actions)
        assert not any("no linked Issue specification oracle" in action for action in actions)

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
        client.get_pr_review_threads_strict.return_value = []
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
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", side_effect=[False, True])
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor._merge_pr")
    def test_codex_completion_rechecks_threads_before_validation(
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
        client.get_pr_review_threads_strict.return_value = []
        client.get_issue.return_value = {"number": 99, "title": "Specification"}
        client.get_pr_comments.return_value = [codex_review_summary("✅ **Completed**")]
        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True

        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        assert mock_threads.call_count == 2
        mock_run_validation.assert_not_called()
        mock_worktree.assert_not_called()
        mock_merge_pr.assert_not_called()
        assert any("Codex review completed" in action and "unresolved review threads" in action for action in actions)

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
        client.get_pr_review_threads_strict.return_value = []
        client.get_pr_comments.return_value = [codex_review_summary("✅ **Completed**", reviewed_sha="old-head-h1")]
        client.get_pull_request.return_value = {"head": {"sha": current_sha}}
        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True

        _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_worktree.assert_called_once_with("owner/repo", 100, current_sha)
        mock_run_validation.assert_called_once_with(
            "owner/repo",
            pr_data,
            config,
            github_client=client,
            claimed_review_threads_section="(No claimed-addressed review threads for this run.)",
        )
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
        client.get_pr_review_threads_strict.return_value = []
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
        client.get_pr_review_threads_strict.return_value = []
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


class TestMaxAdversarialValidationsGating:
    """Tests for MAX_ADVERSARIAL_VALIDATIONS limit enforcement."""

    @pytest.fixture(autouse=True)
    def dedicated_reviewer_publication(self):
        """Keep flow tests focused while requiring successful App publication."""
        with patch(
            "auto_coder.pr_processor.publish_adversarial_review",
            return_value=ReviewPublicationResult(True, "APPROVE", ""),
        ) as publisher:
            yield publisher

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_max_adversarial_validations_zero_skips_validation_and_merges_green_pr(
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
        client.get_pr_review_threads_strict.return_value = []
        client.get_pr_comments.return_value = []
        client.get_pull_request.return_value = {"head": {"sha": head_sha}}

        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True
        config.MAX_ADVERSARIAL_VALIDATIONS = 0

        pr_data = {"number": 100, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-branch", "sha": head_sha}}

        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_run_validation.assert_not_called()
        mock_worktree.assert_not_called()
        mock_merge_pr.assert_called_once()
        assert any("reached maximum adversarial review limit (0)" in action for action in actions)
        assert any("Successfully merged PR #100" in action for action in actions)

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_max_adversarial_validations_reached_with_prior_comments_skips_and_merges(
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
        head_sha = "newcommit1234"
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.get_pr_comments.return_value = [
            {"body": "<!-- auto-coder-adversarial-validation:v4:oldsha1 -->\n## ❌ Auto-Coder adversarial validation: NEEDS_FIX"},
            {"body": "<!-- auto-coder-adversarial-validation:v4:oldsha2 -->\n## ❌ Auto-Coder adversarial validation: NEEDS_FIX"},
        ]
        client.get_pull_request.return_value = {"head": {"sha": head_sha}}

        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True
        config.MAX_ADVERSARIAL_VALIDATIONS = 2

        pr_data = {"number": 100, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-branch", "sha": head_sha}}

        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_run_validation.assert_not_called()
        mock_worktree.assert_not_called()
        mock_merge_pr.assert_called_once()
        assert any("reached maximum adversarial review limit (2)" in action for action in actions)
        assert any("Successfully merged PR #100" in action for action in actions)

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_max_adversarial_validations_not_reached_executes_validation(
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
        mock_worktree.return_value.__enter__.return_value = "/tmp/worktree"
        mock_run_validation.return_value = AdversarialValidationResult(
            result="PASS",
            summary="All specifications verified",
            findings=[],
        )

        head_sha = "newcommit1234"
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.get_pr_comments.return_value = [
            {"body": "<!-- auto-coder-adversarial-validation:v4:oldsha1 -->\n## ❌ Auto-Coder adversarial validation: NEEDS_FIX"},
        ]
        client.get_pull_request.return_value = {"head": {"sha": head_sha}}

        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True
        config.MAX_ADVERSARIAL_VALIDATIONS = 2

        pr_data = {"number": 100, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-branch", "sha": head_sha}}

        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_run_validation.assert_called_once()
        mock_worktree.assert_called_once()
        mock_merge_pr.assert_called_once()
        assert any("Adversarial validation passed" in action for action in actions)
        assert any("Successfully merged PR #100" in action for action in actions)

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_max_adversarial_validations_reached_with_needs_fix_on_current_sha_merges_green_pr(
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
        client.get_pr_review_threads_strict.return_value = []
        client.get_pr_comments.return_value = [
            {"body": f"<!-- auto-coder-adversarial-validation:v4:{head_sha} -->\n## ❌ Auto-Coder adversarial validation: NEEDS_FIX"},
        ]
        client.get_pull_request.return_value = {"head": {"sha": head_sha}}

        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True
        config.MAX_ADVERSARIAL_VALIDATIONS = 1

        pr_data = {"number": 100, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-branch", "sha": head_sha}}

        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_run_validation.assert_not_called()
        mock_worktree.assert_not_called()
        mock_merge_pr.assert_called_once()
        assert any("reached maximum adversarial review limit (1)" in action for action in actions)
        assert any("Successfully merged PR #100" in action for action in actions)


class TestClaimedReviewThreadValidationFlow:
    """End-to-end coverage for issue #1619: a claimed-addressed review thread
    does not block merge outright; it is carried into a fresh adversarial
    validation run and resolved only on a valid ADDRESSED disposition."""

    def test_new_provenance_reply_revalidates_and_passes_same_sha(self):
        from auto_coder.adversarial_validator import ReviewThreadDisposition
        from auto_coder.pr_processor import ClaimedReviewThreadGateState
        from auto_coder.review_thread_validation import ClaimedReviewThread

        head_sha = "same-head-123"
        claimed = ClaimedReviewThread(
            thread_id="provenance-1",
            root_comment_database_id=1,
            root_author_login="auto-coder-reviewer[bot]",
            original_finding="Explain uv.lock provenance",
            discussion="agent[bot]: Generated from dependency X.\n<!-- auto-coder-review-addressed:v1 -->",
            is_change_provenance=True,
            claim_evidence="agent[bot]: Generated from dependency X.\n<!-- auto-coder-review-addressed:v1 -->",
        )
        validation = AdversarialValidationResult(
            result="PASS",
            summary="Provenance independently verified",
            thread_dispositions=[
                ReviewThreadDisposition(
                    thread_id="provenance-1",
                    status="ADDRESSED",
                    rationale="The dependency diff causally regenerates the lockfile",
                    evidence="pyproject.toml adds X and uv.lock contains the corresponding resolution only",
                )
            ],
        )
        client = MagicMock()
        client.get_pr_comments.return_value = []
        client.get_issue.return_value = {"number": 99, "title": "Contract", "body": "Required behavior"}
        client.get_pull_request.return_value = {"head": {"sha": head_sha}}
        config = AutomationConfig()
        config.AUTO_MERGE = True
        pr_data = {"number": 123, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-123", "sha": head_sha}}

        with (
            patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
            patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
            patch("auto_coder.pr_processor._check_github_actions_status", return_value=GitHubActionsStatusResult(success=True, ids=[1])),
            patch("auto_coder.pr_processor._get_claimed_review_thread_state", return_value=ClaimedReviewThreadGateState(claimed=(claimed,))),
            patch("auto_coder.pr_processor._get_published_adversarial_validation_status", return_value=("INCONCLUSIVE", None)),
            patch("auto_coder.pr_processor._get_published_adversarial_validation_comment", return_value=("prior review without this reply", None)),
            patch("auto_coder.pr_processor.run_adversarial_validation", return_value=validation) as run_validation,
            patch("auto_coder.pr_processor.publish_adversarial_review", return_value=ReviewPublicationResult(True, "APPROVE", "")) as publish,
            patch("auto_coder.pr_processor.resolve_addressed_review_threads", return_value=["provenance-1"]),
            patch("auto_coder.pr_processor.isolated_pr_head_worktree"),
            patch("auto_coder.pr_processor._merge_pr", return_value=True),
        ):
            actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        run_validation.assert_called_once()
        published_result = publish.call_args.args[3]
        assert published_result.clarification_reply_fingerprint.startswith("<!-- auto-coder-change-provenance-evidence:v1:")
        assert published_result.publish_clarification_thread is False
        assert any("unchanged commit" in action and "new implementer provenance evidence" in action for action in actions)
        assert any("Successfully merged PR #123" in action for action in actions)

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._get_claimed_review_thread_state")
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.publish_adversarial_review", return_value=ReviewPublicationResult(True, "APPROVE", ""))
    @patch("auto_coder.pr_processor.resolve_addressed_review_threads")
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor._merge_pr")
    def test_claimed_thread_allows_validation_and_gets_resolved(
        self,
        mock_merge_pr,
        mock_worktree,
        mock_resolve,
        mock_publish,
        mock_adv_val,
        mock_claimed_state,
        mock_checks,
        mock_mergeable,
        mock_exit_if_in_progress,
    ):
        from auto_coder.adversarial_validator import ReviewThreadDisposition
        from auto_coder.pr_processor import ClaimedReviewThreadGateState
        from auto_coder.review_thread_validation import ClaimedReviewThread

        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        mock_worktree.return_value.__enter__.return_value = "/tmp/worktree"
        claimed = ClaimedReviewThread(thread_id="thread-1", root_comment_database_id=1, root_author_login="chatgpt-codex-connector[bot]", original_finding="finding", discussion="discussion")
        mock_claimed_state.return_value = ClaimedReviewThreadGateState(claimed=(claimed,), has_blocking_unresolved=False)
        mock_merge_pr.return_value = True
        mock_resolve.return_value = ["thread-1"]
        mock_adv_val.return_value = AdversarialValidationResult(
            result="PASS",
            summary="Pass",
            findings=[],
            thread_dispositions=[ReviewThreadDisposition(thread_id="thread-1", status="ADDRESSED", rationale="Verified fix", evidence="Reproduced original path; now passes")],
        )

        config = AutomationConfig()
        config.AUTO_MERGE = True
        pr_data = {"number": 123, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-123", "sha": "123abc456"}}

        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.get_pull_request.return_value = {"head": {"sha": "123abc456"}}
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        assert any("claimed-addressed review thread" in a for a in actions)
        mock_adv_val.assert_called_once()
        _, call_kwargs = mock_adv_val.call_args
        assert "thread-1" in call_kwargs["claimed_review_threads_section"]
        mock_resolve.assert_called_once()
        resolve_args = mock_resolve.call_args[0]
        assert resolve_args[4] == (claimed,)
        assert any("Resolved 1 claimed review thread" in a for a in actions)
        assert any("Successfully merged PR #123" in a for a in actions)
        mock_merge_pr.assert_called_once()

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._get_claimed_review_thread_state")
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.publish_adversarial_review", return_value=ReviewPublicationResult(True, "APPROVE", ""))
    @patch("auto_coder.pr_processor.resolve_addressed_review_threads")
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor._merge_pr")
    def test_stale_resolution_rollback_failure_blocks_merge(
        self,
        mock_merge_pr,
        mock_worktree,
        mock_resolve,
        mock_publish,
        mock_adv_val,
        mock_claimed_state,
        mock_checks,
        mock_mergeable,
        mock_exit_if_in_progress,
    ):
        """[P1] A stale-resolution rollback that could not be confirmed must
        block merge for this run rather than being logged and ignored."""
        from auto_coder.adversarial_validator import ReviewThreadDisposition
        from auto_coder.pr_processor import ClaimedReviewThreadGateState
        from auto_coder.review_thread_validation import ClaimedReviewThread, StaleReviewThreadResolutionError

        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        mock_worktree.return_value.__enter__.return_value = "/tmp/worktree"
        claimed = ClaimedReviewThread(thread_id="thread-1", root_comment_database_id=1, root_author_login="chatgpt-codex-connector[bot]", original_finding="finding", discussion="discussion")
        mock_claimed_state.return_value = ClaimedReviewThreadGateState(claimed=(claimed,), has_blocking_unresolved=False)
        mock_adv_val.return_value = AdversarialValidationResult(
            result="PASS",
            summary="Pass",
            findings=[],
            thread_dispositions=[ReviewThreadDisposition(thread_id="thread-1", status="ADDRESSED", rationale="Verified fix", evidence="Reproduced original path; now passes")],
        )
        mock_resolve.side_effect = StaleReviewThreadResolutionError("thread-1", "owner/repo", 123)

        config = AutomationConfig()
        config.AUTO_MERGE = True
        pr_data = {"number": 123, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-123", "sha": "123abc456"}}

        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.get_pull_request.return_value = {"head": {"sha": "123abc456"}}
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        assert any("stale head" in a and "thread-1" in a for a in actions)
        mock_merge_pr.assert_not_called()

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress")
    @patch("auto_coder.pr_processor._merge_pr")
    def test_persisted_stale_blocker_refuses_merge_on_a_later_run_and_clears_once_reverted(self, mock_merge_pr, mock_exit_if_in_progress, tmp_path, monkeypatch):
        """[P1] A rollback failure persisted by an earlier run must block a
        later, separate `_handle_pr_merge()` invocation immediately —
        bypassing CI/mergeability checks entirely, since GitHub's own
        unresolved-thread state can no longer be trusted for this PR — and
        must stop blocking only once a later retry is confirmed."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        from auto_coder.review_thread_validation import StaleReviewThreadRegistry

        StaleReviewThreadRegistry().record("owner/repo", 123, "thread-1")

        config = AutomationConfig()
        config.AUTO_MERGE = True
        pr_data = {"number": 123, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-123", "sha": "123abc456"}}

        # Run 1: the blocker is still there and the rollback retry still fails.
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.unresolve_review_thread.side_effect = Exception("still failing")
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        assert any("thread(s) thread-1" in a and "stale head" in a for a in actions)
        mock_merge_pr.assert_not_called()
        mock_exit_if_in_progress.assert_not_called()  # blocked before even reaching CI checks
        assert StaleReviewThreadRegistry().pending_for_pr("owner/repo", 123) == ["thread-1"]

        # Run 2: this time the retry succeeds, clearing the blocker; normal
        # processing (CI checks, etc.) resumes.
        mock_exit_if_in_progress.return_value = True
        client2 = MagicMock()  # unresolve_review_thread succeeds
        client2.get_pr_review_threads_strict.return_value = []
        _handle_pr_merge(client2, "owner/repo", pr_data, config, {})

        mock_exit_if_in_progress.assert_called_once()
        assert StaleReviewThreadRegistry().pending_for_pr("owner/repo", 123) == []

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress")
    @patch("auto_coder.pr_processor._merge_pr")
    def test_corrupt_stale_registry_refuses_merge_rather_than_looking_empty(self, mock_merge_pr, mock_exit_if_in_progress, tmp_path, monkeypatch):
        """[P1] A registry file that exists but cannot be parsed must never be
        treated as "no blockers" — it may be hiding a real stale-resolution
        blocker — so merge must be refused before CI checks run."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        (tmp_path / ".auto-coder").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".auto-coder" / "stale_review_threads.json").write_text("{not valid json", encoding="utf-8")

        config = AutomationConfig()
        config.AUTO_MERGE = True
        pr_data = {"number": 123, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-123", "sha": "123abc456"}}
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []

        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        assert any("registry could not be read" in a for a in actions)
        mock_merge_pr.assert_not_called()
        mock_exit_if_in_progress.assert_not_called()

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress")
    @patch("auto_coder.pr_processor._merge_pr")
    def test_github_side_marker_alone_blocks_a_fresh_run_with_no_local_state(self, mock_merge_pr, mock_exit_if_in_progress, tmp_path, monkeypatch):
        """[P1] Models: resolve -> head advances -> unresolve retries exhausted
        -> registry.record() itself also fails (disk full/permissions), so no
        local state survives. A brand-new `_handle_pr_merge()` invocation with
        an empty local registry (simulating a process restart) must still
        discover the durable GitHub-side marker and refuse to merge until the
        stale thread is confirmed unresolved."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        # No local registry file at all — models the write having failed and
        # the process having restarted with zero in-memory state.

        config = AutomationConfig()
        config.AUTO_MERGE = True
        pr_data = {"number": 123, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-123", "sha": "123abc456"}}

        client = MagicMock()
        client.get_authenticated_user_login.return_value = "agent[bot]"
        client.get_pr_review_threads_strict.return_value = [
            ReviewThread(
                id="thread-1",
                is_resolved=True,  # GitHub still reports it resolved (the false-success state)
                comments=[
                    ReviewThreadComment(database_id=1, body="finding", author_login="chatgpt-codex-connector[bot]"),
                    ReviewThreadComment(database_id=2, body="<!-- auto-coder-stale-review-thread-blocker:v1 -->", author_login="agent[bot]"),
                ],
            )
        ]
        client.unresolve_review_thread.side_effect = Exception("still failing")

        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        assert any("thread(s) thread-1" in a and "stale head" in a for a in actions)
        mock_merge_pr.assert_not_called()
        mock_exit_if_in_progress.assert_not_called()

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress")
    @patch("auto_coder.pr_processor._merge_pr")
    def test_github_marker_scan_failure_blocks_merge(self, mock_merge_pr, mock_exit_if_in_progress, tmp_path, monkeypatch):
        """[P1] Regression oracle: an empty local registry (nothing persisted
        for this PR) plus a GitHub-side marker scan that raises must still
        refuse to merge — the scan failure must fail closed exactly like a
        corrupt local registry, never be silently treated as "no marker-only
        blockers", since it may be the only surviving evidence of a stale
        resolution."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        # No local registry file at all — empty local state.

        config = AutomationConfig()
        config.AUTO_MERGE = True
        pr_data = {"number": 123, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-123", "sha": "123abc456"}}

        client = MagicMock()
        client.get_pr_review_threads_strict.side_effect = Exception("transient GitHub API error")

        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        assert any("registry could not be read" in a or "stale-resolution markers" in a for a in actions)
        mock_merge_pr.assert_not_called()
        mock_exit_if_in_progress.assert_not_called()

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._get_claimed_review_thread_state")
    def test_blocking_unresolved_thread_still_blocks_merge(
        self,
        mock_claimed_state,
        mock_checks,
        mock_mergeable,
        mock_exit_if_in_progress,
    ):
        from auto_coder.pr_processor import ClaimedReviewThreadGateState

        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        mock_claimed_state.return_value = ClaimedReviewThreadGateState(has_blocking_unresolved=True)

        config = AutomationConfig()
        config.AUTO_MERGE = True
        pr_data = {"number": 123, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-123", "sha": "123abc456"}}
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []

        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        assert any("Skipping merge for PR #123 due to unresolved review threads" in a for a in actions)
