"""Tests for closing Jules PRs that fail to pass CI within the configured timeout."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock, patch

from auto_coder.ci_observation import CIConclusion, CIObservationSnapshot, ObservationAvailability, ObservationRequest, ObservationSubject, WorkflowExecutionIdentity, WorkflowObservation
from auto_coder.util.github_action import DetailedChecksResult, GitHubActionsStatusResult, is_ci_observation_recovered
from src.auto_coder.automation_config import AutomationConfig, StaleJulesPRResult
from src.auto_coder.pr_processor import _close_stale_jules_pr, _handle_pr_merge, _should_skip_waiting_for_jules, process_pull_request

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

    @patch("src.auto_coder.pr_processor._remove_reviewer_sessions_for_closed_pr")
    @patch("src.auto_coder.pr_processor.increment_attempt")
    def test_closes_pr_and_increments_attempt_after_timeout(self, mock_increment, mock_remove_sessions):
        github_client = Mock()
        github_client.get_pr_review_threads_strict.return_value = []
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=13)
        checks = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=False)
        mock_increment.return_value = 3

        result = _close_stale_jules_pr(github_client, "owner/repo", pr_data, config, checks)
        actions = result.actions

        github_client.close_pr.assert_called_once()
        mock_remove_sessions.assert_called_once_with("owner/repo", 4643)
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
        github_client.get_pr_review_threads_strict.return_value = []
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
        github_client.get_pr_review_threads_strict.return_value = []
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
        github_client.get_pr_review_threads_strict.return_value = []
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
        github_client.get_pr_review_threads_strict.return_value = []
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
    def test_ignores_claude_pr(self, mock_increment):
        """Claude PRs must not be closed by Jules staleness check, and attempt must not be incremented."""
        github_client = Mock()
        github_client.get_pr_review_threads_strict.return_value = []
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=48)
        pr_data["user"] = {"login": "claude[bot]"}
        pr_data["body"] = "Fixes issue\nclose #4636"
        checks = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=False)

        result = _close_stale_jules_pr(github_client, "owner/repo", pr_data, config, checks)

        assert result.closed is False
        assert result.actions == []
        github_client.close_pr.assert_not_called()
        mock_increment.assert_not_called()

    @patch("src.auto_coder.pr_processor.increment_attempt")
    def test_ignores_claude_routine_session_pr(self, mock_increment):
        """PRs with Claude routine session links must not be closed by Jules staleness check."""
        github_client = Mock()
        github_client.get_pr_review_threads_strict.return_value = []
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=48)
        pr_data["user"] = {"login": "human-dev"}
        pr_data["body"] = "Session URL: https://claude.ai/code/session_01HJKLMNOPQRSTUVWXYZ\nclose #4636"
        checks = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=False)

        result = _close_stale_jules_pr(github_client, "owner/repo", pr_data, config, checks)

        assert result.closed is False
        assert result.actions == []
        github_client.close_pr.assert_not_called()
        mock_increment.assert_not_called()

    @patch("src.auto_coder.pr_processor.increment_attempt")
    def test_ignores_already_closed_pr(self, mock_increment):
        """A PR closed by an earlier run must not be closed (and counted) twice."""
        github_client = Mock()
        github_client.get_pr_review_threads_strict.return_value = []
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
        github_client.get_pr_review_threads_strict.return_value = []
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
        github_client.get_pr_review_threads_strict.return_value = []
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
        github_client.get_pr_review_threads_strict.return_value = []
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
        github_client.get_pr_review_threads_strict.return_value = []
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
        github_client.get_pr_review_threads_strict.return_value = []
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
        github_client.get_pr_review_threads_strict.return_value = []
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
        github_client.get_pr_review_threads_strict.return_value = []
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=13)
        checks = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=False)
        mock_increment.return_value = 2

        result = _close_stale_jules_pr(github_client, "owner/repo", pr_data, config, checks)

        github_client.remove_labels.assert_not_called()
        assert result.issue_numbers == [4636]
        assert not any("Removed @auto-coder" in action for action in result.actions)

    @patch("src.auto_coder.pr_processor.increment_attempt")
    def test_close_keeps_issue_label_when_labels_disabled(self, mock_increment):
        github_client = Mock()
        github_client.get_pr_review_threads_strict.return_value = []
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        config.DISABLE_LABELS = True
        pr_data = _jules_pr_data(hours_old=13)
        checks = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=False)
        mock_increment.return_value = 2

        result = _close_stale_jules_pr(github_client, "owner/repo", pr_data, config, checks)

        github_client.remove_labels.assert_not_called()
        assert result.issue_numbers == [4636]

    def test_single_candidate_continuation_ignores_source_issue_processing_label(self, tmp_path):
        """The real stale-PR handoff admits identical source Issues with either label state."""
        from src.auto_coder.automation_config import Candidate
        from src.auto_coder.automation_engine import AutomationEngine
        from src.auto_coder.implementation_slots import ImplementationSlotRepository

        outcomes = []
        for name, source_labels in (("without", []), ("with", [{"name": "@auto-coder"}])):
            github_client = Mock()
            github_client.get_pr_review_threads_strict.return_value = []
            config = AutomationConfig()
            config.JULES_PR_CI_TIMEOUT_HOURS = 12
            pr_data = _jules_pr_data(hours_old=30)
            pr_data["labels"] = [{"name": "@auto-coder"}]
            issue_data = {
                "number": 4636,
                "title": "Reduce warm /demo load time",
                "labels": list(source_labels),
            }
            authoritative_issue = {
                "number": 4636,
                "title": issue_data["title"],
                "body": "",
                "labels": [{"name": "implementation-ready"}, *source_labels],
            }
            github_client.get_item_type_strict.return_value = "issue"
            github_client.get_issue_dispatch_snapshot_strict.return_value = authoritative_issue
            github_client.get_issue.return_value = issue_data
            github_client.get_issue_details.return_value = issue_data
            github_client.get_all_sub_issues.return_value = []

            engine = AutomationEngine(github_client, config=config)
            engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / name / "slots.json")
            candidate = Candidate(type="pr", data=pr_data, priority=1)

            with (
                patch(
                    "src.auto_coder.issue_dispatch.default_issue_dispatch_db_path",
                    return_value=tmp_path / name / "dispatch.sqlite3",
                ),
                patch(
                    "src.auto_coder.pr_processor._check_github_actions_status",
                    return_value=MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=False),
                ),
                patch("src.auto_coder.pr_processor.increment_attempt", return_value=4) as increment,
                patch(
                    "src.auto_coder.issue_processor._take_issue_actions",
                    return_value=["Created branch issue-4636/attempt-4"],
                ) as take_issue,
            ):
                result = engine._process_single_candidate_unified("owner/repo", candidate, config)

            take_issue.assert_called_once()
            assert take_issue.call_args.args[1] == issue_data
            increment.assert_called_once_with("owner/repo", 4636)
            github_client.remove_labels.assert_not_called()
            outcomes.append(
                (
                    result.actions,
                    engine.implementation_slots.active_owners(),
                    increment.call_args_list,
                )
            )

        assert outcomes[0] == outcomes[1]
        assert any("Started a new attempt for issue #4636" in action for action in outcomes[0][0])
        assert any("Created branch issue-4636/attempt-4" in action for action in outcomes[0][0])

    @patch("src.auto_coder.pr_processor.increment_attempt")
    @patch("src.auto_coder.pr_processor._check_github_actions_status")
    def test_get_candidates_queues_unlocked_issue(self, mock_check_status, mock_increment):
        from src.auto_coder.automation_engine import AutomationEngine

        github_client = Mock()
        github_client.get_pr_review_threads_strict.return_value = []
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


def _ci_observation(availability: ObservationAvailability, facts: tuple = (), *, unavailable_reason=None) -> CIObservationSnapshot:
    """Build a real CIObservationSnapshot for a fixed subject/request (Issue #2275)."""
    return CIObservationSnapshot(
        subject=ObservationSubject("https://api.github.com", "owner/repo", 123, "deadbeef" * 5),
        request=ObservationRequest("github-actions", "checks+workflows"),
        cycle_id="cycle-1",
        invalidation_epoch=0,
        availability=availability,
        facts=facts,
        unavailable_reason=unavailable_reason,
    )


def _success_workflow_fact() -> WorkflowObservation:
    return WorkflowObservation(execution=WorkflowExecutionIdentity("wf-1", "run-1", 1), conclusion=CIConclusion.SUCCESS)


def _pending_workflow_fact() -> WorkflowObservation:
    return WorkflowObservation(execution=WorkflowExecutionIdentity("wf-1", "run-1", 1), conclusion=CIConclusion.PENDING)


def _failing_workflow_fact() -> WorkflowObservation:
    return WorkflowObservation(execution=WorkflowExecutionIdentity("wf-1", "run-1", 1), conclusion=CIConclusion.FAILURE)


def recovered_ci_checks() -> GitHubActionsStatusResult:
    """A complete, KNOWN, all-success, non-pending, error-free CI observation (RECOVERED_CI)."""
    observation = _ci_observation(ObservationAvailability.KNOWN, (_success_workflow_fact(),))
    return GitHubActionsStatusResult(success=True, ids=[1], in_progress=False, error=None, observation=observation)


def failing_ci_checks() -> GitHubActionsStatusResult:
    observation = _ci_observation(ObservationAvailability.KNOWN, (_failing_workflow_fact(),))
    return GitHubActionsStatusResult(success=False, ids=[1], in_progress=False, error=None, observation=observation)


def pending_ci_checks() -> GitHubActionsStatusResult:
    observation = _ci_observation(ObservationAvailability.KNOWN, (_pending_workflow_fact(),))
    return GitHubActionsStatusResult(success=True, ids=[1], in_progress=True, error=None, observation=observation)


class TestIsCiObservationRecovered:
    """Unit coverage for the RECOVERED_CI contract (Issue #2275, REQ-001)."""

    def test_complete_known_success_is_recovered(self):
        assert is_ci_observation_recovered(recovered_ci_checks()) is True

    def test_none_is_not_recovered(self):
        assert is_ci_observation_recovered(None) is False

    def test_missing_observation_is_not_recovered(self):
        checks = GitHubActionsStatusResult(success=True, in_progress=False, error=None, observation=None)
        assert is_ci_observation_recovered(checks) is False

    def test_pending_is_not_recovered(self):
        assert is_ci_observation_recovered(pending_ci_checks()) is False

    def test_failing_is_not_recovered(self):
        assert is_ci_observation_recovered(failing_ci_checks()) is False

    def test_error_is_not_recovered_even_if_success_flag_true(self):
        observation = _ci_observation(ObservationAvailability.KNOWN, (_success_workflow_fact(),))
        checks = GitHubActionsStatusResult(success=True, in_progress=False, error="unexpected", observation=observation)
        assert is_ci_observation_recovered(checks) is False

    def test_known_empty_is_not_recovered(self):
        observation = _ci_observation(ObservationAvailability.KNOWN_EMPTY)
        checks = GitHubActionsStatusResult(success=False, in_progress=True, error="No current CI observations", observation=observation)
        assert is_ci_observation_recovered(checks) is False

    def test_partial_is_not_recovered(self):
        observation = _ci_observation(ObservationAvailability.PARTIAL, unavailable_reason="partial read")
        checks = GitHubActionsStatusResult(success=False, error="partial read", observation=observation)
        assert is_ci_observation_recovered(checks) is False

    def test_unavailable_is_not_recovered(self):
        observation = _ci_observation(ObservationAvailability.UNAVAILABLE, unavailable_reason="GitHub CI request failed (unavailable)")
        checks = GitHubActionsStatusResult(success=False, error="GitHub CI request failed (unavailable)", observation=observation)
        assert is_ci_observation_recovered(checks) is False

    def test_throttled_is_not_recovered(self):
        observation = _ci_observation(ObservationAvailability.THROTTLED, unavailable_reason="GitHub CI request failed (throttled)")
        checks = GitHubActionsStatusResult(success=False, error="GitHub CI request failed (throttled)", observation=observation)
        assert is_ci_observation_recovered(checks) is False

    def test_superseded_is_not_recovered(self):
        observation = _ci_observation(ObservationAvailability.SUPERSEDED, unavailable_reason="completion was fenced by newer observation state")
        checks = GitHubActionsStatusResult(success=False, error="completion was fenced by newer observation state", observation=observation)
        assert is_ci_observation_recovered(checks) is False

    def test_bare_default_success_without_observation_is_not_recovered(self):
        """A default-constructed ``GitHubActionsStatusResult(success=True)`` must never authorize recovery on its own."""
        assert is_ci_observation_recovered(GitHubActionsStatusResult()) is False


class TestShouldSkipWaitingForJules:
    """Test cases for _should_skip_waiting_for_jules time-based behavior."""

    def _client_with_wait_comment(self, comment_age_hours: float) -> Mock:
        github_client = Mock()
        github_client.get_pr_review_threads_strict.return_value = []
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

    def test_recovered_ci_releases_wait_within_timeout(self):
        """REQ-002: RECOVERED_CI makes the comment/timeout-based wait nonblocking."""
        config = AutomationConfig()
        config.JULES_WAIT_TIMEOUT_HOURS = 2
        github_client = self._client_with_wait_comment(comment_age_hours=0.5)

        assert _should_skip_waiting_for_jules(github_client, "owner/repo", {"number": 123}, config, github_checks=recovered_ci_checks()) is False
        # RECOVERED_CI short-circuits before any comment/commit evidence is consulted.
        github_client.get_pr_comments.assert_not_called()

    def test_pending_ci_does_not_release_wait(self):
        """REQ-004: CI that is still pending must not activate the RECOVERED_CI exception."""
        config = AutomationConfig()
        config.JULES_WAIT_TIMEOUT_HOURS = 2
        github_client = self._client_with_wait_comment(comment_age_hours=0.5)

        assert _should_skip_waiting_for_jules(github_client, "owner/repo", {"number": 123}, config, github_checks=pending_ci_checks()) is True

    def test_failing_ci_does_not_release_wait(self):
        """REQ-004: a complete current terminal CI failure remains eligible for the ordinary wait."""
        config = AutomationConfig()
        config.JULES_WAIT_TIMEOUT_HOURS = 2
        github_client = self._client_with_wait_comment(comment_age_hours=0.5)

        assert _should_skip_waiting_for_jules(github_client, "owner/repo", {"number": 123}, config, github_checks=failing_ci_checks()) is True

    @patch("auto_coder.jules_client.JulesClient")
    def test_recovered_ci_releases_wait_with_active_session(self, mock_jules_client_class):
        """REQ-002: an IN_PROGRESS Jules session alone must not veto progress once CI is recovered."""
        mock_jules_client = Mock()
        mock_jules_client.get_session.return_value = {"state": "IN_PROGRESS"}
        mock_jules_client_class.return_value = mock_jules_client
        config = AutomationConfig()
        pr_data = _jules_pr_data(hours_old=1)
        github_client = Mock()
        github_client.get_pr_review_threads_strict.return_value = []

        assert _should_skip_waiting_for_jules(github_client, "owner/repo", pr_data, config, github_checks=recovered_ci_checks()) is False

    @patch("auto_coder.jules_client.JulesClient")
    def test_active_session_still_waits_without_recovered_ci(self, mock_jules_client_class):
        """Positive control: session activity alone still gates progress when CI is not recovered."""
        mock_jules_client = Mock()
        mock_jules_client.get_session.return_value = {"state": "IN_PROGRESS"}
        mock_jules_client_class.return_value = mock_jules_client
        config = AutomationConfig()
        pr_data = _jules_pr_data(hours_old=1)
        github_client = Mock()
        github_client.get_pr_review_threads_strict.return_value = []

        assert _should_skip_waiting_for_jules(github_client, "owner/repo", pr_data, config) is True
        assert _should_skip_waiting_for_jules(github_client, "owner/repo", pr_data, config, github_checks=failing_ci_checks()) is True


class TestProcessPullRequestResumesOnRecoveredCI:
    """Production-path coverage: process_pull_request must itself obtain CI and share it with
    the legacy Jules wait gate, so a recovered head is not deferred purely on session activity
    (Issue #2275, REQ-002/REQ-005/REQ-007, AS-001/AS-005/AS-006).
    """

    @patch("src.auto_coder.pr_processor._process_pr_for_fixes")
    @patch("auto_coder.jules_client.JulesClient")
    @patch("src.auto_coder.pr_processor._check_github_actions_status")
    def test_active_session_no_longer_defers_processing_once_ci_recovers(self, mock_check_status, mock_jules_client_class, mock_process_fixes):
        mock_check_status.return_value = recovered_ci_checks()
        mock_jules_client = Mock()
        mock_jules_client.get_session.return_value = {"state": "IN_PROGRESS"}
        mock_jules_client_class.return_value = mock_jules_client

        from src.auto_coder.pr_processor import ProcessedPRResult

        mock_process_fixes.return_value = ProcessedPRResult(pr_data={}, actions_taken=["Processed normally"], priority="fix", analysis=None)

        github_client = Mock()
        github_client.get_pr_review_threads_strict.return_value = []
        config = AutomationConfig()
        pr_data = _jules_pr_data(hours_old=1)

        result = process_pull_request(github_client, config, "owner/repo", pr_data)

        assert not any("waiting for Jules" in action for action in result.actions_taken)
        mock_process_fixes.assert_called_once()

    @patch("src.auto_coder.pr_processor._process_pr_for_fixes")
    @patch("auto_coder.jules_client.JulesClient")
    @patch("src.auto_coder.pr_processor._check_github_actions_status")
    def test_active_session_still_defers_when_ci_is_still_failing(self, mock_check_status, mock_jules_client_class, mock_process_fixes):
        """Positive preservation control (AS-005): a still-failing head keeps the ordinary wait."""
        mock_check_status.return_value = failing_ci_checks()
        mock_jules_client = Mock()
        mock_jules_client.get_session.return_value = {"state": "IN_PROGRESS"}
        mock_jules_client_class.return_value = mock_jules_client

        github_client = Mock()
        github_client.get_pr_review_threads_strict.return_value = []
        config = AutomationConfig()
        pr_data = _jules_pr_data(hours_old=1)

        result = process_pull_request(github_client, config, "owner/repo", pr_data)

        assert any("waiting for Jules" in action for action in result.actions_taken)
        mock_process_fixes.assert_not_called()


class TestGetCandidatesResumesOnRecoveredCI:
    """The collector must release the same wait as the processor for a recovered head
    (Issue #2275, REQ-007, AS-004).
    """

    @patch("auto_coder.jules_client.JulesClient")
    @patch("src.auto_coder.util.github_action._check_github_actions_status")
    def test_recovered_ci_pr_is_not_excluded_at_collection(self, mock_check_status, mock_jules_client_class):
        from src.auto_coder.automation_engine import AutomationEngine

        mock_check_status.return_value = recovered_ci_checks()
        mock_jules_client = Mock()
        mock_jules_client.get_session.return_value = {"state": "IN_PROGRESS"}
        mock_jules_client_class.return_value = mock_jules_client

        github_client = Mock()
        github_client.get_pr_review_threads_strict.return_value = []
        config = AutomationConfig()
        pr_data = _jules_pr_data(hours_old=1)
        pr_data["draft"] = False
        pr_data["mergeable"] = True
        github_client.get_open_prs_json.return_value = [pr_data]
        github_client.get_open_issues.return_value = []
        github_client.get_open_issues_json.return_value = []

        engine = AutomationEngine(github_client, config=config)
        with patch("src.auto_coder.util.github_action.preload_github_actions_status"):
            candidates = engine._get_candidates("owner/repo")

        assert any(candidate.data.get("number") == pr_data["number"] for candidate in candidates)


class TestSingleTargetTypeDetection:
    """--only <number> must resolve issues that are not PRs.

    get_pull_request() returns an empty result instead of raising for an issue number,
    so the candidate builder has to verify the payload before treating it as a PR.
    """

    def test_auto_detection_falls_back_to_issue(self):
        from src.auto_coder.automation_engine import AutomationEngine

        github_client = Mock()
        github_client.get_pr_review_threads_strict.return_value = []
        config = AutomationConfig()
        issue_data = {"number": 4636, "title": "Reduce warm /demo load time", "labels": []}
        # 404 for a PR lookup surfaces as an empty payload, not an exception
        github_client.get_pull_request.return_value = {}
        github_client.get_pr_details.return_value = {}
        github_client.get_item_type_strict.return_value = "issue"
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
        github_client.get_pr_review_threads_strict.return_value = []
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
        github_client.get_pr_review_threads_strict.return_value = []
        config = AutomationConfig()
        github_client.get_open_prs_json.return_value = []
        github_client.get_open_issues_json.return_value = [self._issue([4643])]

        engine = self._engine(github_client, config)
        with patch("src.auto_coder.util.github_action.preload_github_actions_status"):
            candidates = engine._get_candidates("owner/repo")

        assert [c.data["number"] for c in candidates if c.type == "issue"] == [4636]

    def test_issue_with_open_linked_pr_is_skipped(self):
        github_client = Mock()
        github_client.get_pr_review_threads_strict.return_value = []
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
