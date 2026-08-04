"""
Tests for handing stale Jules issue sessions over to the backend_with_high_score backend.

A Jules session that works on an issue for longer than
``JULES_ISSUE_PR_TIMEOUT_HOURS`` without opening a PR is stopped and the issue is
implemented by the backend_with_high_score backend instead.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.issue_processor import handle_stale_jules_issue_sessions


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _session(session_id: str, age_hours: float, outputs=None) -> dict:
    created = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    return {
        "name": f"sessions/{session_id}",
        "state": "IN_PROGRESS",
        "createTime": _iso(created),
        "outputs": outputs if outputs is not None else {},
    }


def _github_client(issue_number: int = 42, state: str = "open", has_linked_pr: bool = False) -> MagicMock:
    client = MagicMock()
    client.get_issue.return_value = {"number": issue_number, "title": "Broken thing", "state": state}
    client.get_issue_details.return_value = {
        "number": issue_number,
        "title": "Broken thing",
        "body": "Please fix",
        "state": state,
        "labels": ["@auto-coder"],
        "author": "someone",
    }
    client.has_linked_pr.return_value = has_linked_pr
    return client


@pytest.fixture
def config() -> AutomationConfig:
    config = AutomationConfig()
    object.__setattr__(config, "JULES_ISSUE_PR_TIMEOUT_HOURS", 12)
    return config


class TestHandleStaleJulesIssueSessions:
    """Test cases for handle_stale_jules_issue_sessions."""

    def _run(self, sessions, github_client, config, issue_number=42, stopped=False, backend_manager=None):
        jules_client = MagicMock()
        jules_client.list_sessions.return_value = sessions

        cloud_manager = MagicMock()
        cloud_manager.get_issue_by_session.return_value = issue_number

        take_actions = MagicMock(return_value=["Implemented issue #42"])

        with (
            patch("src.auto_coder.issue_processor.JulesClient", return_value=jules_client),
            patch("src.auto_coder.issue_processor.CloudManager", return_value=cloud_manager),
            patch("src.auto_coder.issue_processor.is_session_stopped", return_value=stopped),
            patch("src.auto_coder.issue_processor.mark_session_stopped") as mark_stopped,
            patch("src.auto_coder.issue_processor._take_issue_actions", take_actions),
            patch("src.auto_coder.pr_processor._release_issue_processing_label", return_value=True),
            patch(
                "src.auto_coder.cli_helpers.create_high_score_backend_manager",
                return_value=backend_manager,
            ),
        ):
            result = handle_stale_jules_issue_sessions("owner/repo", config, github_client)

        return result, jules_client, take_actions, mark_stopped

    def test_stops_session_and_delegates_to_high_score_backend(self, config):
        """A session older than the timeout without a PR is stopped and handed over."""
        github_client = _github_client()
        backend_manager = MagicMock()

        result, jules_client, take_actions, mark_stopped = self._run(
            [_session("sess-1", age_hours=13)],
            github_client,
            config,
            backend_manager=backend_manager,
        )

        jules_client.send_message.assert_called_once_with("sess-1", "stop")
        mark_stopped.assert_called_once_with("sess-1")
        assert result.issue_numbers == [42]

        take_actions.assert_called_once()
        assert take_actions.call_args.kwargs["backend_manager"] is backend_manager
        assert take_actions.call_args.args[0] == "owner/repo"
        assert "Implemented issue #42" in result.actions

        # The issue gets a comment explaining the hand-over
        github_client.add_comment_to_issue.assert_called_once()
        comment = github_client.add_comment_to_issue.call_args.args[2]
        assert "12 hours" in comment
        assert "sess-1" in comment

    def test_session_within_timeout_is_left_alone(self, config):
        """A session that is still within the timeout keeps working on the issue."""
        github_client = _github_client()

        result, jules_client, take_actions, mark_stopped = self._run(
            [_session("sess-1", age_hours=11)],
            github_client,
            config,
        )

        jules_client.send_message.assert_not_called()
        take_actions.assert_not_called()
        mark_stopped.assert_not_called()
        assert result.issue_numbers == []
        assert result.actions == []

    def test_session_with_pull_request_is_left_alone(self, config):
        """A session that already produced a PR is never stopped."""
        github_client = _github_client()
        session = _session("sess-1", age_hours=48, outputs={"pullRequest": {"url": "https://github.com/owner/repo/pull/7"}})

        result, jules_client, take_actions, _ = self._run([session], github_client, config)

        jules_client.send_message.assert_not_called()
        take_actions.assert_not_called()
        assert result.issue_numbers == []

    def test_issue_with_linked_pr_is_left_alone(self, config):
        """A PR linked on GitHub counts even when session outputs do not show it yet."""
        github_client = _github_client(has_linked_pr=True)

        result, jules_client, take_actions, _ = self._run([_session("sess-1", age_hours=13)], github_client, config)

        jules_client.send_message.assert_not_called()
        take_actions.assert_not_called()
        assert result.issue_numbers == []

    def test_closed_issue_is_left_alone(self, config):
        """Closed issues are not re-implemented."""
        github_client = _github_client(state="closed")

        result, jules_client, take_actions, _ = self._run([_session("sess-1", age_hours=13)], github_client, config)

        jules_client.send_message.assert_not_called()
        take_actions.assert_not_called()
        assert result.issue_numbers == []

    def test_already_stopped_session_is_not_handled_twice(self, config):
        """A session that was already stopped is skipped on the next run."""
        github_client = _github_client()

        result, jules_client, take_actions, _ = self._run(
            [_session("sess-1", age_hours=30)],
            github_client,
            config,
            stopped=True,
        )

        jules_client.send_message.assert_not_called()
        take_actions.assert_not_called()
        assert result.issue_numbers == []

    def test_pull_request_number_in_cloud_csv_is_skipped(self, config):
        """cloud.csv also tracks PR sessions; those are not implemented as issues."""
        github_client = _github_client()
        github_client.get_issue.return_value = {
            "number": 42,
            "title": "A PR",
            "state": "open",
            "pull_request": {"url": "https://github.com/owner/repo/pull/42"},
        }

        result, jules_client, take_actions, _ = self._run([_session("sess-1", age_hours=13)], github_client, config)

        jules_client.send_message.assert_not_called()
        take_actions.assert_not_called()
        assert result.issue_numbers == []

    def test_failed_stop_message_does_not_delegate(self, config):
        """When Jules rejects the stop message the issue stays with Jules."""
        github_client = _github_client()
        jules_client = MagicMock()
        jules_client.list_sessions.return_value = [_session("sess-1", age_hours=13)]
        jules_client.send_message.side_effect = RuntimeError("HTTP 500")

        cloud_manager = MagicMock()
        cloud_manager.get_issue_by_session.return_value = 42

        take_actions = MagicMock(return_value=[])

        with (
            patch("src.auto_coder.issue_processor.JulesClient", return_value=jules_client),
            patch("src.auto_coder.issue_processor.CloudManager", return_value=cloud_manager),
            patch("src.auto_coder.issue_processor.is_session_stopped", return_value=False),
            patch("src.auto_coder.issue_processor.mark_session_stopped") as mark_stopped,
            patch("src.auto_coder.issue_processor._take_issue_actions", take_actions),
        ):
            result = handle_stale_jules_issue_sessions("owner/repo", config, github_client)

        mark_stopped.assert_not_called()
        take_actions.assert_not_called()
        assert result.issue_numbers == []

    def test_no_session_for_issue_is_skipped(self, config):
        """Sessions without a tracked issue number are ignored."""
        github_client = _github_client()

        result, jules_client, take_actions, _ = self._run(
            [_session("sess-1", age_hours=13)],
            github_client,
            config,
            issue_number=None,
        )

        jules_client.send_message.assert_not_called()
        take_actions.assert_not_called()
        assert result.issue_numbers == []


class TestAutomationEngineHook:
    """The engine exposes the stale-session hand-over as part of its loop."""

    def test_engine_delegates_and_returns_actions(self, config):
        from src.auto_coder.automation_config import StaleJulesIssueResult
        from src.auto_coder.automation_engine import AutomationEngine

        github_client = MagicMock()
        engine = AutomationEngine(github_client, config=config)

        stale_result = StaleJulesIssueResult(actions=["Stopped Jules session 's1' for issue #42"], issue_numbers=[42])

        with patch("src.auto_coder.issue_processor.handle_stale_jules_issue_sessions", return_value=stale_result) as mock_handle:
            actions = engine.handle_stale_jules_issue_sessions("owner/repo")

        mock_handle.assert_called_once_with("owner/repo", config, github_client)
        assert actions == stale_result.actions

    def test_engine_swallows_errors(self, config):
        from src.auto_coder.automation_engine import AutomationEngine

        engine = AutomationEngine(MagicMock(), config=config)

        with patch("src.auto_coder.issue_processor.handle_stale_jules_issue_sessions", side_effect=RuntimeError("boom")):
            assert engine.handle_stale_jules_issue_sessions("owner/repo") == []
