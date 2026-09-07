"""Comprehensive tests for PR review thread gate kill switch (Issue #1816 / Parent #1811).

Covers:
- REQ-001 / AS-001: When pr_review_thread_gate is false, unresolved human review threads do not
  block internal merge eligibility, lower PR processing priority, or trigger automatic repair.
- REQ-002 / AS-002: Disabled mode is non-destructive; unresolved threads remain untouched on GitHub.
- REQ-003 / AS-003: Thread lookup failure (GraphQL error, strict getter unavailable) does not defer
  or fail the PR when gate is disabled; no fail-closed has_blocking_unresolved substitute is applied.
- REQ-004 / AS-004: When gate is disabled and no other feature needs thread data, review-thread
  lookups are not invoked solely to prove the disabled gate is clear.
- REQ-005 / AS-005: Disabling pr_review_thread_gate does not disable pr_adversarial_validation;
  authoritative current-HEAD NEEDS_FIX verdict still blocks and reprioritizes the PR.
- REQ-006 / AS-006: Disabling internal gate does not bypass GitHub branch protection / required
  reviews; GitHub merge rejection remains authoritative.
- REQ-007 / AS-007 / AS-008: Re-enabling gate resumes ordinary enabled behavior from authoritative
  current review-thread state on GitHub without carrying fake clear results forward.
- REQ-008: Regression coverage originates from production PR processing (process_pull_request and
  AutomationEngine._get_candidates) across thread-state acquisition, priority/repair, and merge eligibility.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.adversarial_validator import adversarial_validation_comment_marker
from auto_coder.automation_config import AutomationConfig, EmptyPRResult, PRProcessingOutcome, StaleJulesPRResult
from auto_coder.automation_engine import AutomationEngine
from auto_coder.llm_backend_config import (
    get_feature_switch_from_config,
    get_pr_review_thread_gate_from_config,
)
from auto_coder.pr_processor import (
    CloudReviewRepairResult,
    _handle_pr_merge,
    _is_pr_review_thread_gate_enabled,
    process_pull_request,
)
from auto_coder.util.gh_cache import GitHubClient, ReviewThread, ReviewThreadComment
from auto_coder.util.github_action import GitHubActionsStatusResult

REVIEWER_LOGIN = "auto-coder-reviewer[bot]"


def _adversarial_review(
    head_sha: str,
    status: str,
    login: str = REVIEWER_LOGIN,
    state: str = "CHANGES_REQUESTED",
) -> dict:
    marker = adversarial_validation_comment_marker(head_sha)
    body = f"{marker}\n## PR adversarial validation: {status}\nDetails."
    return {"id": 1, "state": state, "body": body, "user": {"login": login}}


def _pr_data(
    number: int = 100,
    head_sha: str = "a" * 40,
    mergeable: bool = True,
    labels: list | None = None,
    linked_issue: int = 42,
    ref: str = "feature-branch",
) -> dict:
    return {
        "number": number,
        "title": f"Feature PR #{number}",
        "body": f"Fixes #{linked_issue}" if linked_issue else "",
        "head": {"ref": ref, "sha": head_sha},
        "labels": labels or [],
        "mergeable": mergeable,
        "created_at": "2024-01-01T00:00:00Z",
    }


def _make_human_thread(
    thread_id: str = "thread-human-1",
    author_login: str = "human-dev",
    is_resolved: bool = False,
) -> ReviewThread:
    return ReviewThread(
        id=thread_id,
        is_resolved=is_resolved,
        is_outdated=False,
        comments=[
            ReviewThreadComment(
                database_id=202,
                body="Please refactor this helper function.",
                author_login=author_login,
            ),
        ],
    )


class MockGitHubClient(MagicMock):
    """MagicMock subclass defining get_pr_review_threads_strict on its type."""

    def get_pr_review_threads_strict(self, repo_name: str, pr_number: int):
        pass


# ---------------------------------------------------------------------------
# Configuration & Switch Resolution Tests
# ---------------------------------------------------------------------------


class TestPrReviewThreadGateConfig:
    """Canonical switch pr_review_thread_gate configuration and resolution."""

    def test_canonical_switch_defaults_to_true(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("AUTO_CODER_PR_REVIEW_THREAD_GATE", raising=False)
        config = AutomationConfig()
        assert config.pr_review_thread_gate is True
        assert get_pr_review_thread_gate_from_config() is True

    def test_canonical_switch_explicit_false(self):
        config = AutomationConfig(pr_review_thread_gate=False)
        assert config.pr_review_thread_gate is False

    def test_canonical_env_var_false(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("AUTO_CODER_PR_REVIEW_THREAD_GATE", "false")
        config = AutomationConfig()
        assert config.pr_review_thread_gate is False
        assert get_pr_review_thread_gate_from_config() is False

    def test_config_toml_repo_scoped_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        home_dir = tmp_path / "home"
        auto_coder_dir = home_dir / ".auto-coder"
        auto_coder_dir.mkdir(parents=True)
        base_config = auto_coder_dir / "config.toml"
        base_config.write_text(
            """
