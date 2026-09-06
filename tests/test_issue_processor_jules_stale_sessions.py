"""
Tests for handing stale Jules issue sessions over to the backend_with_high_score backend.

A Jules session that works on an issue for longer than
``JULES_ISSUE_PR_TIMEOUT_HOURS`` without opening a PR is stopped and the issue is
implemented by the backend_with_high_score backend instead.
"""

import inspect
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from threading import Event
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
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
    client.get_item_type_strict.return_value = "issue"
    client.get_issue_dispatch_snapshot_strict.return_value = {
        "number": issue_number,
        "state": state,
        "labels": [{"name": "implementation-ready"}, {"name": "@auto-coder"}],
    }
    client.get_issue.return_value = {"number": issue_number, "title": "Broken thing", "state": state}
    client.get_issue_details.return_value = {
        "number": issue_number,
        "title": "Broken thing",
        "body": "Please fix",
        "state": state,
        "labels": ["@auto-coder"],
        "author": "someone",
    }
    client.get_direct_sub_issues_strict.return_value = []
    client.get_parent_issue_details_strict.return_value = None
    client.has_linked_pr.return_value = has_linked_pr
    return client


@pytest.fixture
def config() -> AutomationConfig:
    config = AutomationConfig()
    object.__setattr__(config, "JULES_ISSUE_PR_TIMEOUT_HOURS", 12)
    return config


