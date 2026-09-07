"""Comprehensive tests for PR adversarial validation canonical feature switch (Issue #1815 / Parent #1811).

Covers:
- REQ-001: Canonical switch pr_adversarial_validation and backward-compatibility aliases
  (ENABLE_ADVERSARIAL_VALIDATION, AUTO_CODER_ENABLE_ADVERSARIAL_VALIDATION, AUTO_CODER_PR_ADVERSARIAL_VALIDATION).
- REQ-002 / AS-001: When false, do not run adversarial reviewer, create reviews/comments, or increment attempts.
- REQ-003 / AS-002: When false, existing NEEDS_FIX/NEEDS_TESTS verdicts do not lower candidate priority (retains 2),
  block internal merge eligibility, or trigger repair loops.
- REQ-004 / REQ-006 / AS-003: When false, validator-originated review threads do not block merge and are NOT
  mutated (not resolved, dismissed, deleted, or edited) on GitHub.
- REQ-005 / AS-004 / AS-005: When false, human review threads, CI status, and GitHub mergeability/branch protection
  gates remain fully active and independent.
- REQ-007 / AS-006 / AS-007: Re-enabling restores ordinary enabled semantics for current HEAD; old-HEAD verdicts
  never apply to a new HEAD.
- REQ-008: Full regression coverage across priority selection and merge-gate evaluation.
"""

