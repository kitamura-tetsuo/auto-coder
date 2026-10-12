from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from auto_coder.attempt_manager import extract_attempt_number
from auto_coder.backend_manager import BackendManager
from auto_coder.invocation_admission import InvocationAdmissionGate
from auto_coder.issue_dispatch import CandidateHandoff, IssueAttemptIdentity, IssueDispatchGuard
from auto_coder.issue_local_job import IssueLocalJobAdapter
from auto_coder.local_issue_start_notification import LocalIssueStartNotifier, notification_body, notification_marker
from auto_coder.local_job_handoff import (
    LocalJobKind,
    LocalJobOffer,
    LocalJobRecord,
    LocalJobState,
    LocalJobStore,
    StartNotificationState,
)
from auto_coder.local_job_runner import LocalJobRunner
from auto_coder.local_llm_observability import bind_local_llm_start_callback
from auto_coder.local_llm_observability import logger as observability_logger
from auto_coder.local_llm_observability import observe_local_llm_call, reset_local_llm_start_callback
from tests.test_issue_job_workspace import repository


class RecordingGitHub:
    def __init__(self) -> None:
        self.comments: list[dict[str, str]] = []
        self.posts = 0
        self.post_error: Exception | None = None
        self.read_error: Exception | None = None

    def add_comment_to_issue(self, repository: str, issue_number: int, body: str) -> None:
        self.posts += 1
        self.comments.append({"body": body, "repository": repository, "number": str(issue_number)})
        if self.post_error is not None:
            raise self.post_error

    def get_issue_comments_strict(self, repository: str, issue_number: int) -> list[dict[str, str]]:
        if self.read_error is not None:
            raise self.read_error
        return self.comments


class DefinitelyUnsentError(RuntimeError):
    definitely_not_sent = True


def _job(job_id: str = "stable-attempt-id") -> LocalJobRecord:
    return LocalJobRecord(
        job_id=job_id,
        kind=LocalJobKind.ISSUE_IMPLEMENTATION,
        repository="owner/repo",
        target_number=2471,
        upstream_attempt="3",
        upstream_incarnation="dispatch-claim",
        backend_name="configured-alias",
        input_identity="input",
        invocation_input="prompt",
        state=LocalJobState.RUNNING,
        execution_incarnation="ephemeral-runner-incarnation",
        provider_entered=True,
    )


def _wait_for_state(store: LocalJobStore, job_id: str, state: StartNotificationState) -> None:
    deadline = monotonic() + 5
    while monotonic() < deadline:
        retained = store.get_start_notification(job_id)
        if retained is not None and retained.state is state:
            return
        sleep(0.01)
    retained = store.get_start_notification(job_id)
    assert retained is not None and retained.state is state


def _offer(store: LocalJobStore, tmp_path: Path, *, number: int, attempt: str) -> LocalJobRecord:
    guard = IssueDispatchGuard(tmp_path / f"dispatch-{number}-{attempt}.sqlite3")
    identity = IssueAttemptIdentity("owner", "repo", number, attempt)
    assert guard.reserve(identity, CandidateHandoff("local-alias", "local")).admitted
    offered = store.offer_issue(
        LocalJobOffer(LocalJobKind.ISSUE_IMPLEMENTATION, "owner/repo", number, attempt, "local-alias", f"implement issue {number}"),
        identity,
        guard,
    )
    assert offered is not None
    return offered


def _wait_for_runner(runner: LocalJobRunner) -> None:
    deadline = monotonic() + 10
    while runner.active_count() and monotonic() < deadline:
        sleep(0.01)
    assert runner.active_count() == 0


def test_actual_editing_boundary_posts_truthful_comment_before_client_returns(tmp_path: Path) -> None:
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    github = RecordingGitHub()
    notifier = LocalIssueStartNotifier(store, github)
    pending_client = Event()
    token = bind_local_llm_start_callback(lambda start: notifier.observe_and_publish(_job(), backend=start.backend, model=start.model))
    try:
        with observe_local_llm_call(
            backend="codex-fast",
            backend_type="codex",
            provider=None,
            model="gpt-local",
            invocation_id="client-call",
            continuation=False,
            is_noedit=False,
        ):
            pending_client.set()
            _wait_for_state(store, _job().job_id, StartNotificationState.CONFIRMED)
            retained = store.get_start_notification(_job().job_id)
            assert pending_client.is_set()
            assert retained is not None and retained.state is StartNotificationState.CONFIRMED
            assert github.posts == 1
    finally:
        reset_local_llm_start_callback(token)

    body = github.comments[0]["body"]
    assert "Backend: `codex-fast`" in body
    assert "Model: `gpt-local`" in body
    assert "Implementation attempt: `stable-attempt-id`" in body
    assert notification_marker("stable-attempt-id") in body
    assert "passing tests" in body and "published pull request" in body
    assert extract_attempt_number(body) is None