class TestHandleStaleJulesIssueSessions:
    """Test cases for handle_stale_jules_issue_sessions."""

    def _run(self, sessions, github_client, config, issue_number=42, stopped=False, backend_manager=None, implementation_slots=None):
        if implementation_slots is None:
            implementation_slots = MagicMock()
            implementation_slots.serialize.return_value = nullcontext()
            implementation_slots.active_execution_ids.return_value = ()
            implementation_slots.start_execution.return_value = "replacement-execution"
        jules_client = MagicMock()
        jules_client.get_session.return_value = {"state": "COMPLETED"}
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
            patch("src.auto_coder.issue_processor.increment_attempt", return_value=3) as increment,
            patch(
                "src.auto_coder.cli_helpers.create_high_score_backend_manager",
                return_value=backend_manager,
            ),
        ):
            result = handle_stale_jules_issue_sessions(
                "owner/repo",
                config,
                github_client,
                implementation_slots=implementation_slots,
                authorize_dispatch=lambda _repo, _number, snapshot: snapshot,
            )

        return result, jules_client, take_actions, mark_stopped, increment

    def test_fallback_serializes_with_linked_pr_owner(self, config, tmp_path):
        """A stale fallback cannot mutate while its Issue-rooted PR owns the lock."""
        slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
        owner = ImplementationOwner("issue", 42)
        started = Event()

        def run_fallback():
            started.set()
            return self._run(
                [_session("sess-1", age_hours=13)],
                _github_client(),
                config,
                implementation_slots=slots,
            )

        with ThreadPoolExecutor(max_workers=1) as executor:
            with slots.serialize(owner):
                future = executor.submit(run_fallback)
                assert started.wait(timeout=1)
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.1)

            result, *_ = future.result(timeout=2)

        assert result.issue_numbers == [42]

    def test_stops_session_and_delegates_to_high_score_backend(self, config):
        """A session older than the timeout without a PR is stopped and handed over."""
        github_client = _github_client()
        backend_manager = MagicMock()

        result, jules_client, take_actions, mark_stopped, increment = self._run(
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

        # The abandoned Jules run counts as a failed attempt
        increment.assert_called_once_with("owner/repo", 42)
        assert "Incremented attempt for issue #42 to 3" in result.actions

        # Durable ownership, rather than a GitHub label override, admits fallback work.
        assert "check_labels" not in take_actions.call_args.kwargs
        github_client.remove_labels.assert_not_called()

        # The issue gets a comment explaining the hand-over
        github_client.add_comment_to_issue.assert_called_once()
        comment = github_client.add_comment_to_issue.call_args.args[2]
        assert "12 hours" in comment
        assert "sess-1" in comment

    def test_unready_stale_session_retains_existing_implementation_without_handoff(self, config):
        """Timeout handling must not mutate ownership or dispatch replacement work."""
        github_client = _github_client()
        github_client.get_issue_dispatch_snapshot_strict.return_value = {
            "number": 42,
            "state": "open",
            "labels": [{"name": "@auto-coder"}],
        }

        result, jules_client, take_actions, mark_stopped, increment = self._run(
            [_session("sess-1", age_hours=13)],
            github_client,
            config,
        )

        github_client.get_issue_dispatch_snapshot_strict.assert_called_once_with("owner/repo", 42)
        jules_client.send_message.assert_not_called()
        mark_stopped.assert_not_called()
        increment.assert_not_called()
        take_actions.assert_not_called()
        github_client.add_comment_to_issue.assert_not_called()
        github_client.remove_labels.assert_not_called()
        assert result.issue_numbers == []
        assert result.actions == []

    def test_session_within_timeout_is_left_alone(self, config):
        """A session that is still within the timeout keeps working on the issue."""
        github_client = _github_client()

        result, jules_client, take_actions, mark_stopped, increment = self._run(
            [_session("sess-1", age_hours=11)],
            github_client,
            config,
        )

        jules_client.send_message.assert_not_called()
        take_actions.assert_not_called()
        mark_stopped.assert_not_called()
        increment.assert_not_called()
        assert result.issue_numbers == []
        assert result.actions == []

    def test_session_with_pull_request_is_left_alone(self, config):
        """A session that already produced a PR is never stopped."""
        github_client = _github_client()
        session = _session("sess-1", age_hours=48, outputs={"pullRequest": {"url": "https://github.com/owner/repo/pull/7"}})

        result, jules_client, take_actions, _, increment = self._run([session], github_client, config)

        jules_client.send_message.assert_not_called()
        take_actions.assert_not_called()
        increment.assert_not_called()
        assert result.issue_numbers == []

    def test_issue_with_linked_pr_is_left_alone(self, config):
        """A PR linked on GitHub counts even when session outputs do not show it yet."""
        github_client = _github_client(has_linked_pr=True)

        result, jules_client, take_actions, _, increment = self._run([_session("sess-1", age_hours=13)], github_client, config)

        jules_client.send_message.assert_not_called()
        take_actions.assert_not_called()
        increment.assert_not_called()
        assert result.issue_numbers == []

    def test_closed_issue_is_left_alone(self, config):
        """Closed issues are not re-implemented."""
        github_client = _github_client(state="closed")

        result, jules_client, take_actions, _, increment = self._run([_session("sess-1", age_hours=13)], github_client, config)

        jules_client.send_message.assert_not_called()
        take_actions.assert_not_called()
        increment.assert_not_called()
        assert result.issue_numbers == []

    def test_already_stopped_session_is_not_handled_twice(self, config):
        """A session that was already stopped is skipped on the next run."""
        github_client = _github_client()

        result, jules_client, take_actions, _, increment = self._run(
            [_session("sess-1", age_hours=30)],
            github_client,
            config,
            stopped=True,
        )

        jules_client.send_message.assert_not_called()
        take_actions.assert_not_called()
        increment.assert_not_called()
        assert result.issue_numbers == []

    def test_pull_request_number_in_cloud_csv_is_skipped(self, config):
        """cloud.csv also tracks PR sessions; those are not implemented as issues."""
        github_client = _github_client()
        github_client.get_item_type_strict.return_value = "pr"

        result, jules_client, take_actions, _, increment = self._run([_session("sess-1", age_hours=13)], github_client, config)

        jules_client.send_message.assert_not_called()
        take_actions.assert_not_called()
        increment.assert_not_called()
        assert result.issue_numbers == []

    def test_stale_session_mapped_to_a_pr_number_is_rejected(self, config):
        """A cloud.csv/session mapping to an actual PR must never reach Issue dispatch.

        Simulates stale or corrupt lifecycle state (or a cached Issue response) that
        maps a Jules session to a number GitHub's *authoritative* state identifies as
        a pull request. The cached get_issue() still looks like an ordinary open
        Issue, but get_item_type_strict() (the cache-bypassing authoritative lookup)
        reports a PR, and that must win: no stop message, no attempt increment, no
        implementation backend invocation, and no task-start/hand-over comment.
        """

        class GitHubStub:
            def get_issue(self, repo_name, issue_number):
                # Stale cached response: still looks like an ordinary open Issue.
                return {"number": issue_number, "title": "Actually a PR", "state": "open"}

            def get_issue_details(self, issue):
                return {"number": issue["number"], "title": issue["title"], "state": issue["state"], "body": "", "labels": [], "author": "someone"}

            def get_item_type_strict(self, repo_name, item_number):
                assert (repo_name, item_number) == ("owner/repo", 5266)
                return "pr"

            def has_linked_pr(self, repo_name, issue_number):
                raise AssertionError("Must not evaluate downstream Issue state after PR rejection")

            def add_comment_to_issue(self, *args, **kwargs):
                raise AssertionError("Must not comment on a rejected target")

            def remove_labels(self, *args, **kwargs):
                raise AssertionError("Must not touch labels on a rejected target")

        github_client = GitHubStub()

        result, jules_client, take_actions, mark_stopped, increment = self._run(
            [_session("sess-1", age_hours=13)],
            github_client,
            config,
            issue_number=5266,
        )

        jules_client.send_message.assert_not_called()
        mark_stopped.assert_not_called()
        take_actions.assert_not_called()
        increment.assert_not_called()
        assert result.issue_numbers == []
        assert result.actions == []

    def test_stale_session_is_rejected_when_no_authoritative_lookup_is_available(self, config):
        """A client that cannot perform the cache-bypassing lookup must fail closed.

        get_issue() returns a stale, issue-shaped response, but the client has no
        get_item_type_strict method at all -- so the type has not been established.
        This must be treated the same as an authoritative "pr" result, not as
        confirmation the target is an Issue: no stop message, no attempt increment,
        no implementation backend invocation, and no hand-over comment.
        """

        class GitHubStubWithoutStrictLookup:
            def get_issue(self, repo_name, issue_number):
                return {"number": issue_number, "title": "Stale cached issue data", "state": "open"}

            def get_issue_details(self, issue):
                raise AssertionError("Must not hydrate issue data before the type is established")

            def has_linked_pr(self, repo_name, issue_number):
                raise AssertionError("Must not evaluate downstream Issue state without an authoritative type")

            def add_comment_to_issue(self, *args, **kwargs):
                raise AssertionError("Must not comment on an unresolved target")

            def remove_labels(self, *args, **kwargs):
                raise AssertionError("Must not touch labels on an unresolved target")

        github_client = GitHubStubWithoutStrictLookup()

        result, jules_client, take_actions, mark_stopped, increment = self._run(
            [_session("sess-1", age_hours=13)],
            github_client,
            config,
            issue_number=5266,
        )

        jules_client.send_message.assert_not_called()
        mark_stopped.assert_not_called()
        take_actions.assert_not_called()
        increment.assert_not_called()
        assert result.issue_numbers == []
        assert result.actions == []

    def test_failed_stop_message_does_not_delegate(self, config):
        """When Jules rejects the stop message the issue stays with Jules."""
        github_client = _github_client()
        jules_client = MagicMock()
        jules_client.get_session.return_value = {"state": "COMPLETED"}
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
            patch("src.auto_coder.issue_processor.increment_attempt") as increment,
        ):
            result = handle_stale_jules_issue_sessions("owner/repo", config, github_client, authorize_dispatch=lambda _repo, _number, snapshot: snapshot)

        mark_stopped.assert_not_called()
        take_actions.assert_not_called()
        increment.assert_not_called()
        assert result.issue_numbers == []

    def test_no_session_for_issue_is_skipped(self, config):
        """Sessions without a tracked issue number are ignored."""
        github_client = _github_client()

        result, jules_client, take_actions, _, increment = self._run(
            [_session("sess-1", age_hours=13)],
            github_client,
            config,
            issue_number=None,
        )

        jules_client.send_message.assert_not_called()
        take_actions.assert_not_called()
        increment.assert_not_called()
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

        mock_handle.assert_called_once_with(
            "owner/repo",
            config,
            github_client,
            implementation_slots=engine._get_implementation_slots("owner/repo"),
            authorize_dispatch=engine._authorize_stale_jules_dispatch,
        )
        assert actions == stale_result.actions

    def test_engine_swallows_errors(self, config):
        from src.auto_coder.automation_engine import AutomationEngine

        engine = AutomationEngine(MagicMock(), config=config)

        with patch("src.auto_coder.issue_processor.handle_stale_jules_issue_sessions", side_effect=RuntimeError("boom")):
            assert engine.handle_stale_jules_issue_sessions("owner/repo") == []


