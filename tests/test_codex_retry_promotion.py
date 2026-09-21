from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, patch

from auto_coder.automation_config import AutomationConfig
from auto_coder.cloud_manager import CloudManager, CloudTaskBinding
from auto_coder.cloud_run import CloudRun, CloudRunRepository
from auto_coder.codex_cloud_client import CodexSubmissionOutcome, CodexSubmissionResult
from auto_coder.issue_processor import _acknowledge_retry_projection, _process_issue_codex_cloud_mode
from auto_coder.issue_stage_routing import ImplementationRetryRequest, IssueStageRoutingStore
from auto_coder.retry_dispatch import RetryDispatchRepository


def _authority(request_id: str = "request-1", attempt_id: str = "logical-1") -> ImplementationRetryRequest:
    del attempt_id
    routing = IssueStageRoutingStore(Path.home() / ".auto-coder" / "issue-stage-routing.sqlite3")
    existing = routing.retry_request(request_id)
    if existing is not None:
        return existing
    routing.accept_retry_request(request_id, "owner/repo", 2223, "generation-7")
    predecessor = CloudManager("owner/repo").read_bindings_strict().get("2223")
    routing.capture_retry_predecessor(
        request_id,
        predecessor.provider if predecessor else None,
        predecessor.task_id if predecessor else None,
        predecessor.backend_name if predecessor else None,
    )
    return routing.mark_retry_owned(request_id, f"execution-{request_id}")


def _dispatch(authority: ImplementationRetryRequest, backend: str = "codex-alias", branch: str = "main") -> list[str]:
    config = AutomationConfig()
    config.MAIN_BRANCH = branch
    with (
        patch("auto_coder.issue_processor.get_current_attempt", return_value=4),
        patch("auto_coder.issue_processor.increment_attempt", return_value=5),
        patch("auto_coder.issue_processor.get_commit_log", return_value=""),
    ):
        return _process_issue_codex_cloud_mode(
            "owner/repo",
            {"number": 2223, "title": "Retry safely", "body": "", "labels": []},
            config,
            MagicMock(),
            backend,
            retry_authority=authority,
        )


