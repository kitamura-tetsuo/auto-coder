"""Production GitHub adapter for the approval and merge effects of #1937.

This is the single place that turns a real GitHub review/merge HTTP exchange
into one of the terminal (or deferred) outcomes ``MergeOperationStore``
understands. It owns exactly the following per effect: reserve exclusive
execution rights, send at most one mutating request (never a hidden retry,
fallback method, alternate endpoint, or extra diagnostic GET beyond what is
required to check for an already-satisfied approval or to reconcile a
delivery-unknown effect), interpret the response/exception, and record one
correlated terminal outcome or a typed deferral.

Wiring this into ``pr_processor.py``'s normal PR-processing callers or the
pending-work scheduler is explicitly out of scope for this module (Issue
#1938 is adapter-only; production wiring is owned by a later child issue
under #1936). No CI/review-pass judgment, provider repair, or Issue/session
lifecycle mutation happens here either -- see the Non-goals section of
Issue #1938.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from .logger_config import get_logger
from .merge_operation_state import (
    BlockReason,
    ConfirmationSource,
    EffectName,
    EffectReceipt,
    EffectState,
    MergeOperation,
    MergeOperationIdentity,
    MergeOperationStore,
)
from .util.gh_cache import get_ghapi_client
from .util.github_request_outcome import GitHubApiOutcome, GitHubRequestError, GitHubRequestRefused

logger = get_logger(__name__)

# A page-size/limit pair generous enough for any real PR's review history
# while still bounding a runaway paginator (mirrors GitHubClient's own
# COMMENTS_MAX_PAGES-style guard for review listings in gh_cache.py).
REVIEW_LIST_PAGE_SIZE = 100
REVIEW_LIST_MAX_PAGES = 50

# REQ-003: COMMENTED/PENDING reviews never override a principal's last
# decisive verdict.
_DECISIVE_REVIEW_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED", "DISMISSED"})


class AdapterOutcomeKind(str, Enum):
    """The typed shape of one adapter call's result (REQ-001)."""

    # No execution right was granted: the effect was already not_needed,
    # already confirmed_complete, or otherwise not currently retryable
    # (delivery_unknown/confirmed_rejected awaiting explicit handling, or the
    # operation itself is blocked/superseded/merge_confirmed). No wire call
    # of any kind is made.
    ALREADY_SATISFIED = "already_satisfied"
    CONFIRMED_COMPLETE = "confirmed_complete"
    DEFERRED = "deferred"
    OPERATIONALLY_BLOCKED = "operationally_blocked"
    DEFINITIVE_REJECTION = "definitive_rejection"
    INDETERMINATE = "indeterminate"
    SUPERSEDED = "superseded"
    # A reconcile_* call found the targeted effect was not delivery_unknown;
    # there is nothing to reconcile.
    NO_RECONCILIATION_NEEDED = "no_reconciliation_needed"


@dataclass(frozen=True)
class AdapterResult:
    """One adapter call's typed, correlated result (REQ-001)."""

    kind: AdapterOutcomeKind
    operation: MergeOperation
    http_status: Optional[int] = None
    delivery: str = ""
    reason: str = ""
    retry_at: Optional[float] = None


class MergeOperationAdapterError(RuntimeError):
    """Raised for a caller usage error (e.g. no operation exists yet)."""


@dataclass(frozen=True)
class _ExistingApproval:
    review_id: int
    reviewer_identity: str
    head_sha: str


def _owner_repo(identity: MergeOperationIdentity) -> tuple[str, str]:
    owner, _, repo = identity.repository.partition("/")
    return owner, repo


