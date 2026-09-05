"""Tests for issue #1731: deprioritize PRs blocked awaiting adversarial-review fixes.

A PR whose current HEAD already failed Auto-Coder's own adversarial
validation with material violations (NEEDS_FIX / NEEDS_TESTS) has no further
normal Auto-Coder action to perform until new code arrives, so it must not
keep competing at the ordinary auto-merge-candidate priority (2). The
classification is scoped to the exact validated HEAD SHA and must never be
triggered by validator errors/timeouts, a missing linked-Issue oracle, or
generic/human review state (REQ-001..REQ-004). An independently applicable
higher-priority condition (unmergeable, urgent, breaking-change) still wins
(REQ-005).
"""

from unittest.mock import Mock, patch

from auto_coder.adversarial_validator import adversarial_validation_comment_marker
from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine
from auto_coder.pr_processor import is_current_head_adversarial_review_blocked
from auto_coder.util.github_action import GitHubActionsStatusResult

# Matches the autouse `_stub_reviewer_app_identity` fixture in tests/conftest.py.
REVIEWER_LOGIN = "auto-coder-reviewer[bot]"


def _adversarial_review(head_sha: str, status: str, login: str = REVIEWER_LOGIN, state: str = "CHANGES_REQUESTED") -> dict:
    """Build a native PR review shaped like github_app_reviewer.GitHubAppReviewer.publish()."""
    marker = adversarial_validation_comment_marker(head_sha)
    body = f"{marker}\n## PR adversarial validation: {status}\nDetails."
    return {"id": 1, "state": state, "body": body, "user": {"login": login}}


def _pr_data(number: int, head_sha: str, mergeable: bool = True, labels=None, linked_issue=42) -> dict:
    return {
        "number": number,
        "title": "Some PR",
        "body": f"Fixes #{linked_issue}" if linked_issue else "",
        "head": {"ref": f"pr-{number}", "sha": head_sha},
        "labels": labels or [],
        "mergeable": mergeable,
        "created_at": "2024-01-01T00:00:00Z",
    }