@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_owned_retry_promotes_attributed_predecessor_and_replay_preserves_run(mock_client_type, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    manager = CloudManager("owner/repo")
    assert manager.add_session(2223, "jules-old", provider="jules", backend_name="jules")
    client = mock_client_type.return_value
    client.environment_id = "environment-original"
    client.submit_task.return_value = CodexSubmissionResult(
        CodexSubmissionOutcome.ACCEPTED,
        "task-new",
        "https://example.test/tasks/task-new",
    )

    first = _dispatch(_authority())
    run_repo = CloudRunRepository("owner/repo")
    run_repo.add_pull_request(2223, 5, 91)
    replay = _dispatch(_authority(), backend="codex-alias", branch="changed-after-acceptance")

    client.submit_task.assert_called_once()
    assert first == ["Started Codex Cloud task 'task-new' for issue #2223"]
    assert "already accepted" in replay[0]
    assert manager.read_bindings_strict()["2223"] == CloudTaskBinding("codex-cloud", "task-new", "codex-alias")
    handoff = RetryDispatchRepository("owner/repo").get("request-1")
    assert handoff is not None
    assert (handoff.predecessor_provider, handoff.predecessor_task_id) == ("jules", "jules-old")
    assert handoff.projection_disposition == "accepted-current"
    run = run_repo.get(2223, 5)
    assert run is not None
    assert (run.environment_id, run.base_branch, run.task_url, run.pull_request_numbers) == (
        "environment-original",
        "main",
        "https://example.test/tasks/task-new",
        [91],
    )


def test_older_accepted_retry_is_retained_as_historical_when_later_receipt_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    manager = CloudManager("owner/repo")
    old = CloudTaskBinding("jules", "jules-old", "jules")
    assert manager.add_session(2223, old.task_id, provider=old.provider, backend_name=old.backend_name)
    store = RetryDispatchRepository("owner/repo")
    for authority, task in ((_authority("request-1", "logical-1"), "task-1"), (_authority("request-2", "logical-2"), "task-2")):
        store.claim(authority, "codex-cloud", "codex", {"base_branch": "main"}, predecessor=(old.provider, old.task_id, old.backend_name))
        store.record_outcome(authority.request_id, "accepted", external_id=task)

    disposition = manager.promote_retry_binding(
        2223,
        CloudTaskBinding("codex-cloud", "task-1", "codex"),
        old,
        lambda: store.is_latest_accepted("request-1"),
    )
    store.mark_historical("request-1", "later receipt")

    assert disposition == "historical"
    assert manager.read_bindings_strict()["2223"] == old
    assert store.get("request-1").projection_disposition == "accepted-historical"


@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_unrecognized_conflict_retains_accepted_receipt_as_incomplete(mock_client_type, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    client = mock_client_type.return_value
    client.environment_id = "env"
    client.submit_task.return_value = CodexSubmissionResult(CodexSubmissionOutcome.ACCEPTED, "task-new")
    manager = CloudManager("owner/repo")
    assert manager.add_session(2223, "known-old", provider="jules", backend_name="jules")

    original_promote = CloudManager.promote_retry_binding

    def replace_before_promotion(self, issue_number, binding, predecessor, may_promote, *args):
        self.add_session(issue_number, "unknown-racer", provider="claude-routine", backend_name="claude")
        return original_promote(self, issue_number, binding, predecessor, may_promote, *args)

    with patch.object(CloudManager, "promote_retry_binding", replace_before_promotion):
        result = _dispatch(_authority())

    assert "tracking is incomplete" in result[0]
    handoff = RetryDispatchRepository("owner/repo").get("request-1")
    assert handoff is not None
    assert handoff.outcome == "accepted"
    assert handoff.external_id == "task-new"
    assert handoff.projection_disposition == "accepted-tracking-incomplete"
    assert manager.read_bindings_strict()["2223"].task_id == "unknown-racer"


@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_absent_run_without_retained_environment_stays_incomplete(mock_client_type, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    manager = CloudManager("owner/repo")
    predecessor = CloudTaskBinding("jules", "jules-old", "jules")
    assert manager.add_session(
        2223,
        predecessor.task_id,
        provider=predecessor.provider,
        backend_name=predecessor.backend_name,
    )
    authority = _authority()
    store = RetryDispatchRepository("owner/repo")
    store.claim(
        authority,
        "codex-cloud",
        "codex-alias",
        {"base_branch": "main"},
        predecessor=(predecessor.provider, predecessor.task_id, predecessor.backend_name),
    )
    store.allocate_numeric_attempt(authority.request_id, [4])
    store.record_outcome(
        authority.request_id,
        "accepted",
        external_id="task-retained",
        external_url="https://example.test/tasks/task-retained",
    )

    result = _dispatch(authority)

    mock_client_type.assert_not_called()
    assert result == ["Accepted Codex Cloud task 'task-retained' for issue #2223, but tracking is incomplete: " "Accepted cloud run provenance is incomplete"]
    assert CloudRunRepository("owner/repo").get(2223, 5) is None
    assert manager.read_bindings_strict()["2223"] == predecessor
    retained = RetryDispatchRepository("owner/repo").get(authority.request_id)
    assert retained is not None
    assert retained.external_id == "task-retained"
    assert retained.projection_disposition == "accepted-tracking-incomplete"


@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_claimed_retry_recovers_receipt_from_matching_accepted_run(mock_client_type, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    client = mock_client_type.return_value
    client.environment_id = "environment-retained"
    client.submit_task.return_value = CodexSubmissionResult(CodexSubmissionOutcome.ACCEPTED, "task-recovered")
    authority = _authority()
    original = RetryDispatchRepository.record_outcome
    failed = False

    def fail_first_receipt(self, request_id, outcome, **kwargs):
        nonlocal failed
        if outcome == "accepted" and not failed:
            failed = True
            raise OSError("receipt journal unavailable")
        return original(self, request_id, outcome, **kwargs)

    with patch.object(RetryDispatchRepository, "record_outcome", fail_first_receipt):
        first = _dispatch(authority)
    replay = _dispatch(authority)

    assert "receipt could not be persisted" in first[0]
    assert "already accepted" in replay[0]
    client.submit_task.assert_called_once()
    retained = RetryDispatchRepository("owner/repo").get(authority.request_id)
    assert retained is not None
    assert (retained.outcome, retained.external_id, retained.projection_disposition) == (
        "accepted",
        "task-recovered",
        "accepted-current",
    )


@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_run_write_failure_retains_receipt_and_repairs_without_resubmission(mock_client_type, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    authority = _authority()
    client = mock_client_type.return_value
    client.environment_id = "environment-retained"
    client.submit_task.return_value = CodexSubmissionResult(
        CodexSubmissionOutcome.ACCEPTED,
        "task-after-run-failure",
        "https://example.test/task-after-run-failure",
    )
    original = CloudRunRepository.update_claim
    failed = False

    def fail_once(self, run):
        nonlocal failed
        if run.submission_outcome == "accepted" and not failed:
            failed = True
            raise OSError("run store unavailable")
        return original(self, run)

    with patch.object(CloudRunRepository, "update_claim", fail_once):
        first = _dispatch(authority)
    replay = _dispatch(authority)

    assert "tracking is incomplete" in first[0]
    assert "already accepted" in replay[0]
    client.submit_task.assert_called_once()
    run = CloudRunRepository("owner/repo").get(2223, 5)
    assert run is not None
    assert (run.task_id, run.environment_id, run.submission_outcome) == (
        "task-after-run-failure",
        "environment-retained",
        "accepted",
    )
    handoff = RetryDispatchRepository("owner/repo").get(authority.request_id)
    assert handoff is not None
    assert (handoff.external_id, handoff.environment_id, handoff.projection_disposition) == (
        "task-after-run-failure",
        "environment-retained",
        "accepted-current",
    )


@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_legacy_accepted_retry_confirms_matching_current_projection(mock_client_type, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    authority = _authority()
    routing_path = Path.home() / ".auto-coder" / "issue-stage-routing.sqlite3"
    with sqlite3.connect(routing_path) as connection:
        connection.execute(
            "UPDATE implementation_retry_requests SET predecessor_captured=0,predecessor_provider=NULL,predecessor_task_id=NULL,predecessor_backend_name=NULL WHERE request_id=?",
            (authority.request_id,),
        )
    authority = IssueStageRoutingStore(routing_path).retry_request(authority.request_id)
    assert authority is not None
    dispatch = RetryDispatchRepository("owner/repo")
    dispatch.claim(authority, "codex-cloud", "codex-alias", {"base_branch": "main"})
    handoff = dispatch.allocate_numeric_attempt(authority.request_id, [4])
    assert handoff.numeric_attempt == 5
    dispatch.record_outcome(
        authority.request_id,
        "accepted",
        external_id="legacy-task",
        external_url="https://example.test/legacy-task",
    )
    CloudRunRepository("owner/repo").save(
        CloudRun(
            "owner/repo",
            2223,
            5,
            "codex-cloud",
            task_id="legacy-task",
            backend_name="codex-alias",
            environment_id="legacy-environment",
            base_branch="main",
            task_url="https://example.test/legacy-task",
        )
    )
    assert CloudManager("owner/repo").add_session(2223, "legacy-task", "codex-cloud", "codex-alias")

    result = _dispatch(authority)

    mock_client_type.assert_not_called()
    assert "already accepted" in result[0]
    retained = RetryDispatchRepository("owner/repo").get(authority.request_id)
    assert retained is not None
    assert retained.projection_disposition == "accepted-current"


@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_unreadable_or_mismatched_durable_authority_prevents_creation(mock_client_type, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    authority = _authority()
    with patch.object(IssueStageRoutingStore, "retry_request", side_effect=OSError("authority store unreadable")):
        unreadable = _dispatch(authority)

    with (
        patch("auto_coder.issue_processor.get_current_attempt", return_value=4),
        patch("auto_coder.issue_processor.get_commit_log", return_value=""),
    ):
        mismatched = _process_issue_codex_cloud_mode(
            "owner/repo",
            {"number": 2224, "title": "Wrong target", "body": "", "labels": []},
            AutomationConfig(),
            MagicMock(),
            "codex-alias",
            retry_authority=authority,
        )

    mock_client_type.assert_not_called()
    assert "authority store unreadable" in unreadable[0]
    assert "does not match owned dispatch authority" in mismatched[0]


@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_binding_installed_after_admission_is_not_treated_as_predecessor(mock_client_type, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    manager = CloudManager("owner/repo")
    assert manager.add_session(2223, "predecessor", provider="jules", backend_name="jules")
    authority = _authority()
    assert manager.add_session(2223, "unrelated", provider="claude-routine", backend_name="claude")
    client = mock_client_type.return_value
    client.environment_id = "environment"
    client.submit_task.return_value = CodexSubmissionResult(CodexSubmissionOutcome.ACCEPTED, "task-new")

    result = _dispatch(authority)

    assert "tracking is incomplete" in result[0]
    assert manager.read_bindings_strict()["2223"].task_id == "unrelated"
    retained = RetryDispatchRepository("owner/repo").get(authority.request_id)
    assert retained is not None
    assert retained.projection_disposition == "accepted-tracking-incomplete"


def test_acceptance_and_cross_provider_promotion_share_one_stale_writer_fence(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    manager = CloudManager("owner/repo")
    predecessor = CloudTaskBinding("jules", "predecessor", "jules")
    assert manager.add_session(2223, predecessor.task_id, predecessor.provider, predecessor.backend_name)
    first = _authority("request-1")
    second = _authority("request-2")
    store = RetryDispatchRepository("owner/repo")
    for authority, route in (
        (first, "claude-routine"),
        (second, "codex-cloud"),
    ):
        store.claim(
            authority,
            route,
            route,
            {"base_branch": "main"},
            predecessor=(predecessor.provider, predecessor.task_id, predecessor.backend_name),
        )
    store.record_outcome(first.request_id, "accepted", external_id="claude-task")

    reached_check = Event()
    release_check = Event()

    def older_promotion() -> str:
        def checked() -> bool:
            reached_check.set()
            release_check.wait(timeout=5)
            return store.is_latest_accepted(first.request_id)

        return manager.promote_retry_binding(
            2223,
            CloudTaskBinding("claude-routine", "claude-task", "claude-routine"),
            predecessor,
            checked,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        older = pool.submit(older_promotion)
        assert reached_check.wait(timeout=5)

        def accept_and_promote_newer() -> str:
            store.record_outcome(second.request_id, "accepted", external_id="codex-task")
            disposition = manager.promote_retry_binding(
                2223,
                CloudTaskBinding("codex-cloud", "codex-task", "codex-cloud"),
                predecessor,
                lambda: store.is_latest_accepted(second.request_id),
                (CloudTaskBinding("claude-routine", "claude-task", "claude-routine"),),
            )
            store.mark_prior_accepted_historical(second.request_id)
            return disposition

        newer = pool.submit(accept_and_promote_newer)
        release_check.set()
        assert older.result(timeout=5) == "current"
        assert newer.result(timeout=5) == "current"

    assert manager.read_bindings_strict()["2223"].task_id == "codex-task"
    assert store.get(first.request_id).projection_disposition == "accepted-historical"

    assert (
        _acknowledge_retry_projection(
            manager,
            store,
            first.request_id,
            2223,
            CloudTaskBinding("claude-routine", "claude-task", "claude-routine"),
        )
        == "historical"
    )
    assert store.get(first.request_id).projection_disposition == "accepted-historical"


import sqlite3