class TestRetiredLabelGate:
    """The fallback run keeps the label it inherited and is not blocked by it."""

    def test_processing_scope_has_no_label_gate_configuration(self):
        """The production origin cannot pass a retired label-gate override."""
        from src.auto_coder.issue_processor import _apply_issue_actions_directly

        assert "check_labels" not in inspect.signature(_apply_issue_actions_directly).parameters


def test_daemon_stale_session_stops_old_generation_but_starts_no_replacement_on_error(config, tmp_path):
    """The daemon retires remote ownership before ERROR and starts no replacement."""
    from src.auto_coder.automation_engine import AutomationEngine
    from src.auto_coder.specification_analyzer import SpecificationAnalysisResult
    from src.auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle

    github = _github_client()
    body = "## Requirements\n- REQ-001: Implement revised behavior."
    github.get_issue_dispatch_snapshot_strict.return_value = {
        "number": 42,
        "title": "Revised title",
        "body": body,
        "state": "open",
        "labels": [{"name": "implementation-ready"}, {"name": "@auto-coder"}],
    }
    engine = AutomationEngine(github, config=config)
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine._specification_validators["owner/repo"] = SpecificationValidationLifecycle(
        "owner/repo",
        "validator",
        tmp_path / "validations.json",
        lambda _manifest, _body: SpecificationAnalysisResult("ERROR", error="provider unavailable"),
    )
    jules = MagicMock()
    jules.get_session.return_value = {"state": "COMPLETED"}
    jules.list_sessions.return_value = [_session("sess-1", age_hours=13)]
    cloud = MagicMock()
    cloud.get_issue_by_session.return_value = 42

    with (
        patch("src.auto_coder.issue_processor.JulesClient", return_value=jules),
        patch("src.auto_coder.issue_processor.CloudManager", return_value=cloud),
        patch("src.auto_coder.issue_processor.is_session_stopped", return_value=False),
        patch("src.auto_coder.issue_processor.increment_attempt") as increment,
        patch("src.auto_coder.issue_processor._take_issue_actions") as dispatch,
    ):
        assert engine.handle_stale_jules_issue_sessions("owner/repo") == []

    jules.send_message.assert_called_once_with("sess-1", "stop")
    increment.assert_not_called()
    dispatch.assert_not_called()
    assert engine.implementation_slots.active_owners() == ()