class TestAdversarialReviewBlockedUnit:
    """Direct coverage of is_current_head_adversarial_review_blocked (REQ-001..REQ-004)."""

    def test_needs_fix_at_current_head_blocks(self, mock_github_client, test_repo_name):
        head_sha = "a" * 40
        pr_data = _pr_data(1, head_sha)
        mock_github_client.get_pr_reviews_strict.return_value = [_adversarial_review(head_sha, "NEEDS_FIX")]
        mock_github_client.get_issue.return_value = {"number": 42, "title": "Spec", "body": ""}

        assert is_current_head_adversarial_review_blocked(mock_github_client, test_repo_name, pr_data, AutomationConfig()) is True

    def test_needs_tests_at_current_head_blocks(self, mock_github_client, test_repo_name):
        head_sha = "b" * 40
        pr_data = _pr_data(1, head_sha)
        mock_github_client.get_pr_reviews_strict.return_value = [_adversarial_review(head_sha, "NEEDS_TESTS")]
        mock_github_client.get_issue.return_value = {"number": 42, "title": "Spec", "body": ""}

        assert is_current_head_adversarial_review_blocked(mock_github_client, test_repo_name, pr_data, AutomationConfig()) is True

    def test_stale_sha_does_not_block_new_head(self, mock_github_client, test_repo_name):
        """AS-002/AS-003: a NEEDS_FIX verdict for an older SHA must not block the new HEAD.

        Rejects an implementation that stores a PR-level "failed adversarial
        validation" flag without binding it to the validated SHA: only a
        review body carrying the *new* head's own marker may block it.
        """
        old_sha = "c" * 40
        new_sha = "d" * 40
        pr_data = _pr_data(1, new_sha)
        mock_github_client.get_pr_reviews_strict.return_value = [_adversarial_review(old_sha, "NEEDS_FIX")]
        mock_github_client.get_pr_comments.return_value = []
        mock_github_client.get_issue.return_value = {"number": 42, "title": "Spec", "body": ""}

        assert is_current_head_adversarial_review_blocked(mock_github_client, test_repo_name, pr_data, AutomationConfig()) is False

    def test_validator_error_and_indeterminate_results_are_not_blocked(self, mock_github_client, test_repo_name):
        """REQ-003/AS-004: ERROR, BLOCKED, and INCONCLUSIVE must be retried, not waited on."""
        for status in ("ERROR", "BLOCKED", "INCONCLUSIVE"):
            head_sha = "e" * 40
            pr_data = _pr_data(1, head_sha)
            mock_github_client.get_pr_reviews_strict.return_value = [_adversarial_review(head_sha, status, state="COMMENTED")]
            mock_github_client.get_issue.return_value = {"number": 42, "title": "Spec", "body": ""}

            assert is_current_head_adversarial_review_blocked(mock_github_client, test_repo_name, pr_data, AutomationConfig()) is False, status

    def test_pass_at_current_head_is_not_blocked(self, mock_github_client, test_repo_name):
        head_sha = "f" * 40
        pr_data = _pr_data(1, head_sha)
        mock_github_client.get_pr_reviews_strict.return_value = [_adversarial_review(head_sha, "PASS", state="APPROVED")]
        mock_github_client.get_issue.return_value = {"number": 42, "title": "Spec", "body": ""}

        assert is_current_head_adversarial_review_blocked(mock_github_client, test_repo_name, pr_data, AutomationConfig()) is False

    def test_lookup_failure_fails_open(self, mock_github_client, test_repo_name):
        """An indeterminate/failed lookup must never be treated as a blocking verdict."""
        pr_data = _pr_data(1, "1" * 40)
        mock_github_client.get_issue.return_value = {"number": 42, "title": "Spec", "body": ""}
        mock_github_client.get_pr_reviews_strict.side_effect = RuntimeError("GitHub API unavailable")

        assert is_current_head_adversarial_review_blocked(mock_github_client, test_repo_name, pr_data, AutomationConfig()) is False

    def test_no_linked_issue_oracle_is_not_blocked(self, mock_github_client, test_repo_name):
        """Without a linked-Issue specification oracle, adversarial validation never applies."""
        head_sha = "2" * 40
        pr_data = _pr_data(1, head_sha, linked_issue=None)
        mock_github_client.get_pr_reviews_strict.return_value = [_adversarial_review(head_sha, "NEEDS_FIX")]

        assert is_current_head_adversarial_review_blocked(mock_github_client, test_repo_name, pr_data, AutomationConfig()) is False

    def test_generic_changes_requested_from_other_reviewer_is_not_blocked(self, mock_github_client, test_repo_name):
        """REQ-004: a generic human CHANGES_REQUESTED review must not trigger this classification."""
        head_sha = "3" * 40
        pr_data = _pr_data(1, head_sha)
        mock_github_client.get_issue.return_value = {"number": 42, "title": "Spec", "body": ""}
        mock_github_client.get_pr_comments.return_value = []
        mock_github_client.get_pr_reviews_strict.return_value = [
            {"id": 1, "state": "CHANGES_REQUESTED", "body": "Please address these nits.", "user": {"login": "some-human-reviewer"}},
        ]

        assert is_current_head_adversarial_review_blocked(mock_github_client, test_repo_name, pr_data, AutomationConfig()) is False

    def test_dependabot_pr_is_never_blocked(self, mock_github_client, test_repo_name):
        head_sha = "4" * 40
        pr_data = _pr_data(1, head_sha)
        pr_data["author"] = "dependabot[bot]"
        mock_github_client.get_pr_reviews_strict.return_value = [_adversarial_review(head_sha, "NEEDS_FIX")]
        mock_github_client.get_issue.return_value = {"number": 42, "title": "Spec", "body": ""}

        assert is_current_head_adversarial_review_blocked(mock_github_client, test_repo_name, pr_data, AutomationConfig()) is False

    def test_disabled_adversarial_validation_is_never_blocked(self, mock_github_client, test_repo_name):
        head_sha = "5" * 40
        pr_data = _pr_data(1, head_sha)
        mock_github_client.get_pr_reviews_strict.return_value = [_adversarial_review(head_sha, "NEEDS_FIX")]
        mock_github_client.get_issue.return_value = {"number": 42, "title": "Spec", "body": ""}
        config = AutomationConfig()
        object.__setattr__(config, "ENABLE_ADVERSARIAL_VALIDATION", False)

        assert is_current_head_adversarial_review_blocked(mock_github_client, test_repo_name, pr_data, config) is False


