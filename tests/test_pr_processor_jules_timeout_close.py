"""Tests for closing Jules PRs that fail to pass CI within the configured timeout."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock, patch

from src.auto_coder.automation_config import AutomationConfig, StaleJulesPRResult
from src.auto_coder.pr_processor import _close_stale_jules_pr, _handle_pr_merge, _should_skip_waiting_for_jules, process_pull_request
from src.auto_coder.util.github_action import DetailedChecksResult, GitHubActionsStatusResult

JULES_PR_BODY = "Fixes the reported bug.\n\nSession ID: 901463134778726610\nhttps://jules.google.com/session/901463134778726610\n\nclose #4636"


def _jules_pr_data(hours_old: float) -> dict:
    """Build Jules PR data created ``hours_old`` hours ago."""
    created_at = (datetime.now(timezone.utc) - timedelta(hours=hours_old)).isoformat().replace("+00:00", "Z")
    return {
        "number": 4643,
        "title": "Fix flaky outline rendering",
        "body": JULES_PR_BODY,
        "created_at": created_at,
        "user": {"login": "google-labs-jules[bot]"},
        "head": {"ref": "jules-fix-4636"},
        "base": {"ref": "main"},
    }


class TestCloseStaleJulesPR:
    """Test cases for _close_stale_jules_pr."""

    @patch("src.auto_coder.pr_processor.increment_attempt")
    def test_closes_pr_and_increments_attempt_after_timeout(self, mock_increment):
        github_client = Mock()
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=13)
        checks = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=False)
        mock_increment.return_value = 3

        result = _close_stale_jules_pr(github_client, "owner/repo", pr_data, config, checks)
        actions = result.actions

        github_client.close_pr.assert_called_once()
        close_args = github_client.close_pr.call_args[0]
        assert close_args[0] == "owner/repo"
        assert close_args[1] == 4643
        assert "12 hours" in close_args[2]
        mock_increment.assert_called_once_with("owner/repo", 4636)
        assert any("Closed stale Jules PR #4643" in action for action in actions)
        assert any("Incremented attempt for issue #4636 to 3" in action for action in actions)

    @patch("src.auto_coder.pr_processor.increment_attempt")
    def test_keeps_pr_open_before_timeout(self, mock_increment):
        github_client = Mock()
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=11)
        checks = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=False)

        result = _close_stale_jules_pr(github_client, "owner/repo", pr_data, config, checks)
        actions = result.actions

        assert result.closed is False
        assert actions == []
        github_client.close_pr.assert_not_called()
        mock_increment.assert_not_called()

    @patch("src.auto_coder.pr_processor.increment_attempt")
    def test_keeps_pr_open_when_ci_passed(self, mock_increment):
        github_client = Mock()
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=48)
        checks = MagicMock(spec=GitHubActionsStatusResult, success=True, in_progress=False)

        result = _close_stale_jules_pr(github_client, "owner/repo", pr_data, config, checks)
        actions = result.actions

        assert result.closed is False
        assert actions == []
        github_client.close_pr.assert_not_called()
        mock_increment.assert_not_called()

    @patch("src.auto_coder.pr_processor.increment_attempt")
    def test_keeps_pr_open_while_ci_in_progress(self, mock_increment):
        github_client = Mock()
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=48)
        checks = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=True)

        result = _close_stale_jules_pr(github_client, "owner/repo", pr_data, config, checks)
        actions = result.actions

        assert result.closed is False
        assert actions == []
        github_client.close_pr.assert_not_called()
        mock_increment.assert_not_called()

    @patch("src.auto_coder.pr_processor.increment_attempt")
    def test_ignores_non_jules_pr(self, mock_increment):
        github_client = Mock()
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=48)
        pr_data["user"] = {"login": "human-dev"}
        pr_data["body"] = "A regular PR without a session reference"

        result = _close_stale_jules_pr(github_client, "owner/repo", pr_data, config, None)
        actions = result.actions

        assert result.closed is False
        assert actions == []
        github_client.close_pr.assert_not_called()
        mock_increment.assert_not_called()

    @patch("src.auto_coder.pr_processor.increment_attempt")
    def test_ignores_already_closed_pr(self, mock_increment):
        """A PR closed by an earlier run must not be closed (and counted) twice."""
        github_client = Mock()
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=30)
        pr_data["state"] = "closed"
        checks = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=False)

        result = _close_stale_jules_pr(github_client, "owner/repo", pr_data, config, checks)

        assert result.closed is False
        github_client.close_pr.assert_not_called()
        mock_increment.assert_not_called()

    @patch("src.auto_coder.pr_processor._resolve_jules_pr_issue_number")
    @patch("src.auto_coder.pr_processor.increment_attempt")
    def test_resolves_issue_when_body_has_no_link(self, mock_increment, mock_resolve):
        github_client = Mock()
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=13)
        pr_data["body"] = "Session ID: 901463134778726610"
        checks = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=False)
        mock_resolve.return_value = 4636
        mock_increment.return_value = 2

        result = _close_stale_jules_pr(github_client, "owner/repo", pr_data, config, checks)
        actions = result.actions

        mock_resolve.assert_called_once_with("owner/repo", pr_data, github_client)
        mock_increment.assert_called_once_with("owner/repo", 4636)
        assert any("Closed stale Jules PR #4643" in action for action in actions)


class TestHandlePrMergeJulesPR:
    """Test cases for _handle_pr_merge with Jules PRs."""

    @patch("src.auto_coder.pr_processor.cmd.run_command")
    @patch("src.auto_coder.pr_processor.increment_attempt")
    @patch("src.auto_coder.pr_processor._send_jules_error_feedback")
    @patch("src.auto_coder.pr_processor._fix_pr_issues_with_testing")
    @patch("src.auto_coder.pr_processor._checkout_pr_branch")
    @patch("src.auto_coder.pr_processor._check_github_actions_status")
    @patch("src.auto_coder.pr_processor.get_detailed_checks_from_history")
    @patch("src.auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress")
    @patch("src.auto_coder.pr_processor._get_mergeable_state")
    def test_stale_jules_pr_is_closed_instead_of_fixed_locally(
        self,
        mock_mergeable,
        mock_check_in_progress,
        mock_detailed_checks,
        mock_check_status,
        mock_checkout,
        mock_fix_issues,
        mock_send_feedback,
        mock_increment,
        mock_run_command,
    ):
        """A Jules PR older than the timeout is closed; no local fixes are applied."""
        from src.auto_coder.utils import CommandResult

        github_client = Mock()
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=13)

        mock_check_in_progress.return_value = True
        mock_mergeable.return_value = {"mergeable": True}
        mock_check_status.return_value = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=False, error=None, ids=[1])
        mock_detailed_checks.return_value = MagicMock(spec=DetailedChecksResult, success=False, failed_checks=[{"name": "test"}])
        mock_run_command.return_value = CommandResult(success=True, stdout="main", stderr="", returncode=0)
        mock_increment.return_value = 2

        actions = _handle_pr_merge(github_client, "owner/repo", pr_data, config, {})

        github_client.close_pr.assert_called_once()
        mock_increment.assert_called_once_with("owner/repo", 4636)
        mock_send_feedback.assert_not_called()
        mock_fix_issues.assert_not_called()
        mock_checkout.assert_not_called()
        assert any("Closed stale Jules PR #4643" in action for action in actions)

    @patch("src.auto_coder.pr_processor.cmd.run_command")
    @patch("src.auto_coder.pr_processor._send_jules_error_feedback")
    @patch("src.auto_coder.pr_processor._fix_pr_issues_with_testing")
    @patch("src.auto_coder.pr_processor._checkout_pr_branch")
    @patch("src.auto_coder.pr_processor._check_github_actions_status")
    @patch("src.auto_coder.pr_processor.get_detailed_checks_from_history")
    @patch("src.auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress")
    @patch("src.auto_coder.pr_processor._get_mergeable_state")
    def test_fresh_jules_pr_is_delegated_to_jules_regardless_of_failure_count(
        self,
        mock_mergeable,
        mock_check_in_progress,
        mock_detailed_checks,
        mock_check_status,
        mock_checkout,
        mock_fix_issues,
        mock_send_feedback,
        mock_run_command,
    ):
        """Repeated CI failures never trigger local auto-fix commits on a Jules PR."""
        from src.auto_coder.utils import CommandResult

        github_client = Mock()
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=1)

        mock_check_in_progress.return_value = True
        mock_mergeable.return_value = {"mergeable": True}
        mock_check_status.return_value = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=False, error=None, ids=[1])
        mock_detailed_checks.return_value = MagicMock(spec=DetailedChecksResult, success=False, failed_checks=[{"name": "test"}])
        mock_run_command.return_value = CommandResult(success=True, stdout="main", stderr="", returncode=0)

        # Many previous failure comments used to trigger the removed local fallback
        target_message = "🤖 Auto-Coder: CI checks failed. I've sent the error logs to the Jules session and requested a fix. Please wait for the updates."
        old_time = (datetime.now(timezone.utc) - timedelta(hours=240)).isoformat()
        github_client.get_pr_comments.return_value = [{"body": target_message, "created_at": old_time}] * 20

        actions = _handle_pr_merge(github_client, "owner/repo", pr_data, config, {})

        mock_send_feedback.assert_called_once()
        mock_fix_issues.assert_not_called()
        mock_checkout.assert_not_called()
        github_client.close_pr.assert_not_called()
        assert "Jules will handle fixing PR #4643, skipping local fixes" in actions[-1]


class TestStaleJulesPRWithAutoCoderLabel:
    """A stale Jules PR must be closed even when the @auto-coder label is still attached.

    The label stays on the PR from an earlier processing run, so every gate that skips
    labelled items has to be reached only after the staleness check.
    """

    @patch("src.auto_coder.pr_processor.increment_attempt")
    @patch("src.auto_coder.pr_processor._check_github_actions_status")
    def test_process_pull_request_closes_labelled_stale_jules_pr(self, mock_check_status, mock_increment):
        github_client = Mock()
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=30)
        pr_data["labels"] = [{"name": "@auto-coder"}]
        mock_check_status.return_value = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=False)
        mock_increment.return_value = 4

        result = process_pull_request(github_client, config, "owner/repo", pr_data)

        github_client.close_pr.assert_called_once()
        mock_increment.assert_called_once_with("owner/repo", 4636)
        assert any("Closed stale Jules PR #4643" in action for action in result.actions_taken)
        assert not any("already being processed" in action for action in result.actions_taken)

    @patch("src.auto_coder.pr_processor.increment_attempt")
    @patch("src.auto_coder.pr_processor._check_github_actions_status")
    def test_get_candidates_closes_labelled_stale_jules_pr(self, mock_check_status, mock_increment):
        from src.auto_coder.automation_engine import AutomationEngine

        github_client = Mock()
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=30)
        pr_data["labels"] = [{"name": "@auto-coder"}]
        pr_data["draft"] = False
        github_client.get_open_prs_json.return_value = [pr_data]
        github_client.get_open_issues.return_value = []
        github_client.get_open_issues_json.return_value = []
        mock_check_status.return_value = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=False)
        mock_increment.return_value = 4

        engine = AutomationEngine(github_client, config=config)
        with patch("src.auto_coder.util.github_action.preload_github_actions_status"):
            candidates = engine._get_candidates("owner/repo")

        github_client.close_pr.assert_called_once()
        mock_increment.assert_called_once_with("owner/repo", 4636)
        assert all(candidate.data.get("number") != 4643 for candidate in candidates)

    @patch("src.auto_coder.pr_processor.increment_attempt")
    @patch("src.auto_coder.pr_processor._check_github_actions_status")
    def test_single_candidate_closes_labelled_stale_jules_pr(self, mock_check_status, mock_increment):
        from src.auto_coder.automation_config import Candidate
        from src.auto_coder.automation_engine import AutomationEngine

        github_client = Mock()
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=30)
        pr_data["labels"] = [{"name": "@auto-coder"}]
        mock_check_status.return_value = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=False)
        mock_increment.return_value = 4

        engine = AutomationEngine(github_client, config=config)
        candidate = Candidate(type="pr", data=pr_data, priority=1)

        with patch("src.auto_coder.pr_processor.process_pull_request") as mock_process:
            result = engine._process_single_candidate_unified("owner/repo", candidate, config)

        github_client.close_pr.assert_called_once()
        mock_increment.assert_called_once_with("owner/repo", 4636)
        mock_process.assert_not_called()
        assert result.success is True
        assert any("Closed stale Jules PR #4643" in action for action in result.actions)


class TestUnlockAndRetryLinkedIssue:
    """Closing a stale Jules PR must hand the linked issue back for a new attempt.

    Jules mode keeps the @auto-coder label on the issue while its session works, so a
    dead session leaves the issue locked forever unless the label is released.
    """

    @patch("src.auto_coder.pr_processor.increment_attempt")
    def test_close_releases_issue_label_and_reports_issue(self, mock_increment):
        github_client = Mock()
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=13)
        checks = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=False)
        mock_increment.return_value = 2

        result = _close_stale_jules_pr(github_client, "owner/repo", pr_data, config, checks)

        github_client.remove_labels.assert_called_once_with("owner/repo", 4636, [config.AUTO_CODER_LABEL], item_type="issue")
        assert result.issue_numbers == [4636]
        assert any(f"Removed {config.AUTO_CODER_LABEL} label from issue #4636" in action for action in result.actions)

    @patch("src.auto_coder.pr_processor.increment_attempt")
    def test_close_keeps_issue_label_when_labels_disabled(self, mock_increment):
        github_client = Mock()
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        config.DISABLE_LABELS = True
        pr_data = _jules_pr_data(hours_old=13)
        checks = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=False)
        mock_increment.return_value = 2

        result = _close_stale_jules_pr(github_client, "owner/repo", pr_data, config, checks)

        github_client.remove_labels.assert_not_called()
        assert result.issue_numbers == [4636]

    @patch("src.auto_coder.pr_processor.increment_attempt")
    @patch("src.auto_coder.pr_processor._check_github_actions_status")
    def test_single_candidate_starts_new_attempt_on_issue(self, mock_check_status, mock_increment):
        from src.auto_coder.automation_config import Candidate
        from src.auto_coder.automation_engine import AutomationEngine

        github_client = Mock()
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=30)
        pr_data["labels"] = [{"name": "@auto-coder"}]
        issue_data = {"number": 4636, "title": "Reduce warm /demo load time", "labels": []}
        github_client.get_issue.return_value = issue_data
        github_client.get_issue_details.return_value = issue_data
        github_client.get_all_sub_issues.return_value = []
        mock_check_status.return_value = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=False)
        mock_increment.return_value = 4

        engine = AutomationEngine(github_client, config=config)
        candidate = Candidate(type="pr", data=pr_data, priority=1)

        with patch.object(AutomationEngine, "_take_issue_actions", return_value=["Created branch issue-4636/attempt-4"]) as mock_take_issue:
            result = engine._process_single_candidate_unified("owner/repo", candidate, config)

        mock_take_issue.assert_called_once()
        assert mock_take_issue.call_args[0][1] == issue_data
        assert any("Started a new attempt for issue #4636" in action for action in result.actions)
        assert any("Created branch issue-4636/attempt-4" in action for action in result.actions)

    @patch("src.auto_coder.pr_processor.increment_attempt")
    @patch("src.auto_coder.pr_processor._check_github_actions_status")
    def test_get_candidates_queues_unlocked_issue(self, mock_check_status, mock_increment):
        from src.auto_coder.automation_engine import AutomationEngine

        github_client = Mock()
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=30)
        pr_data["labels"] = [{"name": "@auto-coder"}]
        pr_data["draft"] = False
        issue_data = {"number": 4636, "title": "Reduce warm /demo load time", "labels": []}
        github_client.get_open_prs_json.return_value = [pr_data]
        github_client.get_open_issues_json.return_value = [issue_data]
        github_client.get_issue.return_value = issue_data
        github_client.get_issue_details.return_value = issue_data
        mock_check_status.return_value = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=False)
        mock_increment.return_value = 4

        engine = AutomationEngine(github_client, config=config)
        with patch("src.auto_coder.util.github_action.preload_github_actions_status"):
            candidates = engine._get_candidates("owner/repo")

        issue_candidates = [c for c in candidates if c.type == "issue" and c.data.get("number") == 4636]
        assert len(issue_candidates) == 1, "the unlocked issue must be queued exactly once"


class TestShouldSkipWaitingForJules:
    """Test cases for _should_skip_waiting_for_jules time-based behavior."""

    def _client_with_wait_comment(self, comment_age_hours: float) -> Mock:
        github_client = Mock()
        target_message = "🤖 Auto-Coder: CI checks failed. I've sent the error logs to the Jules session and requested a fix. Please wait for the updates."
        comment_time = (datetime.now(timezone.utc) - timedelta(hours=comment_age_hours)).isoformat()
        github_client.get_pr_comments.return_value = [{"body": target_message, "created_at": comment_time}]
        commit_time = (datetime.now(timezone.utc) - timedelta(hours=comment_age_hours + 1)).isoformat()
        github_client.get_pr_commits.return_value = [{"commit": {"committer": {"date": commit_time}}}]
        return github_client

    def test_returns_false_after_wait_timeout(self):
        config = AutomationConfig()
        config.JULES_WAIT_TIMEOUT_HOURS = 2
        github_client = self._client_with_wait_comment(comment_age_hours=3)

        assert _should_skip_waiting_for_jules(github_client, "owner/repo", {"number": 123}, config) is False

    def test_returns_true_within_wait_timeout(self):
        config = AutomationConfig()
        config.JULES_WAIT_TIMEOUT_HOURS = 2
        github_client = self._client_with_wait_comment(comment_age_hours=0.5)

        assert _should_skip_waiting_for_jules(github_client, "owner/repo", {"number": 123}, config) is True


class TestSingleTargetTypeDetection:
    """--only <number> must resolve issues that are not PRs.

    get_pull_request() returns an empty result instead of raising for an issue number,
    so the candidate builder has to verify the payload before treating it as a PR.
    """

    def test_auto_detection_falls_back_to_issue(self):
        from src.auto_coder.automation_engine import AutomationEngine

        github_client = Mock()
        config = AutomationConfig()
        issue_data = {"number": 4636, "title": "Reduce warm /demo load time", "labels": []}
        # 404 for a PR lookup surfaces as an empty payload, not an exception
        github_client.get_pull_request.return_value = {}
        github_client.get_pr_details.return_value = {}
        github_client.get_issue.return_value = issue_data
        github_client.get_issue_details.return_value = issue_data

        engine = AutomationEngine(github_client, config=config)
        candidate = engine._create_candidate_from_single("owner/repo", "auto", 4636)

        assert candidate is not None
        assert candidate.type == "issue"
        assert candidate.data["number"] == 4636

    def test_missing_pr_returns_no_candidate(self):
        from src.auto_coder.automation_engine import AutomationEngine

        github_client = Mock()
        config = AutomationConfig()
        github_client.get_pull_request.return_value = {}
        github_client.get_pr_details.return_value = {}

        engine = AutomationEngine(github_client, config=config)

        assert engine._create_candidate_from_single("owner/repo", "pr", 4636) is None


class TestLinkedPRSkipUsesOpenPRs:
    """Closed PRs stay in an issue timeline forever and must not hide the issue."""

    def _engine(self, github_client, config):
        from src.auto_coder.automation_engine import AutomationEngine

        return AutomationEngine(github_client, config=config)

    def _issue(self, linked_pr_numbers):
        return {
            "number": 4636,
            "title": "Reduce warm /demo load time",
            "labels": [],
            "created_at": "2026-08-02T06:44:45Z",
            "linked_pr_numbers": linked_pr_numbers,
            "has_linked_prs": bool(linked_pr_numbers),
        }

    def test_issue_with_only_closed_linked_pr_is_collected(self):
        github_client = Mock()
        config = AutomationConfig()
        github_client.get_open_prs_json.return_value = []
        github_client.get_open_issues_json.return_value = [self._issue([4643])]

        engine = self._engine(github_client, config)
        with patch("src.auto_coder.util.github_action.preload_github_actions_status"):
            candidates = engine._get_candidates("owner/repo")

        assert [c.data["number"] for c in candidates if c.type == "issue"] == [4636]

    def test_issue_with_open_linked_pr_is_skipped(self):
        github_client = Mock()
        config = AutomationConfig()
        open_pr = {"number": 4643, "title": "Fix", "labels": [], "draft": False, "created_at": "2026-08-02T16:22:10Z", "head": {"ref": "b", "sha": "s"}, "body": ""}
        github_client.get_open_prs_json.return_value = [open_pr]
        github_client.get_open_issues_json.return_value = [self._issue([4643])]

        engine = self._engine(github_client, config)
        with (
            patch("src.auto_coder.util.github_action.preload_github_actions_status"),
            patch("src.auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult()),
            patch("src.auto_coder.util.github_action.check_github_actions_and_exit_if_in_progress", return_value=False),
        ):
            candidates = engine._get_candidates("owner/repo")

        assert [c.data["number"] for c in candidates if c.type == "issue"] == []