def _list_all_reviews(api: Any, owner: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
    """Fetch every review page in GitHub's own chronological order (REQ-003).

    Any failure (including a real or local throttle deferral) partway
    through propagates to the caller unchanged: a partial page never counts
    as authoritative evidence that no valid approval exists.
    """
    reviews: list[dict[str, Any]] = []
    page = 1
    while page <= REVIEW_LIST_MAX_PAGES:
        batch = api.pulls.list_reviews(owner, repo, pr_number, per_page=REVIEW_LIST_PAGE_SIZE, page=page)
        if not batch:
            break
        reviews.extend(batch)
        if len(batch) < REVIEW_LIST_PAGE_SIZE:
            break
        page += 1
    else:
        raise MergeOperationAdapterError(f"Review listing for {owner}/{repo}#{pr_number} exceeded {REVIEW_LIST_MAX_PAGES} pages")
    return reviews


def _find_valid_approval(api: Any, owner: str, repo: str, pr_number: int, expected_head_sha: str, reviewer_identity: str) -> Optional[_ExistingApproval]:
    """Return the reviewer's current valid approval, or None (REQ-003/REQ-004).

    Valid means: combining every page in GitHub's own order, this
    principal's *last decisive* review (ignoring COMMENTED/PENDING) is
    APPROVED with ``commit_id`` equal to ``expected_head_sha``. A missing
    principal, a different principal, a different head, or a last decisive
    verdict of CHANGES_REQUESTED/DISMISSED are never approval evidence.
    """
    if not reviewer_identity:
        return None
    reviews = _list_all_reviews(api, owner, repo, pr_number)
    last_decisive: Optional[dict[str, Any]] = None
    for review in reviews:
        user = review.get("user") or {}
        if user.get("login") != reviewer_identity:
            continue
        if review.get("state") not in _DECISIVE_REVIEW_STATES:
            continue
        last_decisive = review
    if last_decisive is None:
        return None
    if last_decisive.get("state") != "APPROVED":
        return None
    if last_decisive.get("commit_id") != expected_head_sha:
        return None
    review_id = last_decisive.get("id")
    if not review_id:
        return None
    return _ExistingApproval(review_id=review_id, reviewer_identity=reviewer_identity, head_sha=expected_head_sha)


def _require_operation(store: MergeOperationStore, identity: MergeOperationIdentity) -> MergeOperation:
    operation = store.get(identity)
    if operation is None:
        raise MergeOperationAdapterError(f"No merge operation exists for {identity.key()}; call MergeOperationStore.get_or_create first")
    return operation


def _resolve(store: MergeOperationStore, identity: MergeOperationIdentity, fallback: MergeOperation) -> MergeOperation:
    return store.get(identity) or fallback


def _record_refusal(store: MergeOperationStore, identity: MergeOperationIdentity, effect_name: EffectName, attempt_id: str, generation: int, exc: GitHubRequestRefused, fallback: MergeOperation) -> AdapterResult:
    """A local admission refusal: definitely not sent (REQ-001/REQ-006)."""
    reason = getattr(exc, "reason", "") or (exc.outcome.message or "refused")
    retry_at = getattr(exc, "retry_at", None)
    operation = store.defer_local(identity, effect_name, attempt_id, generation, is_real_throttle=False, retry_after_seconds=0.0, governor_deadline=retry_at, detail=reason)
    resolved = operation or _resolve(store, identity, fallback)
    return AdapterResult(AdapterOutcomeKind.DEFERRED, resolved, delivery="definitely_not_sent", reason=reason, retry_at=resolved.not_before)


def _record_classified_error(store: MergeOperationStore, identity: MergeOperationIdentity, effect_name: EffectName, attempt_id: str, generation: int, exc: GitHubRequestError, fallback: MergeOperation) -> AdapterResult:
    """Classify one received (non-refusal) HTTP outcome into a terminal or deferred result."""
    outcome = exc.outcome
    classification = outcome.classification
    status = outcome.status
    detail = outcome.message or str(exc)

    if classification is GitHubApiOutcome.AUTHENTICATION_FAILURE:
        store.operationally_block(identity, effect_name, attempt_id, generation, BlockReason.AUTHENTICATION, detail=detail)
        return AdapterResult(AdapterOutcomeKind.OPERATIONALLY_BLOCKED, _resolve(store, identity, fallback), http_status=status, delivery="http_response_received", reason="authentication_failure")

    if classification is GitHubApiOutcome.FORBIDDEN:
        store.operationally_block(identity, effect_name, attempt_id, generation, BlockReason.FORBIDDEN, detail=detail)
        return AdapterResult(AdapterOutcomeKind.OPERATIONALLY_BLOCKED, _resolve(store, identity, fallback), http_status=status, delivery="http_response_received", reason="forbidden")

    if classification in (GitHubApiOutcome.PRIMARY_THROTTLED, GitHubApiOutcome.SECONDARY_THROTTLED, GitHubApiOutcome.THROTTLED):
        retry_after = outcome.metadata.retry_after_seconds or 0.0
        operation = store.defer_local(identity, effect_name, attempt_id, generation, is_real_throttle=True, throttle_attempt_id=outcome.context.attempt_id, retry_after_seconds=retry_after, detail=detail)
        resolved = operation or _resolve(store, identity, fallback)
        return AdapterResult(AdapterOutcomeKind.DEFERRED, resolved, http_status=status, delivery="http_response_received", reason=classification.value, retry_at=resolved.not_before)

    if classification is GitHubApiOutcome.TRANSPORT_FAILURE:
        store.record_delivery_unknown(identity, effect_name, attempt_id, generation, detail=detail)
        return AdapterResult(AdapterOutcomeKind.INDETERMINATE, _resolve(store, identity, fallback), http_status=status, delivery=outcome.delivery.value, reason="transport_failure")

    if classification is GitHubApiOutcome.REMOTE_ERROR:
        # REQ-006: a synchronous merge PUT that reports 409 means the
        # expected head no longer matches -- the mutation was rejected
        # (definitely not applied), and the whole operation is stale until a
        # fresh expected head is established via a new get_or_create call.
        if status == 409 and effect_name is EffectName.MERGE:
            store.record_confirmed_unsent(identity, effect_name, attempt_id, generation, detail="expected_head_mismatch")
            store.supersede(identity)
            return AdapterResult(AdapterOutcomeKind.SUPERSEDED, _resolve(store, identity, fallback), http_status=409, delivery="http_response_received", reason="expected_head_mismatch")
        if status is not None and status >= 500:
            # A mutation 5xx never proves the effect was not applied.
            store.record_delivery_unknown(identity, effect_name, attempt_id, generation, detail=detail)
            return AdapterResult(AdapterOutcomeKind.INDETERMINATE, _resolve(store, identity, fallback), http_status=status, delivery="http_response_received", reason="mutation_server_error")
        # 405 (cause-unspecified), 422 without throttle evidence (classify_response
        # already routed real throttle evidence to the branch above), and any
        # other unclassified 4xx are definitive, cause-unspecified rejections:
        # REQ-006 forbids treating any of these as license to try a different
        # method, endpoint, or credential.
        store.record_confirmed_rejected(identity, effect_name, attempt_id, generation, detail=detail)
        return AdapterResult(AdapterOutcomeKind.DEFINITIVE_REJECTION, _resolve(store, identity, fallback), http_status=status, delivery="http_response_received", reason="definitive_rejection")

    # Any residual classification (e.g. REFUSED reaching this branch through
    # a non-governor admission hook) is treated conservatively as a
    # definitely-not-sent local deferral rather than guessed as delivered.
    operation = store.defer_local(identity, effect_name, attempt_id, generation, is_real_throttle=False, retry_after_seconds=0.0, detail=detail)
    resolved = operation or _resolve(store, identity, fallback)
    return AdapterResult(AdapterOutcomeKind.DEFERRED, resolved, http_status=status, reason=classification.value, retry_at=resolved.not_before)


def _record_unclassified_exception(store: MergeOperationStore, identity: MergeOperationIdentity, effect_name: EffectName, attempt_id: str, generation: int, exc: Exception, fallback: MergeOperation) -> AdapterResult:
    """A raw transport/body-read failure not wrapped as ``GitHubRequestError``.

    The status line may have arrived (e.g. a body-read failure) or nothing
    may have arrived at all; either way, mutation delivery cannot be denied
    (REQ-007), so this is always recorded as delivery_unknown, never guessed
    as unsent.
    """
    store.record_delivery_unknown(identity, effect_name, attempt_id, generation, detail=str(exc)[:500])
    return AdapterResult(AdapterOutcomeKind.INDETERMINATE, _resolve(store, identity, fallback), delivery="indeterminate_after_possible_send", reason="unclassified_exception")


def _dispatch_mutation_failure(store: MergeOperationStore, identity: MergeOperationIdentity, effect_name: EffectName, attempt_id: str, generation: int, exc: Exception, fallback: MergeOperation) -> AdapterResult:
    if isinstance(exc, GitHubRequestRefused):
        return _record_refusal(store, identity, effect_name, attempt_id, generation, exc, fallback)
    if isinstance(exc, GitHubRequestError):
        return _record_classified_error(store, identity, effect_name, attempt_id, generation, exc, fallback)
    return _record_unclassified_exception(store, identity, effect_name, attempt_id, generation, exc, fallback)


def attempt_approval(store: MergeOperationStore, token: str, identity: MergeOperationIdentity) -> AdapterResult:
    """Perform one approval attempt: reserve, check-then-post, record (REQ-002/003/004/005)."""
    operation = _require_operation(store, identity)
    reservation = store.reserve_attempt(identity, EffectName.APPROVAL)
    if not reservation.granted:
        return AdapterResult(AdapterOutcomeKind.ALREADY_SATISFIED, reservation.operation)

    attempt_id, generation = reservation.attempt_id, reservation.generation
    owner, repo = _owner_repo(identity)
    api = get_ghapi_client(token)

    try:
        existing = _find_valid_approval(api, owner, repo, identity.pr_number, operation.expected_head_sha, operation.reviewer_identity)
    except Exception as exc:  # noqa: BLE001 - classified by _dispatch_mutation_failure
        return _dispatch_mutation_failure(store, identity, EffectName.APPROVAL, attempt_id, generation, exc, operation)

    if existing is not None:
        # REQ-003/REQ-004: a pre-existing valid approval satisfies the
        # effect via state observation without sending a new POST. This is
        # evidence the *goal* is already achieved, not a causal receipt for
        # some earlier lost POST -- recorded with a distinct
        # ConfirmationSource accordingly.
        receipt = EffectReceipt(ConfirmationSource.STATE_OBSERVATION, review_id=str(existing.review_id), reviewer_identity=existing.reviewer_identity, target_head_sha=existing.head_sha)
        store.record_confirmed_complete(identity, EffectName.APPROVAL, attempt_id, generation, receipt)
        return AdapterResult(AdapterOutcomeKind.CONFIRMED_COMPLETE, _resolve(store, identity, operation), reason="existing_approval_observed")

    try:
        response = api.pulls.create_review(owner, repo, identity.pr_number, event="APPROVE", commit_id=operation.expected_head_sha)
    except Exception as exc:  # noqa: BLE001 - classified by _dispatch_mutation_failure
        return _dispatch_mutation_failure(store, identity, EffectName.APPROVAL, attempt_id, generation, exc, operation)

    review_id = response.get("id") if isinstance(response, dict) else None
    state = response.get("state") if isinstance(response, dict) else None
    commit_id = response.get("commit_id") if isinstance(response, dict) else None
    login = (response.get("user") or {}).get("login") if isinstance(response, dict) else None

    if review_id and state == "APPROVED" and commit_id == operation.expected_head_sha:
        # REQ-005: normal approval success requires a response for the
        # target head with a valid review id/state.
        receipt = EffectReceipt(ConfirmationSource.OWN_RESPONSE, review_id=str(review_id), reviewer_identity=login or operation.reviewer_identity, target_head_sha=operation.expected_head_sha)
        store.record_confirmed_complete(identity, EffectName.APPROVAL, attempt_id, generation, receipt)
        return AdapterResult(AdapterOutcomeKind.CONFIRMED_COMPLETE, _resolve(store, identity, operation), http_status=200, delivery="http_response_received", reason="own_response")

    # An HTTP success whose body is incomplete or contradicts the target
    # (wrong head/state/missing id) is never fabricated into a receipt
    # (REQ-005/AS-006); the request was sent, so this is indeterminate, not
    # unsent.
    store.record_delivery_unknown(identity, EffectName.APPROVAL, attempt_id, generation, detail="incomplete_or_mismatched_review_response")
    return AdapterResult(AdapterOutcomeKind.INDETERMINATE, _resolve(store, identity, operation), http_status=200, delivery="http_response_received", reason="incomplete_or_mismatched_review_response")


def attempt_merge(store: MergeOperationStore, token: str, identity: MergeOperationIdentity) -> AdapterResult:
    """Perform one merge attempt: reserve, send, record (REQ-002/005/006/007)."""
    operation = _require_operation(store, identity)
    reservation = store.reserve_attempt(identity, EffectName.MERGE)
    if not reservation.granted:
        return AdapterResult(AdapterOutcomeKind.ALREADY_SATISFIED, reservation.operation)

    attempt_id, generation = reservation.attempt_id, reservation.generation
    owner, repo = _owner_repo(identity)
    api = get_ghapi_client(token)

    try:
        response = api.pulls.merge(owner, repo, identity.pr_number, merge_method=operation.merge_method, sha=operation.expected_head_sha)
    except Exception as exc:  # noqa: BLE001 - classified by _dispatch_mutation_failure
        return _dispatch_mutation_failure(store, identity, EffectName.MERGE, attempt_id, generation, exc, operation)

    merged = isinstance(response, dict) and response.get("merged") is True
    merge_commit_sha = response.get("sha") if isinstance(response, dict) else None

    if merged and merge_commit_sha:
        # REQ-005: normal merge success requires HTTP success with
        # merged=true and a non-empty merge commit SHA.
        receipt = EffectReceipt(ConfirmationSource.OWN_RESPONSE, target_head_sha=operation.expected_head_sha, merge_commit_sha=str(merge_commit_sha))
        store.record_confirmed_complete(identity, EffectName.MERGE, attempt_id, generation, receipt)
        return AdapterResult(AdapterOutcomeKind.CONFIRMED_COMPLETE, _resolve(store, identity, operation), http_status=200, delivery="http_response_received", reason="own_response")

    # merged=false or a missing SHA in an HTTP-success body is an incomplete
    # success, never a confirmed failure (AS-006): the request was sent.
    store.record_delivery_unknown(identity, EffectName.MERGE, attempt_id, generation, detail="incomplete_merge_response")
    return AdapterResult(AdapterOutcomeKind.INDETERMINATE, _resolve(store, identity, operation), http_status=200, delivery="http_response_received", reason="incomplete_merge_response")


def reconcile_approval(store: MergeOperationStore, token: str, identity: MergeOperationIdentity) -> AdapterResult:
    """Resolve a delivery_unknown approval via a fresh, explicit read-only check (REQ-003/004/007).

    This never sends a new POST. It only ever makes progress in the positive
    direction: a currently observed valid approval for the same
    principal/head satisfies the effect. An unreachable/partial listing, or
    one that shows no valid approval (including one later dismissed),
    changes nothing -- the original mutation's delivery uncertainty is
    preserved rather than guessed at either way.
    """
    operation = _require_operation(store, identity)
    effect = operation.effect(EffectName.APPROVAL)
    if effect.state is not EffectState.DELIVERY_UNKNOWN:
        return AdapterResult(AdapterOutcomeKind.NO_RECONCILIATION_NEEDED, operation)

    owner, repo = _owner_repo(identity)
    api = get_ghapi_client(token)
    try:
        existing = _find_valid_approval(api, owner, repo, identity.pr_number, operation.expected_head_sha, operation.reviewer_identity)
    except Exception as exc:  # noqa: BLE001 - the reconciliation read itself is inconclusive
        logger.info("Approval reconciliation read failed for {}: {}", identity.key(), exc)
        return AdapterResult(AdapterOutcomeKind.INDETERMINATE, operation, reason="reconciliation_read_failed")

    if existing is None:
        return AdapterResult(AdapterOutcomeKind.INDETERMINATE, operation, reason="no_matching_approval_observed")

    receipt = EffectReceipt(ConfirmationSource.STATE_OBSERVATION, review_id=str(existing.review_id), reviewer_identity=existing.reviewer_identity, target_head_sha=existing.head_sha)
    store.record_confirmed_complete(identity, EffectName.APPROVAL, effect.attempt_id, effect.generation, receipt)
    return AdapterResult(AdapterOutcomeKind.CONFIRMED_COMPLETE, _resolve(store, identity, operation), reason="state_observation")


def reconcile_merge(store: MergeOperationStore, token: str, identity: MergeOperationIdentity) -> AdapterResult:
    """Resolve a delivery_unknown merge via a fresh, authoritative PR read (REQ-005/007/008).

    Confirms via a fresh ``GET /pulls/{number}``: ``merged=true``, the PR's
    current head still matches the expected head this operation targeted,
    and a non-empty ``merge_commit_sha``. An external actor's merge counts
    equally -- this never claims the merge as this operation's own request.
    A different (later) head that got merged is never treated as this
    operation's success. The squash/rebase merge commit SHA is never
    required to equal the head SHA (that would reject every real squash or
    rebase merge). An open/unmerged/unreadable observation never flips the
    effect back to confirmed_unsent.
    """
    operation = _require_operation(store, identity)
    effect = operation.effect(EffectName.MERGE)
    if effect.state is not EffectState.DELIVERY_UNKNOWN:
        return AdapterResult(AdapterOutcomeKind.NO_RECONCILIATION_NEEDED, operation)

    owner, repo = _owner_repo(identity)
    api = get_ghapi_client(token)
    try:
        pr_info = api.pulls.get(owner, repo, identity.pr_number)
    except Exception as exc:  # noqa: BLE001 - the reconciliation read itself is inconclusive
        logger.info("Merge reconciliation read failed for {}: {}", identity.key(), exc)
        return AdapterResult(AdapterOutcomeKind.INDETERMINATE, operation, reason="reconciliation_read_failed")

    merged = isinstance(pr_info, dict) and pr_info.get("merged") is True
    head_sha = (pr_info.get("head") or {}).get("sha") if isinstance(pr_info, dict) else None
    merge_commit_sha = pr_info.get("merge_commit_sha") if isinstance(pr_info, dict) else None

    if merged and head_sha == operation.expected_head_sha and merge_commit_sha:
        receipt = EffectReceipt(ConfirmationSource.STATE_OBSERVATION, target_head_sha=operation.expected_head_sha, merge_commit_sha=str(merge_commit_sha))
        store.record_confirmed_complete(identity, EffectName.MERGE, effect.attempt_id, effect.generation, receipt)
        return AdapterResult(AdapterOutcomeKind.CONFIRMED_COMPLETE, _resolve(store, identity, operation), reason="state_observation")

    return AdapterResult(AdapterOutcomeKind.INDETERMINATE, operation, reason="not_confirmed_by_reconciliation")