def test_read_only_boundary_and_pre_boundary_work_do_not_observe_start(tmp_path: Path) -> None:
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    github = RecordingGitHub()
    notifier = LocalIssueStartNotifier(store, github)
    token = bind_local_llm_start_callback(lambda start: notifier.observe_and_publish(_job(), backend=start.backend, model=start.model))
    try:
        assert store.get_start_notification(_job().job_id) is None
        with observe_local_llm_call(
            backend="codex",
            backend_type="codex",
            provider=None,
            model=None,
            invocation_id="review",
            continuation=False,
            is_noedit=True,
        ):
            pass
    finally:
        reset_local_llm_start_callback(token)
    assert store.get_start_notification(_job().job_id) is None
    assert github.posts == 0


def test_ambiguous_accepted_post_reconciles_exact_marker_without_repost(tmp_path: Path) -> None:
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    github = RecordingGitHub()
    github.post_error = TimeoutError("response lost")
    notifier = LocalIssueStartNotifier(store, github)

    notifier.observe_and_publish(_job(), backend="codex", model="unknown")
    _wait_for_state(store, _job().job_id, StartNotificationState.CONFIRMED)
    retained = store.get_start_notification(_job().job_id)
    assert retained is not None and retained.state is StartNotificationState.CONFIRMED
    assert github.posts == 1

    LocalIssueStartNotifier(LocalJobStore(store.path), github).recover()
    assert github.posts == 1


def test_ambiguous_post_with_unavailable_evidence_stays_indeterminate(tmp_path: Path) -> None:
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    github = RecordingGitHub()
    github.post_error = TimeoutError("response lost")
    github.read_error = RuntimeError("GitHub unavailable")

    LocalIssueStartNotifier(store, github).observe_and_publish(_job(), backend="codex", model="unknown")
    _wait_for_state(store, _job().job_id, StartNotificationState.INDETERMINATE)
    retained = store.get_start_notification(_job().job_id)
    assert retained is not None and retained.state is StartNotificationState.INDETERMINATE
    LocalIssueStartNotifier(LocalJobStore(store.path), github).recover()
    assert github.posts == 1


def test_definitely_unsent_delivery_recovers_after_job_settlement(tmp_path: Path) -> None:
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    github = RecordingGitHub()
    github.post_error = DefinitelyUnsentError("request never sent")
    notifier = LocalIssueStartNotifier(store, github)
    notifier.observe_and_publish(_job(), backend="codex", model="model")
    _wait_for_state(store, _job().job_id, StartNotificationState.DEFINITELY_UNSENT)
    retained = store.get_start_notification(_job().job_id)
    assert retained is not None and retained.state is StartNotificationState.DEFINITELY_UNSENT

    github.post_error = None
    LocalIssueStartNotifier(LocalJobStore(store.path), github).recover()
    retained = store.get_start_notification(_job().job_id)
    assert retained is not None and retained.state is StartNotificationState.CONFIRMED
    assert github.posts == 2


def test_concurrent_claim_has_one_authoritative_publisher(tmp_path: Path) -> None:
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    job = _job()
    marker = notification_marker(job.job_id)
    retained = store.observe_issue_start(job, backend_name="codex", model_name="model", marker=marker)
    assert retained is not None
    barrier = Barrier(2)

    def contend() -> object:
        barrier.wait()
        return LocalJobStore(store.path).claim_start_notification(job.job_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(lambda _: contend(), range(2)))
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert winners[0].state is StartNotificationState.INDETERMINATE
    assert notification_body(winners[0]).endswith(marker)


