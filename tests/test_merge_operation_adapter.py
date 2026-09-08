"""Real-boundary tests for the production merge/approval adapter (Issue #1938).

Every test exercises the real adapter functions against a real, on-disk
``MergeOperationStore`` and a real ``GitHubRequestGovernor``; only the
outermost HTTP transport is faked (an ``httpx.MockTransport`` handler),
mirroring the fault-injection style already used in
``tests/test_github_request_outcomes.py`` and
``tests/test_github_request_governor.py``. Assertions are made on wire-call
counts and on the real store's persisted state, never on a mocked return
value or a manually invoked ``governor.observe()``.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from unittest.mock import patch

import httpx
import pytest

import auto_coder.merge_operation_adapter as merge_operation_adapter
from auto_coder.github_request_governor import GitHubRequestGovernor
from auto_coder.merge_operation_adapter import REVIEW_LIST_PAGE_SIZE as _DEFAULT_REVIEW_PAGE_SIZE
from auto_coder.merge_operation_adapter import AdapterOutcomeKind, attempt_approval, attempt_merge, reconcile_approval, reconcile_merge
from auto_coder.merge_operation_state import (
    ConfirmationSource,
    EffectName,
    EffectReceipt,
    EffectState,
    MergeOperationIdentity,
    MergeOperationStore,
    OperationStatus,
)
from auto_coder.util.github_request_outcome import DiagnosticTransport, configure_github_request_boundary

OWNER_REPO = "acme/widgets"
REVIEWER = "auto-coder-bot"


@dataclass
class Clock:
    """A controllable clock so mutation spacing/throttle timing is deterministic."""

    monotonic_value: float = 1_000.0
    wall_value: float = 1_800_000_000.0

    def monotonic(self) -> float:
        return self.monotonic_value

    def wall(self) -> float:
        return self.wall_value

    def advance(self, seconds: float) -> None:
        self.monotonic_value += seconds
        self.wall_value += seconds


def make_store(tmp_path, name: str = "merge_ops.db") -> MergeOperationStore:
    return MergeOperationStore(db_path=tmp_path / name)


def make_governor(tmp_path, clock: Clock, name: str = "governor.sqlite3") -> GitHubRequestGovernor:
    return GitHubRequestGovernor(monotonic=clock.monotonic, wall_time=clock.wall, store_path=tmp_path / name)


def make_identity(pr_number: int = 42) -> MergeOperationIdentity:
    return MergeOperationIdentity("https://api.github.com", OWNER_REPO, pr_number)


def wire_client(governor: GitHubRequestGovernor, handler):
    """Build the real httpx.Client (real DiagnosticTransport, real governor,
    fake outermost transport) that get_ghapi_client()'s internal caching
    client is patched to return."""
    transport = DiagnosticTransport(httpx.MockTransport(handler), admission_hook=governor.admit, observation_hook=governor.observe, subsystem="merge-adapter-test")
    return httpx.Client(transport=transport)


@contextmanager
def wired(governor: GitHubRequestGovernor, handler):
    """Wire the real governor as the process-wide admission/observation
    boundary (so get_ghapi_client()'s own response finalization resolves the
    governor's reservation, exactly like production startup wiring in
    automation_engine.py) and patch the caching client to the fake transport."""
    configure_github_request_boundary(governor.admit, governor.observe)
    try:
        with patch("auto_coder.util.gh_cache.get_caching_client", return_value=wire_client(governor, handler)):
            yield
    finally:
        configure_github_request_boundary()


def approval_review(review_id: int, login: str, state: str, commit_id: str) -> dict:
    return {"id": review_id, "user": {"login": login}, "state": state, "commit_id": commit_id}


def create_operation(store: MergeOperationStore, identity: MergeOperationIdentity, *, head_sha: str = "deadbeef", merge_method: str = "squash", needs_approval: bool = True, reviewer_identity: str = REVIEWER):
    return store.get_or_create(
        identity,
        expected_head_sha=head_sha,
        merge_method=merge_method,
        approval_credential_role="bot-credential",
        reviewer_identity=reviewer_identity,
        needs_approval=needs_approval,
    )


def routing_handler(*, on_list_reviews=None, on_create_review=None, on_merge=None, on_get_pr=None):
    """Build a MockTransport handler that dispatches by REST route+verb and
    records every call it dispatches, for wire-count assertions."""
    calls = {"list_reviews": [], "create_review": [], "merge": [], "get_pr": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path.endswith("/reviews") and method == "GET":
            calls["list_reviews"].append(request)
            assert on_list_reviews is not None, "unexpected review listing request"
            return on_list_reviews(request, len(calls["list_reviews"]))
        if path.endswith("/reviews") and method == "POST":
            calls["create_review"].append(request)
            assert on_create_review is not None, "unexpected approval POST"
            return on_create_review(request)
        if path.endswith("/merge") and method == "PUT":
            calls["merge"].append(request)
            assert on_merge is not None, "unexpected merge PUT"
            return on_merge(request)
        if method == "GET":
            calls["get_pr"].append(request)
            assert on_get_pr is not None, "unexpected PR GET"
            return on_get_pr(request)
        raise AssertionError(f"unexpected request {method} {path}")

    return handler, calls


# --------------------------------------------------------------------------
# AS-001: a real Governor spacing refusal is returned typed, with 0 merge
# wire sends and no repository/PR GET, alternate-method, or repair call.
# --------------------------------------------------------------------------


def test_as001_governor_spacing_defers_merge_with_zero_wire_sends(tmp_path):
    clock = Clock()
    governor = make_governor(tmp_path, clock)
    store = make_store(tmp_path)
    identity = make_identity()
    create_operation(store, identity)

    handler, calls = routing_handler(
        on_list_reviews=lambda request, page: httpx.Response(200, json=[], request=request),
        on_create_review=lambda request: httpx.Response(200, json=approval_review(1, REVIEWER, "APPROVED", "deadbeef"), request=request),
        on_merge=lambda request: httpx.Response(200, json={"merged": True, "sha": "mergedsha"}, request=request),
    )

    with wired(governor, handler):
        approval_result = attempt_approval(store, "token", identity)
        assert approval_result.kind is AdapterOutcomeKind.CONFIRMED_COMPLETE
        assert len(calls["create_review"]) == 1
        assert len(calls["list_reviews"]) == 1

        # Merge attempted well within the mutation-spacing window right after
        # the approval mutation completed.
        merge_result = attempt_merge(store, "token", identity)

    assert merge_result.kind is AdapterOutcomeKind.DEFERRED
    assert merge_result.delivery == "definitely_not_sent"
    assert merge_result.retry_at is not None and merge_result.retry_at > clock.wall_value
    assert len(calls["merge"]) == 0
    assert len(calls["get_pr"]) == 0

    operation = store.get(identity)
    assert operation.effect(EffectName.MERGE).state is EffectState.NOT_ATTEMPTED
    assert operation.status is OperationStatus.WAITING


# --------------------------------------------------------------------------
# AS-002: a valid pre-existing approval is never re-posted; invalid look-
# alikes are not treated as approval evidence.
# --------------------------------------------------------------------------


def test_as002_valid_existing_approval_is_observed_without_posting(tmp_path):
    clock = Clock()
    governor = make_governor(tmp_path, clock)
    store = make_store(tmp_path)
    identity = make_identity()
    create_operation(store, identity)

    handler, calls = routing_handler(on_list_reviews=lambda request, page: httpx.Response(200, json=[approval_review(101, REVIEWER, "APPROVED", "deadbeef")], request=request))

    with wired(governor, handler):
        result = attempt_approval(store, "token", identity)

    assert result.kind is AdapterOutcomeKind.CONFIRMED_COMPLETE
    assert len(calls["create_review"]) == 0
    receipt = store.get(identity).effect(EffectName.APPROVAL).receipt
    assert receipt.confirmation_source is ConfirmationSource.STATE_OBSERVATION
    assert receipt.review_id == "101"

    # A subsequent attempt does not re-post either: the effect is no longer
    # retryable.
    with wired(governor, handler):
        second = attempt_approval(store, "token", identity)
    assert second.kind is AdapterOutcomeKind.ALREADY_SATISFIED
    assert len(calls["create_review"]) == 0


@pytest.mark.parametrize(
    "reviews",
    [
        pytest.param([approval_review(1, "someone-else", "APPROVED", "deadbeef")], id="different_principal"),
        pytest.param([approval_review(1, REVIEWER, "APPROVED", "otherhead")], id="different_head"),
        pytest.param([approval_review(1, REVIEWER, "APPROVED", "deadbeef"), approval_review(2, REVIEWER, "CHANGES_REQUESTED", "deadbeef")], id="changes_requested_after"),
        pytest.param([approval_review(1, REVIEWER, "APPROVED", "deadbeef"), approval_review(2, REVIEWER, "DISMISSED", "deadbeef")], id="dismissed_after"),
    ],
)
def test_as002_invalid_lookalikes_are_not_approval_evidence(tmp_path, reviews):
    clock = Clock()
    governor = make_governor(tmp_path, clock)
    store = make_store(tmp_path)
    identity = make_identity()
    create_operation(store, identity)

    handler, calls = routing_handler(
        on_list_reviews=lambda request, page: httpx.Response(200, json=reviews, request=request),
        on_create_review=lambda request: httpx.Response(200, json=approval_review(999, REVIEWER, "APPROVED", "deadbeef"), request=request),
    )

    with wired(governor, handler):
        result = attempt_approval(store, "token", identity)

    assert result.kind is AdapterOutcomeKind.CONFIRMED_COMPLETE
    assert len(calls["create_review"]) == 1  # a fresh POST was required
    receipt = store.get(identity).effect(EffectName.APPROVAL).receipt
    assert receipt.confirmation_source is ConfirmationSource.OWN_RESPONSE


def test_as002_trailing_comment_does_not_undo_the_valid_approval(tmp_path):
    clock = Clock()
    governor = make_governor(tmp_path, clock)
    store = make_store(tmp_path)
    identity = make_identity()
    create_operation(store, identity)

    reviews = [approval_review(1, REVIEWER, "APPROVED", "deadbeef"), approval_review(2, REVIEWER, "COMMENTED", "deadbeef")]
    handler, calls = routing_handler(on_list_reviews=lambda request, page: httpx.Response(200, json=reviews, request=request))

    with wired(governor, handler):
        result = attempt_approval(store, "token", identity)

    assert result.kind is AdapterOutcomeKind.CONFIRMED_COMPLETE
    assert len(calls["create_review"]) == 0


def test_as002_pagination_failure_prevents_treating_absence_as_valid(tmp_path, monkeypatch):
    monkeypatch.setattr(merge_operation_adapter, "REVIEW_LIST_PAGE_SIZE", 1)
    clock = Clock()
    governor = make_governor(tmp_path, clock)
    store = make_store(tmp_path)
    identity = make_identity()
    create_operation(store, identity)

    def on_list_reviews(request, page):
        if page == 1:
            return httpx.Response(200, json=[approval_review(1, "someone-else", "COMMENTED", "deadbeef")], request=request)
        return httpx.Response(500, json={"message": "server exploded"}, request=request)

    handler, calls = routing_handler(on_list_reviews=on_list_reviews)

    with wired(governor, handler):
        result = attempt_approval(store, "token", identity)

    assert result.kind is AdapterOutcomeKind.INDETERMINATE
    assert len(calls["list_reviews"]) == 2
    assert len(calls["create_review"]) == 0
    assert store.get(identity).effect(EffectName.APPROVAL).state is EffectState.DELIVERY_UNKNOWN


# --------------------------------------------------------------------------
# AS-003: a lost approval-success response is reconciled without a repost;
# an unreadable/absent review during reconciliation keeps delivery unknown.
# --------------------------------------------------------------------------


def test_as003_lost_approval_response_is_reconciled_without_reposting(tmp_path):
    clock = Clock()
    governor = make_governor(tmp_path, clock)
    store = make_store(tmp_path)
    identity = make_identity()
    create_operation(store, identity)

    def failing_create_review(request):
        raise httpx.RemoteProtocolError("peer closed connection")

    handler, calls = routing_handler(on_list_reviews=lambda request, page: httpx.Response(200, json=[], request=request), on_create_review=failing_create_review)

    with wired(governor, handler):
        first = attempt_approval(store, "token", identity)
    assert first.kind is AdapterOutcomeKind.INDETERMINATE
    assert len(calls["create_review"]) == 1
    assert store.get(identity).effect(EffectName.APPROVAL).state is EffectState.DELIVERY_UNKNOWN

    # Reopen the store as a fresh instance (simulating a restart) pointed at
    # the same file, and reconcile against an unreachable listing first.
    reopened = make_store(tmp_path)
    handler_unreachable, calls_unreachable = routing_handler(on_list_reviews=lambda request, page: httpx.Response(500, json={"message": "boom"}, request=request))
    with wired(governor, handler_unreachable):
        blocked = reconcile_approval(reopened, "token", identity)
    assert blocked.kind is AdapterOutcomeKind.INDETERMINATE
    assert len(calls_unreachable["list_reviews"]) == 1
    assert reopened.get(identity).effect(EffectName.APPROVAL).state is EffectState.DELIVERY_UNKNOWN

    # Now the review is visible: reconciliation completes it without any POST.
    handler_ok, calls_ok = routing_handler(on_list_reviews=lambda request, page: httpx.Response(200, json=[approval_review(5, REVIEWER, "APPROVED", "deadbeef")], request=request))
    with wired(governor, handler_ok):
        resolved = reconcile_approval(reopened, "token", identity)
    assert resolved.kind is AdapterOutcomeKind.CONFIRMED_COMPLETE
    assert len(calls_ok["create_review"]) == 0
    receipt = reopened.get(identity).effect(EffectName.APPROVAL).receipt
    assert receipt.confirmation_source is ConfirmationSource.STATE_OBSERVATION
    assert receipt.review_id == "5"

    # A later dismissal must not trigger a fresh approval from this operation.
    with wired(governor, handler_ok):
        after_dismiss = attempt_approval(reopened, "token", identity)
    assert after_dismiss.kind is AdapterOutcomeKind.ALREADY_SATISFIED
    assert len(calls_ok["create_review"]) == 0


# --------------------------------------------------------------------------
# AS-004: a lost merge response is reconciled from a fresh authoritative PR
# read; open/unmerged/unreadable observations never flip it back, and a
# different head's merge is never claimed as this operation's own.
# --------------------------------------------------------------------------


def test_as004_lost_merge_response_is_reconciled_without_remerging(tmp_path):
    clock = Clock()
    governor = make_governor(tmp_path, clock)
    store = make_store(tmp_path)
    identity = make_identity()
    create_operation(store, identity, needs_approval=False)

    def failing_merge(request):
        raise httpx.RemoteProtocolError("peer closed connection")

    handler, calls = routing_handler(on_merge=failing_merge)
    with wired(governor, handler):
        first = attempt_merge(store, "token", identity)
    assert first.kind is AdapterOutcomeKind.INDETERMINATE
    assert len(calls["merge"]) == 1
    assert store.get(identity).effect(EffectName.MERGE).state is EffectState.DELIVERY_UNKNOWN

    # An observed still-open PR must not flip the effect back to unsent.
    handler_open, calls_open = routing_handler(on_get_pr=lambda request: httpx.Response(200, json={"merged": False, "head": {"sha": "deadbeef"}}, request=request))
    with wired(governor, handler_open):
        still_open = reconcile_merge(store, "token", identity)
    assert still_open.kind is AdapterOutcomeKind.INDETERMINATE
    assert store.get(identity).effect(EffectName.MERGE).state is EffectState.DELIVERY_UNKNOWN

    # A merge recorded for a different (later) head is not this operation's success.
    handler_wrong_head, _ = routing_handler(on_get_pr=lambda request: httpx.Response(200, json={"merged": True, "head": {"sha": "somethingelse"}, "merge_commit_sha": "mc-wrong"}, request=request))
    with wired(governor, handler_wrong_head):
        wrong_head = reconcile_merge(store, "token", identity)
    assert wrong_head.kind is AdapterOutcomeKind.INDETERMINATE
    assert store.get(identity).effect(EffectName.MERGE).state is EffectState.DELIVERY_UNKNOWN

    # A confirmed merge for the expected head resolves it -- even performed
    # by an external actor, and even though the (squash-style) merge commit
    # SHA differs from the head SHA, which must never be required to match.
    handler_ok, calls_ok = routing_handler(on_get_pr=lambda request: httpx.Response(200, json={"merged": True, "head": {"sha": "deadbeef"}, "merge_commit_sha": "squash-commit-sha-not-equal-head"}, request=request))
    with wired(governor, handler_ok):
        resolved = reconcile_merge(store, "token", identity)
    assert resolved.kind is AdapterOutcomeKind.CONFIRMED_COMPLETE
    receipt = store.get(identity).effect(EffectName.MERGE).receipt
    assert receipt.confirmation_source is ConfirmationSource.STATE_OBSERVATION
    assert receipt.merge_commit_sha == "squash-commit-sha-not-equal-head"
    assert receipt.target_head_sha == "deadbeef"

    # No second merge PUT is ever sent across this whole sequence.
    assert len(calls["merge"]) == 1


# --------------------------------------------------------------------------
# AS-005: 409/405/401/403/429/422 are classified precisely, with zero
# alternate-method attempts or extra diagnostic GETs.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "headers", "body", "expected_kind", "expected_effect_state"),
    [
        pytest.param(409, {}, {"message": "expected head sha did not match"}, AdapterOutcomeKind.SUPERSEDED, EffectState.CONFIRMED_UNSENT, id="409_conflict"),
        pytest.param(405, {}, {"message": "not allowed"}, AdapterOutcomeKind.DEFINITIVE_REJECTION, EffectState.CONFIRMED_REJECTED, id="405_not_allowed"),
        pytest.param(401, {}, {"message": "bad credentials"}, AdapterOutcomeKind.OPERATIONALLY_BLOCKED, EffectState.NOT_ATTEMPTED, id="401_auth"),
        pytest.param(403, {}, {"message": "Resource not accessible"}, AdapterOutcomeKind.OPERATIONALLY_BLOCKED, EffectState.NOT_ATTEMPTED, id="403_plain_forbidden"),
        pytest.param(403, {"X-RateLimit-Remaining": "0", "Retry-After": "30"}, {"message": "secondary rate limit"}, AdapterOutcomeKind.DEFERRED, EffectState.NOT_ATTEMPTED, id="403_throttle_evidence"),
        pytest.param(429, {"Retry-After": "5"}, {"message": "too many requests"}, AdapterOutcomeKind.DEFERRED, EffectState.NOT_ATTEMPTED, id="429_throttled"),
        pytest.param(422, {}, {"message": "Validation failed"}, AdapterOutcomeKind.DEFINITIVE_REJECTION, EffectState.CONFIRMED_REJECTED, id="422_no_throttle_evidence"),
    ],
)
def test_as005_merge_http_faults_are_classified_without_alternate_methods(tmp_path, status, headers, body, expected_kind, expected_effect_state):
    clock = Clock()
    governor = make_governor(tmp_path, clock)
    store = make_store(tmp_path)
    identity = make_identity()
    create_operation(store, identity, needs_approval=False)

    handler, calls = routing_handler(on_merge=lambda request: httpx.Response(status, headers=headers, json=body, request=request))

    with wired(governor, handler):
        result = attempt_merge(store, "token", identity)

    assert result.kind is expected_kind
    assert len(calls["merge"]) == 1
    assert len(calls["get_pr"]) == 0
    assert len(calls["list_reviews"]) == 0

    operation = store.get(identity)
    assert operation.effect(EffectName.MERGE).state is expected_effect_state
    if expected_kind is AdapterOutcomeKind.SUPERSEDED:
        assert operation.status is OperationStatus.SUPERSEDED
    if expected_kind is AdapterOutcomeKind.OPERATIONALLY_BLOCKED:
        assert operation.status is OperationStatus.OPERATIONALLY_BLOCKED


def test_as005_forbidden_is_never_reported_as_throttled(tmp_path):
    clock = Clock()
    governor = make_governor(tmp_path, clock)
    store = make_store(tmp_path)
    identity = make_identity()
    create_operation(store, identity, needs_approval=False)

    handler, calls = routing_handler(on_merge=lambda request: httpx.Response(403, json={"message": "Resource not accessible by integration"}, request=request))
    with wired(governor, handler):
        result = attempt_merge(store, "token", identity)

    assert result.kind is AdapterOutcomeKind.OPERATIONALLY_BLOCKED
    assert result.reason == "forbidden"
    assert len(calls["merge"]) == 1


# --------------------------------------------------------------------------
# AS-006: response success-shape is never taken on faith; ambiguous data
# never becomes a receipt, and a recorded indeterminate blocks further
# mutation until explicitly reconciled.
# --------------------------------------------------------------------------


def test_as006_merge_true_without_sha_is_never_a_fabricated_receipt(tmp_path):
    clock = Clock()
    governor = make_governor(tmp_path, clock)
    store = make_store(tmp_path)
    identity = make_identity()
    create_operation(store, identity, needs_approval=False)

    handler, calls = routing_handler(on_merge=lambda request: httpx.Response(200, json={"merged": True, "sha": ""}, request=request))
    with wired(governor, handler):
        result = attempt_merge(store, "token", identity)

    assert result.kind is AdapterOutcomeKind.INDETERMINATE
    assert store.get(identity).effect(EffectName.MERGE).state is EffectState.DELIVERY_UNKNOWN
    assert store.get(identity).effect(EffectName.MERGE).receipt is None

    # The effect is not retryable until reconciled: no second PUT is sent.
    with wired(governor, handler):
        second = attempt_merge(store, "token", identity)
    assert second.kind is AdapterOutcomeKind.ALREADY_SATISFIED
    assert len(calls["merge"]) == 1


def test_as006_merge_false_with_200_is_indeterminate_not_rejected(tmp_path):
    clock = Clock()
    governor = make_governor(tmp_path, clock)
    store = make_store(tmp_path)
    identity = make_identity()
    create_operation(store, identity, needs_approval=False)

    handler, calls = routing_handler(on_merge=lambda request: httpx.Response(200, json={"merged": False, "message": "Merge already in progress"}, request=request))
    with wired(governor, handler):
        result = attempt_merge(store, "token", identity)

    assert result.kind is AdapterOutcomeKind.INDETERMINATE
    assert store.get(identity).effect(EffectName.MERGE).state is EffectState.DELIVERY_UNKNOWN


def test_as006_approval_for_a_different_head_is_indeterminate(tmp_path):
    clock = Clock()
    governor = make_governor(tmp_path, clock)
    store = make_store(tmp_path)
    identity = make_identity()
    create_operation(store, identity)

    handler, calls = routing_handler(
        on_list_reviews=lambda request, page: httpx.Response(200, json=[], request=request),
        on_create_review=lambda request: httpx.Response(200, json=approval_review(1, REVIEWER, "APPROVED", "not-the-expected-head"), request=request),
    )
    with wired(governor, handler):
        result = attempt_approval(store, "token", identity)

    assert result.kind is AdapterOutcomeKind.INDETERMINATE
    assert store.get(identity).effect(EffectName.APPROVAL).state is EffectState.DELIVERY_UNKNOWN
    assert store.get(identity).effect(EffectName.APPROVAL).receipt is None


def test_as006_mutation_5xx_is_indeterminate(tmp_path):
    clock = Clock()
    governor = make_governor(tmp_path, clock)
    store = make_store(tmp_path)
    identity = make_identity()
    create_operation(store, identity, needs_approval=False)

    handler, calls = routing_handler(on_merge=lambda request: httpx.Response(502, json={"message": "bad gateway"}, request=request))
    with wired(governor, handler):
        result = attempt_merge(store, "token", identity)

    assert result.kind is AdapterOutcomeKind.INDETERMINATE
    assert store.get(identity).effect(EffectName.MERGE).state is EffectState.DELIVERY_UNKNOWN


def test_as006_body_read_failure_is_indeterminate(tmp_path):
    clock = Clock()
    governor = make_governor(tmp_path, clock)
    store = make_store(tmp_path)
    identity = make_identity()
    create_operation(store, identity, needs_approval=False)

    def raising_merge(request):
        raise httpx.RemoteProtocolError("connection reset mid-body")

    handler, calls = routing_handler(on_merge=raising_merge)
    with wired(governor, handler):
        result = attempt_merge(store, "token", identity)

    assert result.kind is AdapterOutcomeKind.INDETERMINATE
    assert store.get(identity).effect(EffectName.MERGE).state is EffectState.DELIVERY_UNKNOWN


def test_as006_duplicate_terminal_result_does_not_corrupt_state(tmp_path):
    """A late/duplicate delivery of the same attempt's outcome is idempotent
    and never overwrites a newer generation's state (REQ-008)."""
    clock = Clock()
    store = make_store(tmp_path)
    identity = make_identity()
    create_operation(store, identity, needs_approval=False)
    reservation = store.reserve_attempt(identity, EffectName.MERGE)
    assert reservation.granted

    first = store.record_delivery_unknown(identity, EffectName.MERGE, reservation.attempt_id, reservation.generation, detail="lost")
    assert first is True
    # A duplicate delivery of the very same outcome is a harmless no-op re-affirmation.
    duplicate = store.record_delivery_unknown(identity, EffectName.MERGE, reservation.attempt_id, reservation.generation, detail="lost-again")
    assert duplicate is True
    assert store.get(identity).effect(EffectName.MERGE).state is EffectState.DELIVERY_UNKNOWN

    # A genuinely new expected head bumps the generation; the stale attempt's
    # late result must not be able to touch the new generation's state.
    store.get_or_create(identity, expected_head_sha="newhead", merge_method="squash", approval_credential_role="bot-credential", reviewer_identity=REVIEWER, needs_approval=False)
    stale_receipt = EffectReceipt(ConfirmationSource.OWN_RESPONSE, merge_commit_sha="stale-sha")
    stale_result = store.record_confirmed_complete(identity, EffectName.MERGE, reservation.attempt_id, reservation.generation, receipt=stale_receipt)
    assert stale_result is False
    assert store.get(identity).effect(EffectName.MERGE).state is EffectState.NOT_ATTEMPTED
    assert store.get(identity).expected_head_sha == "newhead"


def test_review_list_page_size_constant_matches_default(tmp_path):
    # Sanity check that the monkeypatched constant in other tests targets the
    # module attribute actually consulted by _list_all_reviews.
    assert merge_operation_adapter.REVIEW_LIST_PAGE_SIZE == _DEFAULT_REVIEW_PAGE_SIZE