def test_ready_stale_replacement_reenters_normal_capacity_after_validation(config, tmp_path):
    """A READY daemon handoff cannot dispatch when another Issue takes released capacity."""
    from src.auto_coder.automation_engine import AutomationEngine
    from src.auto_coder.specification_analyzer import SpecificationAnalysisResult
    from src.auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle

    github = _github_client()
    body = "## Requirements\n- REQ-001: Implement replacement."
    current = {"number": 42, "title": "Replacement", "body": body, "state": "open", "labels": [{"name": "implementation-ready"}]}
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda *_args: dict(current)
    engine = AutomationEngine(github, config=config)
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine.implementation_slots = slots
    analysis_started = Event()
    analysis_release = Event()

    def analyze(_manifest, _body):
        analysis_started.set()
        assert analysis_release.wait(5)
        return SpecificationAnalysisResult("READY")

    engine._specification_validators["owner/repo"] = SpecificationValidationLifecycle("owner/repo", "validator", tmp_path / "validations.json", analyze)
    jules = MagicMock()
    jules.get_session.return_value = {"state": "COMPLETED"}
    jules.list_sessions.return_value = [_session("sess-1", age_hours=13)]
    cloud = MagicMock()
    cloud.get_issue_by_session.return_value = 42
    with (
        patch("src.auto_coder.issue_processor.JulesClient", return_value=jules),
        patch("src.auto_coder.issue_processor.CloudManager", return_value=cloud),
        patch("src.auto_coder.issue_processor.is_session_stopped", return_value=False),
        patch("src.auto_coder.issue_processor.increment_attempt") as increment,
        patch("src.auto_coder.issue_processor._take_issue_actions") as dispatch,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        run = pool.submit(engine.handle_stale_jules_issue_sessions, "owner/repo")
        assert analysis_started.wait(5)
        assert slots.start_execution(ImplementationOwner("issue", 99)) is not None
        analysis_release.set()
        run.result(timeout=5)
    increment.assert_not_called()
    dispatch.assert_not_called()
    assert slots.active_owners() == (ImplementationOwner("issue", 99),)


def test_stale_replacement_rechecks_after_link_lookup_before_ownership(config, tmp_path):
    """Withdrawal during post-validation I/O invalidates READY before replacement mutation."""
    from src.auto_coder.automation_engine import AutomationEngine

    github = _github_client()
    body = "## Requirements\n- REQ-001: Implement replacement."
    current = {"number": 42, "title": "Replacement", "body": body, "state": "open", "labels": [{"name": "implementation-ready"}]}
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda *_args: dict(current)

    def withdraw(*_args):
        current["body"] = body + "\nEdited"
        current["labels"] = []
        return False

    github.has_linked_pr.side_effect = withdraw
    engine = AutomationEngine(github, config=config)
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    jules = MagicMock()
    jules.get_session.return_value = {"state": "COMPLETED"}
    jules.list_sessions.return_value = [_session("sess-1", age_hours=13)]
    cloud = MagicMock()
    cloud.get_issue_by_session.return_value = 42
    with (
        patch("src.auto_coder.issue_processor.JulesClient", return_value=jules),
        patch("src.auto_coder.issue_processor.CloudManager", return_value=cloud),
        patch("src.auto_coder.issue_processor.is_session_stopped", return_value=False),
        patch("src.auto_coder.issue_processor.increment_attempt") as increment,
        patch("src.auto_coder.issue_processor._take_issue_actions") as dispatch,
    ):
        engine.handle_stale_jules_issue_sessions("owner/repo")
    jules.send_message.assert_called_once_with("sess-1", "stop")
    increment.assert_not_called()
    dispatch.assert_not_called()


def test_stale_replacement_rechecks_after_stop_io_before_ownership(config, tmp_path):
    """Withdrawal during the real stop request invalidates READY before admission."""
    from src.auto_coder.automation_engine import AutomationEngine

    github = _github_client()
    body = "## Requirements\n- REQ-001: Implement replacement."
    current = {"number": 42, "title": "Replacement", "body": body, "state": "open", "labels": [{"name": "implementation-ready"}]}
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda *_args: dict(current)
    engine = AutomationEngine(github, config=config)
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine.implementation_slots = slots
    jules = MagicMock()
    jules.get_session.return_value = {"state": "COMPLETED"}
    jules.list_sessions.return_value = [_session("sess-1", age_hours=13)]

    def withdraw_during_stop(*_args):
        current["body"] = body + "\nEdited during stop"
        current["labels"] = []

    jules.send_message.side_effect = withdraw_during_stop
    cloud = MagicMock()
    cloud.get_issue_by_session.return_value = 42
    with (
        patch("src.auto_coder.issue_processor.JulesClient", return_value=jules),
        patch("src.auto_coder.issue_processor.CloudManager", return_value=cloud),
        patch("src.auto_coder.issue_processor.is_session_stopped", return_value=False),
        patch("src.auto_coder.issue_processor.increment_attempt") as increment,
        patch("src.auto_coder.issue_processor._take_issue_actions") as dispatch,
    ):
        engine.handle_stale_jules_issue_sessions("owner/repo")
    jules.send_message.assert_called_once_with("sess-1", "stop")
    increment.assert_not_called()
    dispatch.assert_not_called()
    assert slots.active_owners() == ()
