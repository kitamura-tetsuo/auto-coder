"""Tests for the automatic stale-Jules-PR recovery identity (Issue #2286).

These exercise :mod:`auto_coder.stale_jules_recovery` directly against real
``IssueStageRoutingStore``/``ImplementationSlotRepository`` instances (both
file/DB backed, no mocks) plus the real
``implementation_ownership.acquire_explicit_retry`` boundary this module's
callers hand a recovery identity to, following the same production-boundary
pattern as ``tests/test_implementation_ownership.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.auto_coder.implementation_ownership import acquire_explicit_retry
from src.auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from src.auto_coder.issue_stage_routing import IssueStageRoutingStore
from src.auto_coder.stale_jules_recovery import (
    capture_stale_jules_recovery,
    current_stale_jules_recovery_context,
    find_pending_stale_jules_recovery,
    is_recovery_request_id,
    publish_recovery_attempt_comment,
    recovery_request_id,
    stale_jules_recovery_context,
)

REPO = "owner/repo"


def _stores(tmp_path):
    slots = ImplementationSlotRepository(REPO, 1, tmp_path / "slots.json")
    routing = IssueStageRoutingStore(tmp_path / "routing.sqlite3")
    return slots, routing


# ---------------------------------------------------------------------------
# Deterministic identity
# ---------------------------------------------------------------------------


def test_recovery_request_id_is_deterministic_and_scoped_to_the_exact_pair():
    first = recovery_request_id(REPO, 42, 100)
    second = recovery_request_id(REPO, 42, 100)
    assert first == second
    assert is_recovery_request_id(first)
    assert first != recovery_request_id(REPO, 42, 101)
    assert first != recovery_request_id(REPO, 43, 100)
    assert first != recovery_request_id("owner/other", 42, 100)


def test_is_recovery_request_id_rejects_manual_retry_identities():
    assert not is_recovery_request_id("implementation-retry-deadbeef")
    assert not is_recovery_request_id("")


# ---------------------------------------------------------------------------
# capture_stale_jules_recovery / find_pending_stale_jules_recovery
# ---------------------------------------------------------------------------


def test_capture_is_idempotent_across_reprocessing(tmp_path):
    _, routing = _stores(tmp_path)
    first = capture_stale_jules_recovery(routing, REPO, 1, 100, "generation-a")
    second = capture_stale_jules_recovery(routing, REPO, 1, 100, "generation-a")
    assert first == second
    assert first is not None
    assert first.request_id == recovery_request_id(REPO, 1, 100)
    assert first.status == "pending"


def test_capture_withholds_grant_on_generation_conflict(tmp_path):
    """REQ-005: a spec change underneath an unconsumed grant permanently
    disqualifies that exact (Issue, PR) pair rather than silently rebinding."""
    _, routing = _stores(tmp_path)
    capture_stale_jules_recovery(routing, REPO, 1, 100, "generation-a")

    result = capture_stale_jules_recovery(routing, REPO, 1, 100, "generation-b")

    assert result is None
    pending = find_pending_stale_jules_recovery(routing, REPO, 1)
    assert pending is not None
    assert pending.generation == "generation-a"


def test_find_pending_returns_none_once_owned(tmp_path):
    slots, routing = _stores(tmp_path)
    owner = ImplementationOwner("issue", 1)
    authority = capture_stale_jules_recovery(routing, REPO, 1, 100, "generation-a")
    assert authority is not None
    assert find_pending_stale_jules_recovery(routing, REPO, 1) is not None

    acquire_explicit_retry(routing, slots, REPO, 1, "generation-a", authority.request_id)

    assert find_pending_stale_jules_recovery(routing, REPO, 1) is None


def test_find_pending_returns_none_once_invalidated(tmp_path):
    _, routing = _stores(tmp_path)
    authority = capture_stale_jules_recovery(routing, REPO, 1, 100, "generation-a")
    assert authority is not None

    routing.invalidate_retry_request(authority.request_id, "generation-b")

    assert find_pending_stale_jules_recovery(routing, REPO, 1) is None


def test_find_pending_ignores_manual_retry_requests(tmp_path):
    """An operator-issued --only --force --retry request must never be
    mistaken for an automatic stale-Jules recovery grant."""
    _, routing = _stores(tmp_path)
    routing.accept_retry_request("implementation-retry-abcdef", REPO, 1, "generation-a")

    assert find_pending_stale_jules_recovery(routing, REPO, 1) is None


def test_find_pending_scopes_to_the_exact_issue(tmp_path):
    _, routing = _stores(tmp_path)
    capture_stale_jules_recovery(routing, REPO, 1, 100, "generation-a")

    assert find_pending_stale_jules_recovery(routing, REPO, 2) is None


# ---------------------------------------------------------------------------
# acquire_explicit_retry bypasses the owned-start tombstone once the
# predecessor owner record is free -- but NOT while it still retains PR/session
# membership, even with no live local execution. That distinction is the
# invariant this module's engine-level caller relies on to defer until real
# retirement clears the predecessor.
# ---------------------------------------------------------------------------


def test_acquire_explicit_retry_crosses_tombstone_once_predecessor_owner_is_free(tmp_path):
    slots, routing = _stores(tmp_path)
    owner = ImplementationOwner("issue", 1)
    generation = "generation-a"

    first_execution = slots.start_execution(owner, generation=generation)
    assert first_execution is not None
    routing.record_implementation_owned(REPO, 1, generation)
    slots.finish_execution(owner, first_execution)
    assert not slots.has_qualifying_implementation_activity(owner)
    assert routing.is_implementation_owned(REPO, 1, generation)

    authority = capture_stale_jules_recovery(routing, REPO, 1, 100, generation)
    assert authority is not None

    result = acquire_explicit_retry(routing, slots, REPO, 1, generation, authority.request_id)

    assert result.status == "owned"
    assert result.ownership_reference is not None


def test_qualifying_activity_stays_true_with_only_a_retained_pr(tmp_path):
    """Pins the invariant the engine-level automatic-recovery gate depends on
    (Issue #2286 REQ-003): a finished local execution alone does not free the
    owner while its PR membership is still retained -- only real retirement
    does."""
    slots, _routing = _stores(tmp_path)
    owner = ImplementationOwner("issue", 1)

    execution_id = slots.start_execution(owner, generation="generation-a", implementation_pr=555)
    assert execution_id is not None
    slots.finish_execution(owner, execution_id)

    assert slots.has_qualifying_implementation_activity(owner)


# ---------------------------------------------------------------------------
# publish_recovery_attempt_comment
# ---------------------------------------------------------------------------


def test_publish_recovery_attempt_comment_posts_once_and_orders_above_priors():
    github_client = MagicMock()
    github_client.get_issue_comments.return_value = [
        {"body": "Auto-Coder Attempt: 3", "created_at": "2020-01-01T00:00:00Z"},
    ]

    new_attempt = publish_recovery_attempt_comment(github_client, REPO, 1, "stale-jules-recovery-abc")

    assert new_attempt == 4
    github_client.add_comment_to_issue.assert_called_once()
    posted_body = github_client.add_comment_to_issue.call_args[0][2]
    assert "Auto-Coder Attempt: 4" in posted_body
    assert "stale-jules-recovery-abc" in posted_body


def test_publish_recovery_attempt_comment_is_idempotent_on_reentry():
    """REQ-002: reentry cannot increment again once the comment for this
    exact recovery has already been published."""
    github_client = MagicMock()
    github_client.get_issue_comments.return_value = []

    first = publish_recovery_attempt_comment(github_client, REPO, 1, "stale-jules-recovery-abc")
    assert first == 1
    posted_body = github_client.add_comment_to_issue.call_args[0][2]

    github_client.get_issue_comments.return_value = [{"body": posted_body, "created_at": "2020-01-01T00:00:00Z"}]
    github_client.add_comment_to_issue.reset_mock()

    second = publish_recovery_attempt_comment(github_client, REPO, 1, "stale-jules-recovery-abc")

    assert second == 1
    github_client.add_comment_to_issue.assert_not_called()


def test_publish_recovery_attempt_comment_defers_on_unreadable_evidence():
    """REQ-002: unavailable prior-attempt evidence must defer, never be
    treated as 'no prior attempts' (attempt zero)."""
    github_client = MagicMock()
    github_client.get_issue_comments.side_effect = RuntimeError("boom")

    result = publish_recovery_attempt_comment(github_client, REPO, 1, "stale-jules-recovery-abc")

    assert result is None
    github_client.add_comment_to_issue.assert_not_called()


# ---------------------------------------------------------------------------
# Context propagation
# ---------------------------------------------------------------------------


def test_context_var_defaults_to_none_and_is_scoped():
    assert current_stale_jules_recovery_context() is None

    with stale_jules_recovery_context(None):
        assert current_stale_jules_recovery_context() is None

    routing = MagicMock()
    with stale_jules_recovery_context(routing, "slots-handle"):
        ctx = current_stale_jules_recovery_context()
        assert ctx is not None
        assert ctx.stage_routing is routing
        assert ctx.implementation_slots == "slots-handle"

    assert current_stale_jules_recovery_context() is None
