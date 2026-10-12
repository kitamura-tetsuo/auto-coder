"""Crash-safe GitHub notification for actual local Issue client dispatch."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from hashlib import sha256
from typing import Iterator, Protocol

from .local_job_handoff import LocalJobKind, LocalJobRecord, LocalJobState, LocalJobStore, LocalStartNotification, StartNotificationState
from .local_llm_observability import bind_local_llm_start_callback, reset_local_llm_start_callback
from .logger_config import get_logger
from .util.github_request_outcome import DeliveryCertainty, GitHubRequestError

logger = get_logger(__name__)


class IssueCommentClient(Protocol):
    def add_comment_to_issue(self, repository: str, issue_number: int, body: str) -> None: ...

    def get_issue_comments_strict(self, repository: str, issue_number: int) -> list[dict[str, object]]: ...


def notification_marker(job_id: str) -> str:
    return f"<!-- auto-coder-local-implementation-start:{job_id} -->"


def notification_body(notification: LocalStartNotification) -> str:
    return (
        "Auto-Coder started a local Issue implementation client dispatch.\n\n"
        f"- Backend: `{notification.backend_name}`\n"
        f"- Model: `{notification.model_name or 'unknown'}`\n"
        f"- Implementation attempt: `{notification.job_id}`\n\n"
        "This reports dispatch only; it does not indicate completed edits, passing tests, or a published pull request.\n\n"
        f"{notification.marker}"
    )


class LocalIssueStartNotifier:
    """Publish/reconcile exact-marker comments without controlling the invocation."""

    def __init__(self, store: LocalJobStore, github_client: IssueCommentClient) -> None:
        self._store = store
        self._github = github_client

    def observe_and_publish(self, job: LocalJobRecord, *, backend: str, model: str) -> None:
        marker = notification_marker(job.job_id)
        retained = self._store.observe_issue_start(job, backend_name=backend, model_name=model or "unknown", marker=marker)
        if retained is None:
            logger.warning("Could not retain local Issue start notification for {}", job.job_id)
            return
        # GitHub latency and failure are not part of provider admission or
        # capacity. The durable pending row is established synchronously, then
        # delivery proceeds while the editing client is allowed to run.
        threading.Thread(
            target=self._publish_if_safe,
            args=(retained,),
            name=f"local-issue-start-{job.target_number}",
            daemon=True,
        ).start()

    def recover(self) -> None:
        try:
            recoverable = self._store.recoverable_start_notifications()
        except Exception as exc:
            logger.warning("Local Issue start notification recovery enumeration failed: {}", type(exc).__name__)
            return
        for notification in recoverable:
            try:
                if notification.state is StartNotificationState.INDETERMINATE:
                    self._reconcile(notification)
                else:
                    self._publish_if_safe(notification)
            except Exception as exc:
                logger.warning("Local Issue start notification recovery failed for {}: {}", notification.job_id, type(exc).__name__)

    def _publish_if_safe(self, notification: LocalStartNotification) -> None:
        try:
            claimed = self._store.claim_start_notification(notification.job_id)
        except Exception as exc:
            logger.warning("Local Issue start notification claim failed for {}: {}", notification.job_id, type(exc).__name__)
            return
        if claimed is None:
            return
        try:
            self._github.add_comment_to_issue(claimed.repository, claimed.issue_number, notification_body(claimed))
        except Exception as exc:
            definite = (isinstance(exc, GitHubRequestError) and exc.outcome.delivery is DeliveryCertainty.DEFINITELY_NOT_SENT) or getattr(exc, "definitely_not_sent", False) is True
            next_state = StartNotificationState.DEFINITELY_UNSENT if definite else StartNotificationState.INDETERMINATE
            try:
                self._store.set_start_notification_state(claimed.job_id, StartNotificationState.INDETERMINATE, next_state, f"{type(exc).__name__}: {exc}")
            except Exception as state_exc:
                logger.warning("Local Issue start notification failure state could not be retained for {}: {}", claimed.job_id, type(state_exc).__name__)
            logger.warning("Local Issue start comment delivery {} for {}: {}", next_state.value, claimed.job_id, type(exc).__name__)
            if not definite:
                self._reconcile(claimed)
            return
        try:
            confirmed = self._store.set_start_notification_state(claimed.job_id, StartNotificationState.INDETERMINATE, StartNotificationState.CONFIRMED)
        except Exception as exc:
            logger.warning("Local Issue start notification receipt write failed for {}: {}", claimed.job_id, type(exc).__name__)
            return
        if not confirmed:
            logger.warning("Local Issue start comment posted but receipt could not be retained for {}", claimed.job_id)

    def _reconcile(self, notification: LocalStartNotification) -> None:
        try:
            comments = self._github.get_issue_comments_strict(notification.repository, notification.issue_number)
        except Exception as exc:
            logger.warning("Local Issue start comment reconciliation unavailable for {}: {}", notification.job_id, type(exc).__name__)
            return
        found = any(isinstance(comment, dict) and notification.marker in str(comment.get("body", "")) for comment in comments)
        if found:
            try:
                confirmed = self._store.set_start_notification_state(notification.job_id, StartNotificationState.INDETERMINATE, StartNotificationState.CONFIRMED)
            except Exception as exc:
                logger.warning("Local Issue start notification reconciliation receipt write failed for {}: {}", notification.job_id, type(exc).__name__)
                return
            if not confirmed:
                logger.warning("Local Issue start notification reconciliation receipt was not retained for {}", notification.job_id)


@contextmanager
def bind_synchronous_issue_start_notification(
    *,
    repository: str,
    issue_number: int,
    implementation_attempt: str,
    github_client: IssueCommentClient,
    store: LocalJobStore | None = None,
) -> Iterator[None]:
    """Bind the reachable synchronous Issue path to the shared client boundary."""
    identity = sha256(f"synchronous-local-issue:{repository}:{issue_number}:{implementation_attempt}".encode()).hexdigest()
    job = LocalJobRecord(
        job_id=identity,
        kind=LocalJobKind.ISSUE_IMPLEMENTATION,
        repository=repository,
        target_number=issue_number,
        upstream_attempt=implementation_attempt,
        upstream_incarnation="synchronous",
        backend_name="local",
        input_identity="synchronous",
        invocation_input="",
        state=LocalJobState.RUNNING,
        provider_entered=True,
    )
    notifier = LocalIssueStartNotifier(store or LocalJobStore(), github_client)
    token = bind_local_llm_start_callback(lambda start: notifier.observe_and_publish(job, backend=start.backend, model=start.model))
    try:
        yield
    finally:
        reset_local_llm_start_callback(token)
