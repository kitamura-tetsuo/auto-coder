from unittest.mock import MagicMock, patch

from auto_coder.automation_config import AutomationConfig
from auto_coder.cloud_manager import CloudManager, CloudTaskBinding
from auto_coder.cloud_run import CloudRunRepository
from auto_coder.codex_cloud_client import CodexSubmissionOutcome, CodexSubmissionResult
from auto_coder.issue_processor import _process_issue_codex_cloud_mode
from auto_coder.issue_stage_routing import ImplementationRetryRequest
from auto_coder.retry_dispatch import RetryDispatchRepository


def _authority(request_id: str = "request-1", attempt_id: str = "logical-1") -> ImplementationRetryRequest:
    return ImplementationRetryRequest(
        request_id=request_id,
        repository="owner/repo",
        target_number=2223,
        generation="generation-7",
        attempt_id=attempt_id,
        status="owned",
        ownership_reference=f"execution-{request_id}",
    )


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

    def replace_before_promotion(self, issue_number, binding, predecessor, may_promote):
        self.add_session(issue_number, "unknown-racer", provider="claude-routine", backend_name="claude")
        return original_promote(self, issue_number, binding, predecessor, may_promote)

    with patch.object(CloudManager, "promote_retry_binding", replace_before_promotion):
        result = _dispatch(_authority())

    assert "tracking is incomplete" in result[0]
    handoff = RetryDispatchRepository("owner/repo").get("request-1")
    assert handoff is not None
    assert handoff.outcome == "accepted"
    assert handoff.external_id == "task-new"
    assert handoff.projection_disposition == "accepted-tracking-incomplete"
    assert manager.read_bindings_strict()["2223"].task_id == "unknown-racer"