def test_production_runner_posts_at_real_adapter_manager_boundary_after_false_start(tmp_path: Path, monkeypatch, _use_real_commands) -> None:
    """REQ-001/002: exercise handoff -> runner -> adapter -> manager -> client."""
    source, _head = repository(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    false_start = _offer(store, tmp_path, number=2470, attempt="1")
    actual_start = _offer(store, tmp_path, number=2471, attempt="1")
    github = RecordingGitHub()
    engine = MagicMock()
    engine.github = github
    slots = MagicMock()
    slots.owner_incarnation.return_value = "owner-incarnation"
    slots.implementation_generation.return_value = "generation"
    adapter = IssueLocalJobAdapter(engine, "owner/repo", store, slots, source)
    entered = Event()
    release = Event()

    client = MagicMock()
    client.config_backend = SimpleNamespace(backend_type="codex")
    client.model_name = "actual-model"
    client.use_noedit_options = False
    client.get_last_session_id.return_value = None

    def editing_client(prompt: str, *, is_noedit: bool = False) -> str:
        assert prompt.startswith(actual_start.invocation_input)
        assert is_noedit is False
        entered.set()
        assert release.wait(10)
        return "ACTION_SUMMARY: implemented"

    client._run_llm_cli.side_effect = editing_client
    manager = BackendManager(
        default_backend="actual-alias",
        default_client=client,
        factories={"actual-alias": lambda: client},
        order=["actual-alias"],
    )
    manager._get_current_provider_name = lambda _backend: "local-provider"  # type: ignore[method-assign]
    config = MagicMock()
    config.get_model_for_backend.return_value = "configured-model"
    builds = 0

    def build_manager(**_kwargs):  # type: ignore[no-untyped-def]
        nonlocal builds
        builds += 1
        if builds == 1:
            raise RuntimeError("backend construction failed after provider checkpoint")
        return manager

    monkeypatch.setenv("AUTO_CODER_DEFER_LOCAL_TESTS", "1")
    runner = LocalJobRunner(
        store,
        InvocationAdmissionGate(),
        repository="owner/repo",
        capacity=1,
        adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter},
        job_ids=frozenset({false_start.job_id, actual_start.job_id}),
    )
    with (
        patch.object(adapter, "authorize_provider_entry", return_value=True),
        patch("auto_coder.llm_backend_config.get_llm_config", return_value=config),
        patch("auto_coder.cli_helpers.build_backend_manager", side_effect=build_manager),
    ):
        assert runner.poll() == 1
        _wait_for_runner(runner)
        retained_false_start = store.get(false_start.job_id)
        assert retained_false_start is not None
        artifact = store.get_result_artifact(retained_false_start.result_reference)
        assert retained_false_start.provider_entered, artifact.output if artifact is not None else retained_false_start.diagnostic
        assert store.get_start_notification(false_start.job_id) is None
        assert github.posts == 0

        assert runner.poll() == 1
        assert entered.wait(10)
        _wait_for_state(store, actual_start.job_id, StartNotificationState.CONFIRMED)
        assert github.posts == 1
        assert client._run_llm_cli.call_count == 1
        comment = github.comments[0]
        assert comment["repository"] == "owner/repo"
        assert comment["number"] == "2471"
        assert "Backend: `actual-alias`" in comment["body"]
        assert "Model: `actual-model`" in comment["body"]
        assert f"Implementation attempt: `{actual_start.job_id}`" in comment["body"]
        assert notification_marker(actual_start.job_id) in comment["body"]
        assert runner.active_count() == 1
        release.set()
        _wait_for_runner(runner)
    runner.close(wait=True)
    manager.close()


def test_notification_store_and_github_failures_do_not_change_live_client_result(tmp_path: Path) -> None:
    """REQ-007: synchronous storage and asynchronous transport failures are inert."""

    class RaisingStore:
        def observe_issue_start(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("notification store unavailable")

    body_ran = False
    warnings: list[str] = []
    sink = observability_logger.add(lambda message: warnings.append(str(message)))
    store_notifier = LocalIssueStartNotifier(RaisingStore(), RecordingGitHub())  # type: ignore[arg-type]
    token = bind_local_llm_start_callback(lambda start: store_notifier.observe_and_publish(_job("store-failure"), backend=start.backend, model=start.model))
    try:
        with observe_local_llm_call(
            backend="codex",
            backend_type="codex",
            provider=None,
            model="model",
            invocation_id="store-failure-call",
            continuation=False,
            is_noedit=False,
        ):
            body_ran = True
    finally:
        reset_local_llm_start_callback(token)
        observability_logger.remove(sink)
    assert body_ran
    assert any("Local Issue start notification failed before client dispatch" in warning for warning in warnings)

    durable_store = LocalJobStore(tmp_path / "jobs.sqlite3")
    github = RecordingGitHub()
    github.post_error = TimeoutError("response lost")
    github.read_error = RuntimeError("comments unavailable")
    github_notifier = LocalIssueStartNotifier(durable_store, github)
    token = bind_local_llm_start_callback(lambda start: github_notifier.observe_and_publish(_job("github-failure"), backend=start.backend, model=start.model))
    client_result = ""
    try:
        with observe_local_llm_call(
            backend="codex",
            backend_type="codex",
            provider=None,
            model="model",
            invocation_id="github-failure-call",
            continuation=False,
            is_noedit=False,
        ):
            client_result = "ACTION_SUMMARY: completed despite notification failure"
    finally:
        reset_local_llm_start_callback(token)
    assert client_result == "ACTION_SUMMARY: completed despite notification failure"
    _wait_for_state(durable_store, "github-failure", StartNotificationState.INDETERMINATE)
    retained = durable_store.get_start_notification("github-failure")
    assert retained is not None and retained.state is not StartNotificationState.CONFIRMED
    assert github.posts == 1
