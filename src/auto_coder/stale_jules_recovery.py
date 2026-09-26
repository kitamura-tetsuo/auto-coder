"""Durable automatic recovery identity for stale-Jules-PR retries (Issue #2286).

Ties a Jules PR's stale-CI closure to exactly one durable recovery identity
``R``, reusing the existing explicit-retry machinery already built for the
operator-issued ``--only --force --retry`` path (``issue_stage_routing.py``'s
``implementation_retry_requests`` table and ``implementation_ownership.py``'s
``acquire_explicit_retry``) instead of a parallel controller.

``R`` is a deterministic ``ImplementationRetryRequest`` request id derived
from ``(repository, issue_number, pr_number)`` alone (never from a
timestamp, comment, or PR head SHA), so reprocessing the same stale-PR
closure -- a restart, a duplicate wake, or the same PR re-observed -- always
reuses the same durable record instead of minting another one. Generic PR
closure, an Issue comment, or a bare attempt-counter change never create
``R`` by themselves; only :func:`capture_stale_jules_recovery` does, and only
when a current Implementation generation (``G``) for the target Issue is
available.

The actual "wait for predecessor retirement" and "bypass the owned-start
tombstone for a distinct successor attempt" behavior is intentionally not
reimplemented here: callers reuse ``ImplementationSlotRepository`` (retirement
already frees the owner record; ``has_qualifying_implementation_activity``
already reports whether it is still busy) and ``acquire_explicit_retry``
(the existing bypass) directly. This module only owns durably recording and
rediscovering the recovery identity itself.
"""

from __future__ import annotations

import contextlib
import hashlib
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, List, Optional

from .attempt_manager import ATTEMPT_TRIGGER_PREFIX, extract_attempt_number, extract_attempt_trigger, format_attempt_comment
from .issue_stage_routing import ImplementationRetryRequest, IssueStageRoutingStore, RetryRequestConflict
from .logger_config import get_logger

logger = get_logger(__name__)

RECOVERY_REQUEST_PREFIX = "stale-jules-recovery"


@dataclass(frozen=True)
class StaleJulesRecoveryContext:
    """Durable-store handles the engine hands PR processing for this cycle.

    Threading these two handles through every intermediate PR-processing
    helper (``process_pull_request`` -> its merge/repair sub-helpers ->
    ``_close_stale_jules_pr``) would touch a large, unrelated call chain for
    a purely additive capability. A context variable set once at the
    ``process_pull_request`` boundary keeps the signature change local to
    the two places that actually need these handles.
    """

    stage_routing: IssueStageRoutingStore
    implementation_slots: Optional[Any]


_current_context: "ContextVar[Optional[StaleJulesRecoveryContext]]" = ContextVar("stale_jules_recovery_context", default=None)


@contextlib.contextmanager
def stale_jules_recovery_context(stage_routing: Optional[IssueStageRoutingStore], implementation_slots: Optional[Any] = None) -> Iterator[None]:
    """Make *stage_routing*/*implementation_slots* available to nested PR processing.

    A no-op (context stays unset) when *stage_routing* is ``None``, so
    callers that do not have engine context (most existing tests) see
    exactly today's legacy behavior.
    """
    if stage_routing is None:
        yield
        return
    token = _current_context.set(StaleJulesRecoveryContext(stage_routing, implementation_slots))
    try:
        yield
    finally:
        _current_context.reset(token)


def current_stale_jules_recovery_context() -> Optional[StaleJulesRecoveryContext]:
    """Return the context set by the nearest enclosing :func:`stale_jules_recovery_context`."""
    return _current_context.get()


def recovery_request_id(repository: str, issue_number: int, pr_number: int) -> str:
    """Return the deterministic recovery identity ``R`` for one (Issue, PR) pair.

    Deterministic so reprocessing the same stale-PR closure decision (restart,
    duplicate wake, or the same still-open PR re-observed) reuses the same
    durable record rather than minting another one (REQ-001).
    """
    digest = hashlib.sha256(f"{repository}#{issue_number}#{pr_number}".encode("utf-8")).hexdigest()[:32]
    return f"{RECOVERY_REQUEST_PREFIX}-{digest}"


