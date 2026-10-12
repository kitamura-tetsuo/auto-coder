from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from time import monotonic, sleep

from auto_coder.attempt_manager import extract_attempt_number
from auto_coder.local_issue_start_notification import LocalIssueStartNotifier, notification_body, notification_marker
from auto_coder.local_job_handoff import (
    LocalJobKind,
    LocalJobRecord,
    LocalJobState,
    LocalJobStore,
    StartNotificationState,
)
from auto_coder.local_llm_observability import bind_local_llm_start_callback, observe_local_llm_call, reset_local_llm_start_callback


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
