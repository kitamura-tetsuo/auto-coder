from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from auto_coder.durable_repair_allowance import RepairAllowanceLedger
from auto_coder.issue_dispatch import AdapterOutcome, CandidateHandoff, DispatchOutcome, IssueAttemptIdentity, IssueDispatchGuard
from auto_coder.local_job_handoff import (
    InvocationOutcome,
    LocalJobKind,
    LocalJobOffer,
    LocalJobState,
    LocalJobStore,
)
from auto_coder.local_review_repair import (
    LocalReviewRepairRequest,
    LocalReviewRepairStore,
    admit_local_repair_allowance,
)


def _issue_offer(tmp_path: Path, attempt: str = "attempt-1", prompt: str = "implement"):
    guard = IssueDispatchGuard(tmp_path / "dispatch.sqlite3")
    identity = IssueAttemptIdentity("owner", "repo", 42, attempt)
    claim = guard.reserve(identity, CandidateHandoff("codex", "local"))
    offer = LocalJobOffer(LocalJobKind.ISSUE_IMPLEMENTATION, "owner/repo", 42, attempt, "codex", prompt)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    return store, guard, identity, claim, offer


def test_issue_offer_is_durable_idempotent_and_attempt_bound(tmp_path: Path) -> None:
    store, guard, identity, dispatch_claim, offer = _issue_offer(tmp_path)

    accepted = store.offer_issue(offer, identity, guard)

    assert accepted is not None
    assert accepted.state is LocalJobState.PENDING
    assert accepted.upstream_incarnation == dispatch_claim.claim_incarnation
    assert LocalJobStore(store.path).offer_issue(offer, identity, guard) == accepted
    assert LocalJobStore(store.path).get(offer.job_id) == accepted

    conflicting = LocalJobOffer(LocalJobKind.ISSUE_IMPLEMENTATION, "owner/repo", 42, "attempt-1", "codex", "different input")
    assert store.offer_issue(conflicting, identity, guard) is None

    second_store, second_guard, second_identity, _, second_offer = _issue_offer(tmp_path, "attempt-2")
    second = second_store.offer_issue(second_offer, second_identity, second_guard)
    assert second is not None
    assert second.job_id != accepted.job_id


def test_offer_refuses_caller_identity_without_upstream_ownership(tmp_path: Path) -> None:
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    guard = IssueDispatchGuard(tmp_path / "dispatch.sqlite3")
    identity = IssueAttemptIdentity("owner", "repo", 42, "not-admitted")
    offer = LocalJobOffer(LocalJobKind.ISSUE_IMPLEMENTATION, "owner/repo", 42, "not-admitted", "codex", "input")

    assert store.offer_issue(offer, identity, guard) is None
    assert store.get(offer.job_id) is None


def test_issue_offer_refuses_a_finalized_indeterminate_invocation(tmp_path: Path) -> None:
    store, guard, identity, dispatch_claim, offer = _issue_offer(tmp_path)
    finalized = guard.finalize(dispatch_claim, AdapterOutcome(DispatchOutcome.INDETERMINATE, diagnostic="backend may have started"))
    assert finalized.outcome is DispatchOutcome.INDETERMINATE

    assert store.offer_issue(offer, identity, guard) is None
    assert store.get(offer.job_id) is None


def test_claim_contention_and_stale_writer_are_fenced(tmp_path: Path) -> None:
    store, guard, identity, _, offer = _issue_offer(tmp_path)
    assert store.offer_issue(offer, identity, guard) is not None

    def contend() -> object:
        return LocalJobStore(store.path).claim(offer.job_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: contend(), range(2)))
    winners = [claim for claim in claims if claim is not None and claim.acquired]
    assert len(winners) == 1
    loser = next(claim for claim in claims if claim is not None and not claim.acquired)
    winner = winners[0]

    assert not store.record_result(loser, InvocationOutcome.COMPLETED, "result://loser")
    assert store.record_result(winner, InvocationOutcome.COMPLETED, "result://winner")
    assert store.get(offer.job_id).result_reference == "result://winner"  # type: ignore[union-attr]


