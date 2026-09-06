"""Tests for the durable dispatch-claim store (GitHub Issue #1791).

Covers the acceptance scenarios from the Issue:
AS-001 concurrent workers cross the boundary once, AS-002 restart after an
accepted dispatch does not duplicate, AS-003 ambiguous delivery fails closed,
AS-004 crash after claim publication is not a rejection, AS-005 definitive
rejection may retry, AS-006 a new head SHA is a new identity, AS-007 claim
storage uncertainty prevents dispatch, AS-008 the legacy label is irrelevant.
"""

import tempfile
import threading
from pathlib import Path

import pytest

from auto_coder.dispatch_claim_store import (
    DispatchClaimStore,
    DispatchIdentity,
    DispatchOutcome,
    get_dispatch_claim_store,
)


@pytest.fixture()
def store(tmp_path):
    return DispatchClaimStore(db_path=tmp_path / "dispatch_claims.db")


@pytest.fixture()
def identity():
    return DispatchIdentity(repo_name="owner/repo", pr_number=42, head_sha="a" * 40, workflow_id="ci.yml")


class TestClaimAcquisition:
    def test_first_claim_is_acquired(self, store, identity):
        result = store.try_acquire_claim(identity)
        assert result.acquired is True

    def test_second_claim_is_denied_while_pending(self, store, identity):
        first = store.try_acquire_claim(identity)
        assert first.acquired is True

        second = store.try_acquire_claim(identity)
        assert second.acquired is False

    def test_concurrent_workers_cross_boundary_once(self, store, identity):
        """AS-001: exactly one of many concurrent claim attempts is admitted."""
        results = []
        lock = threading.Lock()

        def attempt():
            r = store.try_acquire_claim(identity)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=attempt) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        acquired_count = sum(1 for r in results if r.acquired)
        assert acquired_count == 1

    def test_restart_after_accepted_dispatch_does_not_duplicate(self, store, identity):
        """AS-002: a new store instance (simulating a restart) still sees the claim."""
        claim = store.try_acquire_claim(identity)
        assert claim.acquired is True
        assert store.record_outcome(identity, DispatchOutcome.ACCEPTED) is True

        restarted_store = DispatchClaimStore(db_path=store._db_path)
        second_claim = restarted_store.try_acquire_claim(identity)
        assert second_claim.acquired is False

    def test_indeterminate_outcome_keeps_identity_suppressing(self, store, identity):
        """AS-003: an ambiguous delivery must not allow a speculative resend."""
        claim = store.try_acquire_claim(identity)
        assert claim.acquired is True
        assert store.record_outcome(identity, DispatchOutcome.INDETERMINATE) is True

        retry = store.try_acquire_claim(identity)
        assert retry.acquired is False

    def test_crash_after_claim_publication_is_not_treated_as_rejection(self, store, identity):
        """AS-004: a claim left in PENDING (no outcome ever recorded) still suppresses."""
        claim = store.try_acquire_claim(identity)
        assert claim.acquired is True
        # Simulate a crash: no record_outcome call ever happens.

        retry = store.try_acquire_claim(identity)
        assert retry.acquired is False

    def test_definitive_rejection_may_retry(self, store, identity):
        """AS-005: a definite rejection allows the same identity to be dispatched again."""
        claim = store.try_acquire_claim(identity)
        assert claim.acquired is True
        assert store.record_outcome(identity, DispatchOutcome.REJECTED) is True

        retry = store.try_acquire_claim(identity)
        assert retry.acquired is True

    def test_new_head_sha_is_a_new_identity(self, store, identity):
        """AS-006: a claim for SHA A does not block an otherwise eligible SHA B."""
        claim_a = store.try_acquire_claim(identity)
        assert claim_a.acquired is True
        store.record_outcome(identity, DispatchOutcome.ACCEPTED)

        identity_b = DispatchIdentity(
            repo_name=identity.repo_name,
            pr_number=identity.pr_number,
            head_sha="b" * 40,
            workflow_id=identity.workflow_id,
        )
        claim_b = store.try_acquire_claim(identity_b)
        assert claim_b.acquired is True

    def test_storage_uncertainty_prevents_dispatch(self, identity, monkeypatch):
        """AS-007: a store that cannot be read/locked fails closed."""
        store = DispatchClaimStore(db_path=Path("/nonexistent-dir-for-test") / "sub" / "dispatch.db")

        def _boom(self):
            raise OSError("cannot create directory")

        monkeypatch.setattr(DispatchClaimStore, "_connect", _boom)

        result = store.try_acquire_claim(identity)
        assert result.acquired is False
        assert result.reason

    def test_label_irrelevant_to_dispatch_admission(self, store, identity):
        """AS-008: admission depends only on the durable claim, never on any label."""
        # The store API has no notion of a GitHub label at all; two otherwise
        # identical identities produce the same decision regardless of any
        # label bookkeeping happening elsewhere in the caller.
        claim = store.try_acquire_claim(identity)
        assert claim.acquired is True

        same_identity_again = DispatchIdentity(
            repo_name=identity.repo_name,
            pr_number=identity.pr_number,
            head_sha=identity.head_sha,
            workflow_id=identity.workflow_id,
        )
        result = store.try_acquire_claim(same_identity_again)
        assert result.acquired is False


class TestDefaultStore:
    def test_get_dispatch_claim_store_returns_singleton(self):
        assert get_dispatch_claim_store() is get_dispatch_claim_store()