from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from auto_coder.adversarial_validator import (
    adversarial_validation_comment_marker,
)
from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine
from auto_coder.llm_backend_config import (
    get_feature_switch_from_config,
    get_pr_adversarial_validation_from_config,
)
from auto_coder.pr_processor import (
    _get_review_thread_gate_state,
    _handle_pr_merge,
    _is_pr_adversarial_validation_enabled,
    is_authoritative_adversarial_thread,
    is_current_head_adversarial_review_blocked,
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


def _make_adversarial_thread(
    thread_id: str = "thread-adv-1",
    author_login: str = REVIEWER_LOGIN,
    heading: str = "### Auto-Coder adversarial finding: Broken invariant",
    is_resolved: bool = False,
) -> ReviewThread:
    return ReviewThread(
        id=thread_id,
        is_resolved=is_resolved,
        is_outdated=False,
        comments=(
            ReviewThreadComment(
                database_id=101,
                body=f"{heading}\nViolated specification at lines 10-15.",
                author_login=author_login,
            ),
        ),
    )


def _make_human_thread(
    thread_id: str = "thread-human-1",
    author_login: str = "human-dev",
    is_resolved: bool = False,
) -> ReviewThread:
    return ReviewThread(
        id=thread_id,
        is_resolved=is_resolved,
        is_outdated=False,
        comments=(
            ReviewThreadComment(
                database_id=202,
                body="Please refactor this helper function.",
                author_login=author_login,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# REQ-001: Configuration & Aliases Synchronization
# ---------------------------------------------------------------------------


class TestPrAdversarialValidationConfig:
    """REQ-001: Canonical switch and backward-compatibility aliases synchronization."""

    def test_canonical_switch_defaults_to_true(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("AUTO_CODER_PR_ADVERSARIAL_VALIDATION", raising=False)
        monkeypatch.delenv("AUTO_CODER_ENABLE_ADVERSARIAL_VALIDATION", raising=False)

        config = AutomationConfig()
        assert config.pr_adversarial_validation is True
        assert config.ENABLE_ADVERSARIAL_VALIDATION is True

    def test_canonical_switch_explicit_false(self):
        config = AutomationConfig(pr_adversarial_validation=False)
        assert config.pr_adversarial_validation is False
        assert config.ENABLE_ADVERSARIAL_VALIDATION is False

    def test_legacy_parameter_sync_false(self):
        config = AutomationConfig(enable_adversarial_validation=False)
        assert config.pr_adversarial_validation is False
        assert config.ENABLE_ADVERSARIAL_VALIDATION is False

    def test_canonical_parameter_precedence_over_legacy_parameter(self):
        config = AutomationConfig(
            pr_adversarial_validation=False,
            enable_adversarial_validation=True,
        )
        assert config.pr_adversarial_validation is False
        assert config.ENABLE_ADVERSARIAL_VALIDATION is False

    def test_canonical_env_var_false(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("AUTO_CODER_PR_ADVERSARIAL_VALIDATION", "false")
        monkeypatch.delenv("AUTO_CODER_ENABLE_ADVERSARIAL_VALIDATION", raising=False)

        config = AutomationConfig()
        assert config.pr_adversarial_validation is False
        assert config.ENABLE_ADVERSARIAL_VALIDATION is False

    def test_legacy_env_var_false(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("AUTO_CODER_PR_ADVERSARIAL_VALIDATION", raising=False)
        monkeypatch.setenv("AUTO_CODER_ENABLE_ADVERSARIAL_VALIDATION", "false")

        config = AutomationConfig()
        assert config.pr_adversarial_validation is False
        assert config.ENABLE_ADVERSARIAL_VALIDATION is False

    def test_canonical_env_var_precedence_over_legacy_env_var(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("AUTO_CODER_PR_ADVERSARIAL_VALIDATION", "false")
        monkeypatch.setenv("AUTO_CODER_ENABLE_ADVERSARIAL_VALIDATION", "true")

        config = AutomationConfig()
        assert config.pr_adversarial_validation is False
        assert config.ENABLE_ADVERSARIAL_VALIDATION is False

    def test_config_toml_repo_scoped_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        home_dir = tmp_path / "home"
        auto_coder_dir = home_dir / ".auto-coder"
        auto_coder_dir.mkdir(parents=True)
        base_config = auto_coder_dir / "config.toml"
        base_config.write_text(
            """
[features]
pr_adversarial_validation = true
""",
            encoding="utf-8",
        )

        repo_dir = auto_coder_dir / "custom" / "repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / "config.toml").write_text(
            """
[features]
pr_adversarial_validation = false
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.delenv("AUTO_CODER_PR_ADVERSARIAL_VALIDATION", raising=False)
        monkeypatch.delenv("AUTO_CODER_ENABLE_ADVERSARIAL_VALIDATION", raising=False)

        assert get_feature_switch_from_config("pr_adversarial_validation", repo_name="other/repo") is True
        assert get_feature_switch_from_config("pr_adversarial_validation", repo_name="custom/repo") is False
        assert get_pr_adversarial_validation_from_config(repo_name="custom/repo") is False

    def test_is_pr_adversarial_validation_enabled_respects_repo_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        home_dir = tmp_path / "home"
        auto_coder_dir = home_dir / ".auto-coder"
        auto_coder_dir.mkdir(parents=True)
        base_config = auto_coder_dir / "config.toml"
        base_config.write_text(
            """
[features]
pr_adversarial_validation = true
""",
            encoding="utf-8",
        )

        repo_disabled_dir = auto_coder_dir / "disabled" / "repo"
        repo_disabled_dir.mkdir(parents=True)
        (repo_disabled_dir / "config.toml").write_text(
            """
[features]
pr_adversarial_validation = false
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.delenv("AUTO_CODER_PR_ADVERSARIAL_VALIDATION", raising=False)
        monkeypatch.delenv("AUTO_CODER_ENABLE_ADVERSARIAL_VALIDATION", raising=False)

        config = AutomationConfig(pr_adversarial_validation=True)
        assert _is_pr_adversarial_validation_enabled(config, "enabled/repo") is True
        assert _is_pr_adversarial_validation_enabled(config, "disabled/repo") is False


# ---------------------------------------------------------------------------
# REQ-002 / AS-001: Disabled on green unreviewed PR
# ---------------------------------------------------------------------------


class TestAS001DisabledOnGreenPRSkipsValidatorAndMerges:
    """AS-001 / REQ-002: When disabled on green unreviewed PR, skips validation and merges."""

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_skips_validation_and_merges_directly(
        self,
        mock_merge_pr,
        mock_worktree,
        mock_run_validation,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        head_sha = "f" * 40
        pr_data = _pr_data(100, head_sha, linked_issue=42)

        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.get_pull_request.return_value = {"head": {"sha": head_sha}}

        config = AutomationConfig(pr_adversarial_validation=False)
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_run_validation.assert_not_called()
        mock_worktree.assert_not_called()
        client.add_comment_to_pr.assert_not_called()
        mock_merge_pr.assert_called_once_with(
            "owner/repo",
            100,
            {},
            config,
            github_client=client,
            expected_head_sha=head_sha,
        )
        assert any("Successfully merged PR #100" in a for a in actions)


# ---------------------------------------------------------------------------
# REQ-003 / AS-002: Existing NEEDS_FIX / NEEDS_TESTS does not block or deprioritize
# ---------------------------------------------------------------------------


class TestAS002ExistingVerdictDoesNotBlockOrDeprioritize:
    """REQ-003 / AS-002: Existing NEEDS_FIX/NEEDS_TESTS does not lower priority or block merge when disabled."""

    def test_is_current_head_adversarial_review_blocked_returns_false_when_disabled(self):
        client = MagicMock()
        head_sha = "a" * 40
        pr_data = _pr_data(100, head_sha)
        client.get_pr_reviews_strict.return_value = [_adversarial_review(head_sha, "NEEDS_FIX")]
        client.get_issue.return_value = {"number": 42, "title": "Spec", "body": ""}

        config_disabled = AutomationConfig(pr_adversarial_validation=False)
        assert is_current_head_adversarial_review_blocked(client, "owner/repo", pr_data, config_disabled) is False

        config_enabled = AutomationConfig(pr_adversarial_validation=True)
        assert is_current_head_adversarial_review_blocked(client, "owner/repo", pr_data, config_enabled) is True

    @patch("auto_coder.util.github_action._check_github_actions_status")
    def test_candidate_priority_is_2_not_1_when_disabled(self, mock_check_actions, mock_github_client):
        """AS-002: Candidate priority remains 2 (normal merge candidate) rather than 1 when disabled."""
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
        mock_check_actions.return_value = GitHubActionsStatusResult(success=True, ids=[])

        engine = AutomationEngine(mock_github_client)
        engine.config = AutomationConfig(pr_adversarial_validation=False)

        candidates = engine._get_candidates("owner/repo", max_items=10)
        assert len(candidates) == 1
        assert candidates[0].data["number"] == 100
        assert candidates[0].priority == 2

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_handle_pr_merge_proceeds_despite_existing_needs_fix_when_disabled(
        self,
        mock_merge_pr,
        mock_run_validation,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        head_sha = "a" * 40
        pr_data = _pr_data(100, head_sha, linked_issue=42)

        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.get_pull_request.return_value = {"head": {"sha": head_sha}}
        client.get_pr_reviews_strict.return_value = [_adversarial_review(head_sha, "NEEDS_FIX")]

        config = AutomationConfig(pr_adversarial_validation=False)
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_run_validation.assert_not_called()
        mock_merge_pr.assert_called_once()
        assert any("Successfully merged PR #100" in a for a in actions)


# ---------------------------------------------------------------------------
# REQ-004 / REQ-006 / AS-003: Validator threads non-blocking & untouched
# ---------------------------------------------------------------------------


class TestAS003ValidatorThreadsNonBlockingAndUntouched:
    """REQ-004 / REQ-006 / AS-003: Authoritative validator threads do not block merge and are NOT mutated on GitHub."""

    def test_authoritative_adversarial_thread_identified(self):
        adv_thread = _make_adversarial_thread()
        human_thread = _make_human_thread()

        assert is_authoritative_adversarial_thread(adv_thread, "owner/repo") is True
        assert is_authoritative_adversarial_thread(human_thread, "owner/repo") is False

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_validator_thread_does_not_block_merge_and_does_not_mutate_github(
        self,
        mock_merge_pr,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        head_sha = "a" * 40
        pr_data = _pr_data(100, head_sha, linked_issue=42)

        client = GitHubClient(token="fake_token")
        validator_thread = _make_adversarial_thread(thread_id="thread-adv-1")
        client.get_pr_review_threads_strict = MagicMock(return_value=[validator_thread])
        client.get_pull_request = MagicMock(return_value={"head": {"sha": head_sha}})
        client.get_pull_request_metadata_strict = MagicMock(
            return_value={
                "number": 100,
                "head": {"ref": "feature-branch", "sha": head_sha},
                "body": "Fixes #42",
                "user": {"login": "some-author"},
                "state": "open",
            }
        )
        client.resolve_pr_review_thread = MagicMock()
        client.dismiss_pull_request_review = MagicMock()
        client.delete_comment = MagicMock()
        client.edit_comment = MagicMock()

        config = AutomationConfig(pr_adversarial_validation=False)
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        # Merge must succeed
        mock_merge_pr.assert_called_once()
        assert any("Successfully merged PR #100" in a for a in actions)

        # REQ-006: GitHub state must not be mutated
        client.resolve_pr_review_thread.assert_not_called()
        client.dismiss_pull_request_review.assert_not_called()
        client.delete_comment.assert_not_called()
        client.edit_comment.assert_not_called()

    def test_get_review_thread_gate_state_bypasses_validator_threads_when_disabled(self):
        client = GitHubClient(token="fake_token")
        validator_thread = _make_adversarial_thread()
        client.get_pr_review_threads_strict = MagicMock(return_value=[validator_thread])

        config_disabled = AutomationConfig(pr_adversarial_validation=False)
        state_disabled = _get_review_thread_gate_state(client, "owner/repo", 100, config=config_disabled)
        assert state_disabled.has_unresolved is False

        config_enabled = AutomationConfig(pr_adversarial_validation=True)
        state_enabled = _get_review_thread_gate_state(client, "owner/repo", 100, config=config_enabled)
        assert state_enabled.has_unresolved is True


# ---------------------------------------------------------------------------
# REQ-005 / AS-004: Human review thread remains independent and blocks
# ---------------------------------------------------------------------------


class TestAS004HumanReviewThreadRemainsBlocking:
    """REQ-005 / AS-004: Human review thread continues to block merge when validator is disabled."""

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._merge_pr")
    def test_human_thread_blocks_merge_even_when_adversarial_validation_disabled(
        self,
        mock_merge_pr,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        head_sha = "a" * 40
        pr_data = _pr_data(100, head_sha, linked_issue=42)

        client = GitHubClient(token="fake_token")
        human_thread = _make_human_thread(thread_id="thread-human-1")
        adv_thread = _make_adversarial_thread(thread_id="thread-adv-1")
        client.get_pr_review_threads_strict = MagicMock(return_value=[human_thread, adv_thread])
        client.get_pull_request = MagicMock(return_value={"head": {"sha": head_sha}})
        client.get_pull_request_metadata_strict = MagicMock(
            return_value={
                "number": 100,
                "head": {"ref": "feature-branch", "sha": head_sha},
                "body": "Fixes #42",
                "user": {"login": "some-author"},
                "state": "open",
            }
        )

        config = AutomationConfig(pr_adversarial_validation=False)
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_merge_pr.assert_not_called()
        assert any("Skipping merge for PR #100 due to unresolved review threads" in a for a in actions)


# ---------------------------------------------------------------------------
# REQ-005 / AS-005: Non-adversarial merge gates still apply
# ---------------------------------------------------------------------------


class TestAS005NonAdversarialMergeGatesStillApply:
    """REQ-005 / AS-005: Failing CI, unmergeable PR, and GitHub branch protections still block merge."""

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._merge_pr")
    def test_failing_ci_blocks_merge(
        self,
        mock_merge_pr,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        mock_checks.return_value = GitHubActionsStatusResult(success=False, ids=[1, 2])
        head_sha = "a" * 40
        pr_data = _pr_data(100, head_sha, linked_issue=42)

        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.get_pull_request.return_value = {"head": {"sha": head_sha}}

        config = AutomationConfig(pr_adversarial_validation=False)
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_merge_pr.assert_not_called()
        assert any("GitHub Actions checks failed" in a for a in actions)

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": False, "merge_state_status": "dirty"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._merge_pr")
    def test_unmergeable_conflict_blocks_merge(
        self,
        mock_merge_pr,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        head_sha = "a" * 40
        pr_data = _pr_data(100, head_sha, mergeable=False, linked_issue=42)

        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.get_pull_request.return_value = {"head": {"sha": head_sha}}

        config = AutomationConfig(pr_adversarial_validation=False)
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_merge_pr.assert_not_called()
        assert any("is not mergeable" in a for a in actions)

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor._merge_pr", return_value=False)
    def test_branch_protection_failure_reports_error(
        self,
        mock_merge_pr,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        head_sha = "a" * 40
        pr_data = _pr_data(100, head_sha, linked_issue=42)

        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.get_pull_request.return_value = {"head": {"sha": head_sha}}

        config = AutomationConfig(pr_adversarial_validation=False)
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        mock_merge_pr.assert_called_once()
        assert any("Failed to merge PR #100" in a for a in actions)


# ---------------------------------------------------------------------------
# REQ-007 / AS-006 / AS-007: Re-enabling behavior
# ---------------------------------------------------------------------------


class TestAS006AndAS007ReEnablingSemantics:
    """REQ-007 / AS-006 / AS-007: Re-enabling restores ordinary enabled semantics."""

    @patch("auto_coder.util.github_action._check_github_actions_status")
    def test_re_enabling_on_unchanged_head_re_engages_blocking_verdict(self, mock_check_actions, mock_github_client):
        """AS-006: Re-enabling on unchanged HEAD re-engages blocking verdict and deprioritizes to 1."""
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
        mock_check_actions.return_value = GitHubActionsStatusResult(success=True, ids=[])

        engine = AutomationEngine(mock_github_client)

        # 1. Disabled: Priority is 2
        engine.config = AutomationConfig(pr_adversarial_validation=False)
        candidates_disabled = engine._get_candidates("owner/repo", max_items=10)
        assert len(candidates_disabled) == 1
        assert candidates_disabled[0].priority == 2

        # 2. Re-enabled: Priority drops to 1
        engine.config = AutomationConfig(pr_adversarial_validation=True)
        candidates_enabled = engine._get_candidates("owner/repo", max_items=10)
        assert len(candidates_enabled) == 1
        assert candidates_enabled[0].priority == 1

    @patch("auto_coder.util.github_action._check_github_actions_status")
    def test_re_enabling_after_head_change_does_not_block_new_head(self, mock_check_actions, mock_github_client):
        """AS-007: When re-enabled, an older-HEAD verdict does not apply to a new HEAD."""
        old_sha = "a" * 40
        new_sha = "b" * 40
        pr_data = _pr_data(100, new_sha)
        mock_github_client.get_open_prs_json.return_value = [pr_data]
        mock_github_client.get_pr_details.return_value = pr_data
        mock_github_client.get_pr_comments.return_value = []
        mock_github_client.get_pr_commits.return_value = []
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False
        mock_github_client.get_issue.return_value = {"number": 42, "title": "Spec", "body": ""}
        # Review only on old_sha
        mock_github_client.get_pr_reviews_strict.return_value = [_adversarial_review(old_sha, "NEEDS_FIX")]
        mock_check_actions.return_value = GitHubActionsStatusResult(success=True, ids=[])

        engine = AutomationEngine(mock_github_client)
        engine.config = AutomationConfig(pr_adversarial_validation=True)

        candidates = engine._get_candidates("owner/repo", max_items=10)
        assert len(candidates) == 1
        assert candidates[0].priority == 2