[features]
pr_review_thread_gate = true
""",
            encoding="utf-8",
        )

        repo_dir = auto_coder_dir / "custom" / "repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / "config.toml").write_text(
            """
[features]
pr_review_thread_gate = false
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.delenv("AUTO_CODER_PR_REVIEW_THREAD_GATE", raising=False)

        assert get_feature_switch_from_config("pr_review_thread_gate", repo_name="other/repo") is True
        assert get_feature_switch_from_config("pr_review_thread_gate", repo_name="custom/repo") is False
        assert get_pr_review_thread_gate_from_config(repo_name="custom/repo") is False

    def test_is_pr_review_thread_gate_enabled_respects_repo_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        home_dir = tmp_path / "home"
        auto_coder_dir = home_dir / ".auto-coder"
        auto_coder_dir.mkdir(parents=True)
        base_config = auto_coder_dir / "config.toml"
        base_config.write_text(
            """
[features]
pr_review_thread_gate = true
""",
            encoding="utf-8",
        )

        repo_disabled_dir = auto_coder_dir / "disabled" / "repo"
        repo_disabled_dir.mkdir(parents=True)
        (repo_disabled_dir / "config.toml").write_text(
            """
[features]
pr_review_thread_gate = false
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.delenv("AUTO_CODER_PR_REVIEW_THREAD_GATE", raising=False)

        config = AutomationConfig(pr_review_thread_gate=True)
        assert _is_pr_review_thread_gate_enabled(config, "enabled/repo") is True
        assert _is_pr_review_thread_gate_enabled(config, "disabled/repo") is False


# ---------------------------------------------------------------------------
# AS-001 / REQ-001 / REQ-008: Ordinary unresolved human thread is bypassed
# ---------------------------------------------------------------------------


class TestAS001UnresolvedHumanThreadBypassed:
    """AS-001: Unresolved human thread does not lower priority, block merge, or trigger repair."""

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._close_empty_pr", return_value=EmptyPRResult(closed=False))
    @patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult(closed=False))
    @patch("auto_coder.pr_processor._delegate_cloud_review_thread_repair")
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_production_pr_processing_bypasses_human_thread_and_merges(
        self,
        mock_merge_pr,
        mock_repair,
        mock_jules_close,
        mock_empty_close,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        head_sha = "a" * 40
        pr_data = _pr_data(100, head_sha, linked_issue=42)

        client = MagicMock(spec=GitHubClient)
        human_thread = _make_human_thread()
        client.get_pr_review_threads_strict.return_value = [human_thread]
        client.get_pull_request.return_value = {"head": {"sha": head_sha}}
        client.get_issue.return_value = {"number": 42, "title": "Issue", "body": "Spec"}
        client.get_pr_comments.return_value = []
        client.get_pr_reviews_strict.return_value = [_adversarial_review(head_sha, "PASS")]

        config = AutomationConfig(pr_review_thread_gate=False)

        # Run from production PR processing origin
        result = process_pull_request(client, config, "owner/repo", pr_data)

        # Merge must succeed
        mock_merge_pr.assert_called_once()
        assert any("Successfully merged PR #100" in a for a in result.actions_taken)
        assert result.outcome == PRProcessingOutcome.SUCCESS

        # Automatic repair must NOT be triggered
        mock_repair.assert_not_called()

    @patch("auto_coder.util.github_action._check_github_actions_status")
    def test_candidate_priority_retains_auto_merge_priority(self, mock_check_actions, mock_github_client):
        """Unresolved thread does not lower candidate priority (retains 2)."""
        head_sha = "a" * 40
        pr_data = _pr_data(100, head_sha)
        mock_github_client.get_open_prs_json.return_value = [pr_data]
        mock_github_client.get_pr_details.return_value = pr_data
        mock_github_client.get_pr_comments.return_value = []
        mock_github_client.get_pr_commits.return_value = []
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False
        mock_github_client.get_issue.return_value = {"number": 42, "title": "Spec", "body": ""}
        mock_github_client.get_pr_reviews_strict.return_value = []
        mock_check_actions.return_value = GitHubActionsStatusResult(success=True, ids=[1])

        engine = AutomationEngine(mock_github_client)
        engine.config = AutomationConfig(pr_review_thread_gate=False)

        candidates = engine._get_candidates("owner/repo", max_items=10)
        assert len(candidates) == 1
        assert candidates[0].data["number"] == 100
        assert candidates[0].priority == 2


# ---------------------------------------------------------------------------
# AS-002 / REQ-002: Disabled mode is non-destructive
# ---------------------------------------------------------------------------


class TestAS002DisabledModeIsNonDestructive:
    """AS-002: Threads remain unresolved and unchanged on GitHub; no synthetic resolutions/replies."""

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._close_empty_pr", return_value=EmptyPRResult(closed=False))
    @patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult(closed=False))
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_repeated_processing_does_not_mutate_review_threads_on_github(
        self,
        mock_merge_pr,
        mock_jules_close,
        mock_empty_close,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        head_sha = "a" * 40
        pr_data = _pr_data(100, head_sha, linked_issue=42)

        client = MagicMock(spec=GitHubClient)
        human_thread = _make_human_thread(thread_id="thread-human-1")
        client.get_pr_review_threads_strict.return_value = [human_thread]
        client.get_pull_request.return_value = {"head": {"sha": head_sha}}
        client.get_issue.return_value = {"number": 42, "title": "Issue", "body": "Spec"}
        client.get_pr_comments.return_value = []
        client.get_pr_reviews_strict.return_value = [_adversarial_review(head_sha, "PASS")]

        client.resolve_pr_review_thread = MagicMock()
        client.dismiss_pull_request_review = MagicMock()
        client.delete_comment = MagicMock()
        client.edit_comment = MagicMock()
        client.add_comment_to_pr = MagicMock()

        config = AutomationConfig(pr_review_thread_gate=False)

        # Process multiple times
        for _ in range(3):
            process_pull_request(client, config, "owner/repo", pr_data)

        # Threads must NOT be mutated on GitHub
        client.resolve_pr_review_thread.assert_not_called()
        client.dismiss_pull_request_review.assert_not_called()
        client.delete_comment.assert_not_called()
        client.edit_comment.assert_not_called()
        assert human_thread.is_resolved is False


# ---------------------------------------------------------------------------
# AS-003 / REQ-003 / REQ-008: Thread lookup failure does not stop the PR
# ---------------------------------------------------------------------------


class TestAS003ThreadLookupFailureDoesNotStopPR:
    """AS-003: Review-thread GraphQL failure alone does not defer/fail PR; no fail-closed substitute."""

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._close_empty_pr", return_value=EmptyPRResult(closed=False))
    @patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult(closed=False))
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_graphql_lookup_failure_does_not_fail_or_defer_pr(
        self,
        mock_merge_pr,
        mock_jules_close,
        mock_empty_close,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        head_sha = "a" * 40
        pr_data = _pr_data(100, head_sha, linked_issue=42)

        client = MagicMock(spec=GitHubClient)
        # GraphQL lookup fails with network error
        client.get_pr_review_threads_strict.side_effect = Exception("GraphQL Network Error: 502 Bad Gateway")
        client.get_pull_request.return_value = {"head": {"sha": head_sha}}
        client.get_issue.return_value = {"number": 42, "title": "Issue", "body": "Spec"}
        client.get_pr_comments.return_value = []
        client.get_pr_reviews_strict.return_value = [_adversarial_review(head_sha, "PASS")]

        config = AutomationConfig(pr_review_thread_gate=False)
        result = process_pull_request(client, config, "owner/repo", pr_data)

        # Must merge successfully without deferral or fail-closed block
        mock_merge_pr.assert_called_once()
        assert any("Successfully merged PR #100" in a for a in result.actions_taken)
        assert result.outcome == PRProcessingOutcome.SUCCESS

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._close_empty_pr", return_value=EmptyPRResult(closed=False))
    @patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult(closed=False))
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_missing_strict_getter_does_not_apply_fail_closed_substitute(
        self,
        mock_merge_pr,
        mock_jules_close,
        mock_empty_close,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        """When strict getter is unavailable, no fail-closed has_blocking_unresolved=True is applied."""
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        head_sha = "a" * 40
        pr_data = _pr_data(100, head_sha, linked_issue=42)

        # Client without get_pr_review_threads_strict
        class BasicClient:
            def get_pull_request(self, repo, pr):
                return {"head": {"sha": head_sha}}

            def get_issue(self, repo, issue):
                return {"number": 42, "title": "Issue", "body": "Spec"}

            def get_pr_comments(self, repo, pr):
                return []

            def get_pr_reviews_strict(self, repo, pr):
                return [_adversarial_review(head_sha, "PASS")]

        client = BasicClient()
        config = AutomationConfig(pr_review_thread_gate=False)
        result = process_pull_request(client, config, "owner/repo", pr_data)

        mock_merge_pr.assert_called_once()
        assert any("Successfully merged PR #100" in a for a in result.actions_taken)
        assert result.outcome == PRProcessingOutcome.SUCCESS


# ---------------------------------------------------------------------------
# AS-004 / REQ-004: No gate-only lookup
# ---------------------------------------------------------------------------


class TestAS004NoGateOnlyLookup:
    """AS-004: When gate is disabled and no other feature needs threads, lookup is not invoked."""

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._close_empty_pr", return_value=EmptyPRResult(closed=False))
    @patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult(closed=False))
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_no_thread_lookup_when_adversarial_validation_disabled(
        self,
        mock_merge_pr,
        mock_jules_close,
        mock_empty_close,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        head_sha = "a" * 40
        pr_data = _pr_data(100, head_sha, linked_issue=42)

        client = MagicMock(spec=GitHubClient)
        client.get_pull_request.return_value = {"head": {"sha": head_sha}}

        config = AutomationConfig(
            pr_review_thread_gate=False,
            pr_adversarial_validation=False,
        )
        result = process_pull_request(client, config, "owner/repo", pr_data)

        # Thread lookup methods must NOT be invoked solely to prove gate is clear
        client.get_pr_review_threads_strict.assert_not_called()
        client.has_unresolved_review_threads.assert_not_called()
        mock_merge_pr.assert_called_once()
        assert any("Successfully merged PR #100" in a for a in result.actions_taken)

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._close_empty_pr", return_value=EmptyPRResult(closed=False))
    @patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult(closed=False))
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_no_thread_lookup_when_adversarial_validation_not_applicable(
        self,
        mock_merge_pr,
        mock_jules_close,
        mock_empty_close,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        """When PR has no linked issue, adversarial validation is not applicable, so no lookup is performed."""
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        head_sha = "a" * 40
        # Issue-less PR
        pr_data = _pr_data(100, head_sha, linked_issue=0)

        client = MagicMock(spec=GitHubClient)
        client.get_pull_request.return_value = {"head": {"sha": head_sha}}

        config = AutomationConfig(
            pr_review_thread_gate=False,
            pr_adversarial_validation=True,
        )
        result = process_pull_request(client, config, "owner/repo", pr_data)

        client.get_pr_review_threads_strict.assert_not_called()
        client.has_unresolved_review_threads.assert_not_called()
        mock_merge_pr.assert_called_once()
        assert any("Successfully merged PR #100" in a for a in result.actions_taken)


# ---------------------------------------------------------------------------
# AS-005 / REQ-005: Adversarial validation remains independent
# ---------------------------------------------------------------------------


class TestAS005AdversarialValidationRemainsIndependent:
    """AS-005: NEEDS_FIX verdict still blocks and reprioritizes even when gate is disabled."""

    @patch("auto_coder.util.github_action._check_github_actions_status")
    def test_adversarial_needs_fix_lowers_priority_when_gate_disabled(self, mock_check_actions, mock_github_client):
        """Authoritative NEEDS_FIX still deprioritizes candidate to priority 1."""
        head_sha = "a" * 40
        pr_data = _pr_data(100, head_sha)
        mock_github_client.get_open_prs_json.return_value = [pr_data]
        mock_github_client.get_pr_details.return_value = pr_data
        mock_github_client.get_pr_comments.return_value = []
        mock_github_client.get_pr_commits.return_value = []
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False
        mock_github_client.get_issue.return_value = {"number": 42, "title": "Spec", "body": ""}
        mock_github_client.get_pr_reviews_strict.return_value = [_adversarial_review(head_sha, "NEEDS_FIX")]
        mock_check_actions.return_value = GitHubActionsStatusResult(success=True, ids=[1])

        engine = AutomationEngine(mock_github_client)
        engine.config = AutomationConfig(
            pr_review_thread_gate=False,
            pr_adversarial_validation=True,
        )

        candidates = engine._get_candidates("owner/repo", max_items=10)
        assert len(candidates) == 1
        assert candidates[0].priority == 1  # Blocked by adversarial validation

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._close_empty_pr", return_value=EmptyPRResult(closed=False))
    @patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult(closed=False))
    @patch("auto_coder.pr_processor._merge_pr")
    def test_adversarial_needs_fix_blocks_merge_when_gate_disabled(
        self,
        mock_merge_pr,
        mock_jules_close,
        mock_empty_close,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        head_sha = "a" * 40
        pr_data = _pr_data(100, head_sha, linked_issue=42)

        client = MagicMock(spec=GitHubClient)
        client.get_pr_review_threads_strict.return_value = []
        client.get_pull_request.return_value = {"head": {"sha": head_sha}}
        client.get_issue.return_value = {"number": 42, "title": "Issue", "body": "Spec"}
        client.get_pr_comments.return_value = []
        client.get_pr_reviews_strict.return_value = [_adversarial_review(head_sha, "NEEDS_FIX")]

        config = AutomationConfig(
            pr_review_thread_gate=False,
            pr_adversarial_validation=True,
        )
        result = process_pull_request(client, config, "owner/repo", pr_data)

        # Merge must be blocked by adversarial review
        mock_merge_pr.assert_not_called()
        assert any("Adversarial validation remains non-pass for PR #100: NEEDS_FIX" in a for a in result.actions_taken)


# ---------------------------------------------------------------------------
# AS-006 / REQ-006: GitHub-required review cannot be bypassed
# ---------------------------------------------------------------------------


class TestAS006GitHubRequiredReviewCannotBeBypassed:
    """AS-006: GitHub branch protection / required review rejection remains authoritative."""

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._close_empty_pr", return_value=EmptyPRResult(closed=False))
    @patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult(closed=False))
    @patch("auto_coder.pr_processor._merge_pr", return_value=False)
    def test_github_merge_refusal_is_authoritative(
        self,
        mock_merge_pr,
        mock_jules_close,
        mock_empty_close,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        head_sha = "a" * 40
        pr_data = _pr_data(100, head_sha, linked_issue=42)

        client = MagicMock(spec=GitHubClient)
        human_thread = _make_human_thread()
        client.get_pr_review_threads_strict.return_value = [human_thread]
        client.get_pull_request.return_value = {"head": {"sha": head_sha}}
        client.get_issue.return_value = {"number": 42, "title": "Issue", "body": "Spec"}
        client.get_pr_comments.return_value = []
        client.get_pr_reviews_strict.return_value = [_adversarial_review(head_sha, "PASS")]

        config = AutomationConfig(pr_review_thread_gate=False)
        result = process_pull_request(client, config, "owner/repo", pr_data)

        # Merge was attempted, but GitHub refused
        mock_merge_pr.assert_called_once()
        # Must not be reported as merged
        assert not any("Successfully merged PR #100" in a for a in result.actions_taken)
        assert result.outcome != PRProcessingOutcome.SUCCESS


# ---------------------------------------------------------------------------
# AS-007 / AS-008 / REQ-007: Re-enable semantics
# ---------------------------------------------------------------------------


class TestAS007AS008ReenableSemantics:
    """AS-007 & AS-008: Re-enabling gate resumes ordinary behavior from authoritative GitHub state."""

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._close_empty_pr", return_value=EmptyPRResult(closed=False))
    @patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult(closed=False))
    @patch("auto_coder.pr_processor._delegate_cloud_review_thread_repair")
    @patch("auto_coder.pr_processor._merge_pr")
    def test_as007_reenable_with_still_unresolved_thread_blocks_and_repairs(
        self,
        mock_merge_pr,
        mock_repair,
        mock_jules_close,
        mock_empty_close,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        """AS-007: Re-enabling gate sees still-unresolved thread, blocks merge and triggers repair."""
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        head_sha = "a" * 40
        pr_data = _pr_data(100, head_sha, linked_issue=42)

        client = MockGitHubClient(spec=GitHubClient)
        human_thread = _make_human_thread()
        client.get_pr_review_threads_strict = MagicMock(return_value=[human_thread])
        client.get_pull_request = MagicMock(return_value={"head": {"sha": head_sha}})
        client.get_issue = MagicMock(return_value={"number": 42, "title": "Issue", "body": "Spec"})
        client.get_pr_comments = MagicMock(return_value=[])
        client.get_pr_reviews_strict = MagicMock(return_value=[_adversarial_review(head_sha, "PASS")])
        mock_repair.return_value = CloudReviewRepairResult(["Delegated review repair"], delivered=True)

        config_enabled = AutomationConfig(pr_review_thread_gate=True)
        result = process_pull_request(client, config_enabled, "owner/repo", pr_data)

        # Merge must be skipped
        mock_merge_pr.assert_not_called()
        assert any("Skipping merge for PR #100 due to unresolved review threads" in a for a in result.actions_taken)
        # Automatic repair delegation must be called
        mock_repair.assert_called_once()

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._close_empty_pr", return_value=EmptyPRResult(closed=False))
    @patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult(closed=False))
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_as008_reenable_after_real_external_resolution_merges(
        self,
        mock_merge_pr,
        mock_jules_close,
        mock_empty_close,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        """AS-008: Reviewer genuinely resolves thread on GitHub before re-enable; PR merges cleanly."""
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        head_sha = "a" * 40
        pr_data = _pr_data(100, head_sha, linked_issue=42)

        client = MockGitHubClient(spec=GitHubClient)
        # Genuinely resolved on GitHub
        resolved_thread = _make_human_thread(is_resolved=True)
        client.get_pr_review_threads_strict = MagicMock(return_value=[resolved_thread])
        client.get_pull_request = MagicMock(return_value={"head": {"sha": head_sha}})
        client.get_issue = MagicMock(return_value={"number": 42, "title": "Issue", "body": "Spec"})
        client.get_pr_comments = MagicMock(return_value=[])
        client.get_pr_reviews_strict = MagicMock(return_value=[_adversarial_review(head_sha, "PASS")])

        config_enabled = AutomationConfig(pr_review_thread_gate=True)
        result = process_pull_request(client, config_enabled, "owner/repo", pr_data)

        # Merge must succeed with no stale blocked state
        mock_merge_pr.assert_called_once()
        assert any("Successfully merged PR #100" in a for a in result.actions_taken)
        assert result.outcome == PRProcessingOutcome.SUCCESS