def test_restart_keeps_running_job_for_reconciliation_not_reissue(tmp_path: Path) -> None:
    store, guard, identity, _, offer = _issue_offer(tmp_path)
    assert store.offer_issue(offer, identity, guard) is not None
    claim = store.claim(offer.job_id)
    assert claim is not None and claim.acquired

    restarted = LocalJobStore(store.path)
    retained = restarted.discover_unsettled()
    second_claim = restarted.claim(offer.job_id)

    assert len(retained) == 1
    assert retained[0].state is LocalJobState.RUNNING
    assert second_claim is not None and not second_claim.acquired
    assert second_claim.record.execution_incarnation == claim.record.execution_incarnation


def test_result_and_downstream_lifecycle_never_imply_completion_early(tmp_path: Path) -> None:
    store, guard, identity, _, offer = _issue_offer(tmp_path)
    assert store.offer_issue(offer, identity, guard) is not None
    claim = store.claim(offer.job_id)
    assert claim is not None and claim.acquired
    assert not store.record_result(claim, InvocationOutcome.COMPLETED, "")
    assert store.get(offer.job_id).state is LocalJobState.RUNNING  # type: ignore[union-attr]
    assert store.record_result(claim, InvocationOutcome.CANNOT_FIX, "result://output")
    assert store.get(offer.job_id).state is LocalJobState.RESULT_RECORDED  # type: ignore[union-attr]
    assert store.mark_downstream_pending(claim)
    assert store.get(offer.job_id).state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING  # type: ignore[union-attr]
    assert store.settle(claim, "domain consumer settled")
    assert store.get(offer.job_id).state is LocalJobState.SETTLED  # type: ignore[union-attr]


def test_pr_offer_requires_exact_real_claim_and_allowance_generation(tmp_path: Path) -> None:
    request = LocalReviewRepairRequest("owner/repo", 7, "owner/repo", "feature", "a" * 40, ("blocker-1",), "repair")
    repair_store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    repair_claim = repair_store.admit(request)
    assert repair_claim.admitted
    ledger = RepairAllowanceLedger(tmp_path / "allowance.sqlite3")
    authority, reason = admit_local_repair_allowance(request, ledger)
    assert authority is not None and reason == ""
    offer = LocalJobOffer(LocalJobKind.PR_REVIEW_CORRECTION, "owner/repo", 7, request.attempt_id, "codex", request.prompt)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")

    accepted = store.offer_pr_correction(offer, request, repair_store, ledger)

    assert accepted is not None
    assert accepted.upstream_incarnation == f"{repair_claim.incarnation}:{authority.generation_id}"
    stale = LocalReviewRepairRequest("owner/repo", 7, "owner/repo", "feature", "b" * 40, ("blocker-1",), "repair")
    stale_offer = LocalJobOffer(LocalJobKind.PR_REVIEW_CORRECTION, "owner/repo", 7, stale.attempt_id, "codex", stale.prompt)
    assert store.offer_pr_correction(stale_offer, stale, repair_store, ledger) is None


def test_pr_offer_fails_closed_when_allowance_state_is_unreadable(tmp_path: Path) -> None:
    request = LocalReviewRepairRequest("owner/repo", 7, "owner/repo", "feature", "a" * 40, ("blocker-1",), "repair")
    repair_store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    assert repair_store.admit(request).admitted
    allowance_path = tmp_path / "allowance.sqlite3"
    ledger = RepairAllowanceLedger(allowance_path)
    authority, _ = admit_local_repair_allowance(request, ledger)
    assert authority is not None
    allowance_path.write_bytes(b"not sqlite")
    offer = LocalJobOffer(LocalJobKind.PR_REVIEW_CORRECTION, "owner/repo", 7, request.attempt_id, "codex", request.prompt)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")

    assert store.offer_pr_correction(offer, request, repair_store, ledger) is None
    assert store.get(offer.job_id) is None