def is_recovery_request_id(request_id: str) -> bool:
    """Return whether *request_id* was minted by this automatic origin.

    Used to distinguish an automatic stale-Jules recovery grant from an
    operator-issued ``--only --force --retry`` request when both share the
    same durable ``implementation_retry_requests`` table (#2286 does not
    change that CLI contract or its own request identities).
    """
    return isinstance(request_id, str) and request_id.startswith(f"{RECOVERY_REQUEST_PREFIX}-")


def _recovery_attempt_trigger(request_id: str) -> str:
    """Trigger marker recorded on the published ``Auto-Coder Attempt`` comment."""
    return f"{RECOVERY_REQUEST_PREFIX}:{request_id}"


def capture_stale_jules_recovery(
    routing: IssueStageRoutingStore,
    repo_name: str,
    issue_number: int,
    pr_number: int,
    generation: str,
) -> Optional[ImplementationRetryRequest]:
    """Durably record recovery identity ``R`` before any PR-close or attempt effect.

    Idempotent: reprocessing the same (repository, Issue, PR) triple returns
    the same durable record. Returns ``None`` when a durable ``R`` for this
    exact triple already exists bound to a *different* Implementation
    generation -- the Issue's specification changed underneath an
    unconsumed grant, which permanently disqualifies it from an automatic
    retry (REQ-005); a fresh stale PR would be required to try again.
    """
    request_id = recovery_request_id(repo_name, issue_number, pr_number)
    try:
        return routing.accept_retry_request(request_id, repo_name, issue_number, generation)
    except RetryRequestConflict:
        logger.warning(f"Stale-Jules recovery {request_id} for issue #{issue_number} is already bound to a " "different Implementation generation than the one observed now; withholding an " "automatic retry grant for this PR")
        return None


def find_pending_stale_jules_recovery(
    routing: IssueStageRoutingStore,
    repo_name: str,
    issue_number: int,
) -> Optional[ImplementationRetryRequest]:
    """Return this Issue's pending automatic stale-Jules recovery request, if any.

    Only a ``pending`` request is returned: an ``owned`` request has already
    acquired its successor attempt (nothing left to admit), and an
    ``invalidated`` one is permanently disqualified (REQ-005) and must never
    be treated as authorizing a fresh start.
    """
    for request in routing.retry_requests(repo_name, issue_number):
        if request.status == "pending" and is_recovery_request_id(request.request_id):
            return request
    return None


def publish_recovery_attempt_comment(
    github_client: Any,
    repo_name: str,
    issue_number: int,
    request_id: str,
) -> Optional[int]:
    """Publish (or recognize an already-published) numeric attempt for *request_id*.

    Reads every comment looking for the exact trigger marker this recovery
    would have published; if found, returns its recorded number without
    posting again (REQ-002: "reentry cannot increment again"). Otherwise
    allocates one attempt number strictly above every attempt number
    observed in the comment thread and publishes it.

    Returns ``None`` when the comment thread could not be read at all -- the
    caller must defer rather than treat that as "no prior attempts" (REQ-002
    explicitly forbids defaulting unavailable evidence to attempt zero).
    """
    trigger = _recovery_attempt_trigger(request_id)
    try:
        comments = list(github_client.get_issue_comments(repo_name, issue_number))
    except Exception as exc:
        logger.warning(f"Cannot read issue #{issue_number} comments to publish recovery attempt {request_id}: {exc}")
        return None

    attempt_numbers: List[int] = []
    for comment in comments:
        body = comment.get("body", "") or ""
        number = extract_attempt_number(body)
        if number is not None:
            attempt_numbers.append(number)
        if extract_attempt_trigger(body) == trigger:
            # Already published for this exact recovery identity: reuse the
            # retained allocation, never increment again.
            return number

    new_attempt = (max(attempt_numbers) if attempt_numbers else 0) + 1
    comment_body = format_attempt_comment(new_attempt, details=f"{ATTEMPT_TRIGGER_PREFIX}{trigger}")
    github_client.add_comment_to_issue(repo_name, issue_number, comment_body)
    logger.info(f"Published stale-Jules recovery attempt {new_attempt} for issue #{issue_number} (request={request_id})")
    return new_attempt