class TestAdversarialReviewBlockedPriorityIntegration:
    """Exercise the production candidate-collection path (AutomationEngine._get_candidates)."""

    @patch("auto_coder.util.github_action._check_github_actions_status")
    def test_needs_fix_pr_scheduled_at_priority_one(self, mock_check_actions, mock_github_client, mock_gemini_client, test_repo_name):
        """AS-001: an unchanged HEAD blocked on adversarial review is deprioritized."""
        engine = AutomationEngine(mock_github_client)
        head_sha = "a" * 40

        pr_data = _pr_data(1, head_sha)
        mock_github_client.get_open_prs_json.return_value = [pr_data]
        mock_github_client.get_pr_details.return_value = pr_data
        mock_github_client.get_pr_comments.return_value = []
        mock_github_client.get_pr_commits.return_value = []
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False
        mock_github_client.get_issue.return_value = {"number": 42, "title": "Spec", "body": ""}
        mock_github_client.get_pr_reviews_strict.return_value = [_adversarial_review(head_sha, "NEEDS_FIX")]

        mock_check_actions.return_value = GitHubActionsStatusResult(success=True, ids=[])

        candidates = engine._get_candidates(test_repo_name, max_items=10)

        assert len(candidates) == 1
        assert candidates[0].data["number"] == 1
        assert candidates[0].priority == 1

    @patch("auto_coder.util.github_action._check_github_actions_status")
    def test_new_commit_clears_stale_block_even_before_revalidation(self, mock_check_actions, mock_github_client, mock_gemini_client, test_repo_name):
        """AS-002: advancing HEAD clears the stale priority reduction immediately.

        The PR's blocking verdict was recorded for the old SHA; simply moving
        HEAD forward (with no adversarial result yet for the new SHA) must
        restore the ordinary auto-merge-candidate priority.
        """
        engine = AutomationEngine(mock_github_client)
        old_sha = "b" * 40
        new_sha = "c" * 40

        pr_data = _pr_data(1, new_sha)
        mock_github_client.get_open_prs_json.return_value = [pr_data]
        mock_github_client.get_pr_details.return_value = pr_data
        mock_github_client.get_pr_comments.return_value = []
        mock_github_client.get_pr_commits.return_value = []
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False
        mock_github_client.get_issue.return_value = {"number": 42, "title": "Spec", "body": ""}
        # Only the old HEAD's blocking review exists; nothing published for new_sha yet.
        mock_github_client.get_pr_reviews_strict.return_value = [_adversarial_review(old_sha, "NEEDS_FIX")]

        mock_check_actions.return_value = GitHubActionsStatusResult(success=True, ids=[])

        candidates = engine._get_candidates(test_repo_name, max_items=10)

        assert len(candidates) == 1
        assert candidates[0].priority == 2

    @patch("auto_coder.util.github_action._check_github_actions_status")
    def test_unmergeable_pr_keeps_unmergeable_priority_over_adversarial_block(self, mock_check_actions, mock_github_client, mock_gemini_client, test_repo_name):
        """AS-005: an unmergeable, conflict-eligible PR retains priority 2, not 1."""
        engine = AutomationEngine(mock_github_client)
        head_sha = "d" * 40

        pr_data = _pr_data(1, head_sha, mergeable=False)
        mock_github_client.get_open_prs_json.return_value = [pr_data]
        mock_github_client.get_pr_details.return_value = pr_data
        mock_github_client.get_pr_comments.return_value = []
        mock_github_client.get_pr_commits.return_value = []
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False
        mock_github_client.get_issue.return_value = {"number": 42, "title": "Spec", "body": ""}
        mock_github_client.get_pr_reviews_strict.return_value = [_adversarial_review(head_sha, "NEEDS_FIX")]

        mock_check_actions.return_value = GitHubActionsStatusResult(success=True, ids=[])

        candidates = engine._get_candidates(test_repo_name, max_items=10)

        assert len(candidates) == 1
        assert candidates[0].priority == 2

    @patch("auto_coder.util.github_action._check_github_actions_status")
    def test_urgent_label_keeps_urgent_priority_over_adversarial_block(self, mock_check_actions, mock_github_client, mock_gemini_client, test_repo_name):
        """REQ-005: an urgent, mergeable PR retains priority 3, not 1."""
        engine = AutomationEngine(mock_github_client)
        head_sha = "e" * 40

        pr_data = _pr_data(1, head_sha, labels=["urgent"])
        mock_github_client.get_open_prs_json.return_value = [pr_data]
        mock_github_client.get_pr_details.return_value = pr_data
        mock_github_client.get_pr_comments.return_value = []
        mock_github_client.get_pr_commits.return_value = []
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False
        mock_github_client.get_issue.return_value = {"number": 42, "title": "Spec", "body": ""}
        mock_github_client.get_pr_reviews_strict.return_value = [_adversarial_review(head_sha, "NEEDS_FIX")]

        mock_check_actions.return_value = GitHubActionsStatusResult(success=True, ids=[])

        candidates = engine._get_candidates(test_repo_name, max_items=10)

        assert len(candidates) == 1
        assert candidates[0].priority == 3

    @patch("auto_coder.util.github_action._check_github_actions_status")
    def test_breaking_change_label_keeps_highest_priority_over_adversarial_block(self, mock_check_actions, mock_github_client, mock_gemini_client, test_repo_name):
        """REQ-005: a breaking-change, mergeable PR retains priority 7, not 1."""
        engine = AutomationEngine(mock_github_client)
        head_sha = "8" * 40

        pr_data = _pr_data(1, head_sha, labels=["breaking-change"])
        mock_github_client.get_open_prs_json.return_value = [pr_data]
        mock_github_client.get_pr_details.return_value = pr_data
        mock_github_client.get_pr_comments.return_value = []
        mock_github_client.get_pr_commits.return_value = []
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False
        mock_github_client.get_issue.return_value = {"number": 42, "title": "Spec", "body": ""}
        mock_github_client.get_pr_reviews_strict.return_value = [_adversarial_review(head_sha, "NEEDS_FIX")]

        mock_check_actions.return_value = GitHubActionsStatusResult(success=True, ids=[])

        candidates = engine._get_candidates(test_repo_name, max_items=10)

        assert len(candidates) == 1
        assert candidates[0].priority == 7

    @patch("auto_coder.util.github_action._check_github_actions_status")
    def test_validator_error_at_current_head_keeps_ordinary_priority(self, mock_check_actions, mock_github_client, mock_gemini_client, test_repo_name):
        """AS-004: an indeterminate validator result must remain actionable at priority 2."""
        engine = AutomationEngine(mock_github_client)
        head_sha = "6" * 40

        pr_data = _pr_data(1, head_sha)
        mock_github_client.get_open_prs_json.return_value = [pr_data]
        mock_github_client.get_pr_details.return_value = pr_data
        mock_github_client.get_pr_comments.return_value = []
        mock_github_client.get_pr_commits.return_value = []
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False
        mock_github_client.get_issue.return_value = {"number": 42, "title": "Spec", "body": ""}
        mock_github_client.get_pr_reviews_strict.return_value = [_adversarial_review(head_sha, "ERROR", state="COMMENTED")]

        mock_check_actions.return_value = GitHubActionsStatusResult(success=True, ids=[])

        candidates = engine._get_candidates(test_repo_name, max_items=10)

        assert len(candidates) == 1
        assert candidates[0].priority == 2

    @patch("auto_coder.util.github_action._check_github_actions_status")
    def test_unrelated_review_state_alone_is_insufficient(self, mock_check_actions, mock_github_client, mock_gemini_client, test_repo_name):
        """AS-006: unresolved human review feedback alone must not trigger priority 1."""
        engine = AutomationEngine(mock_github_client)
        head_sha = "7" * 40

        pr_data = _pr_data(1, head_sha)
        mock_github_client.get_open_prs_json.return_value = [pr_data]
        mock_github_client.get_pr_details.return_value = pr_data
        mock_github_client.get_pr_comments.return_value = []
        mock_github_client.get_pr_commits.return_value = []
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False
        mock_github_client.get_issue.return_value = {"number": 42, "title": "Spec", "body": ""}
        mock_github_client.get_pr_reviews_strict.return_value = [
            {"id": 1, "state": "CHANGES_REQUESTED", "body": "This needs work.", "user": {"login": "some-human-reviewer"}},
        ]

        mock_check_actions.return_value = GitHubActionsStatusResult(success=True, ids=[])

        candidates = engine._get_candidates(test_repo_name, max_items=10)

        assert len(candidates) == 1
        assert candidates[0].priority == 2
