from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from auto_coder.durable_repair_allowance import DeliveryOutcome, RepairAllowanceLedger
from auto_coder.issue_dispatch import AdapterOutcome, CandidateHandoff, DispatchOutcome, DispatchResult, IssueAttemptIdentity, IssueDispatchGuard
from auto_coder.local_job_handoff import (
    InvocationOutcome,
    LocalJobKind,
    LocalJobOffer,
    LocalJobState,
    LocalJobStore,
)
from auto_coder.local_review_repair import (
    LocalReviewRepairOutcome,
    LocalReviewRepairRequest,
    LocalReviewRepairStore,
    admit_local_repair_allowance,
    execute_local_review_repair,
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


def test_issue_offer_cannot_commit_after_upstream_claim_is_replaced(tmp_path: Path) -> None:
    store, guard, identity, dispatch_claim, offer = _issue_offer(tmp_path)
    inspected = Event()
    resume = Event()
    original = guard.inspect_local_job_authority

    def paused_inspection(candidate: IssueAttemptIdentity):
        result = original(candidate)
        inspected.set()
        assert resume.wait(5)
        return result

    result: list[object] = []
    with patch.object(guard, "inspect_local_job_authority", side_effect=paused_inspection):
        worker = Thread(target=lambda: result.append(store.offer_issue(offer, identity, guard)))
        worker.start()
        assert inspected.wait(5)
        released = guard.finalize(dispatch_claim, AdapterOutcome(DispatchOutcome.NOT_STARTED))
        assert released.outcome is DispatchOutcome.NOT_STARTED
        successor = guard.reserve(identity, CandidateHandoff("codex", "local"))
        assert successor.admitted and successor.claim_incarnation != dispatch_claim.claim_incarnation
        resume.set()
        worker.join(5)

    assert result == [None]
    assert store.get(offer.job_id) is None
    assert store.claim(offer.job_id) is None


def test_issue_adapter_entry_suppresses_later_job_offer(tmp_path: Path) -> None:
    store, guard, identity, dispatch_claim, offer = _issue_offer(tmp_path)
    assert guard.mark_invocation_started(dispatch_claim)

    assert store.offer_issue(offer, identity, guard) is None
    assert store.get(offer.job_id) is None


def test_production_dispatch_callback_suppresses_job_offer_after_entry(tmp_path: Path) -> None:
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    guard = IssueDispatchGuard(tmp_path / "dispatch.sqlite3")
    identity = IssueAttemptIdentity("owner", "repo", 42, "attempt-1")
    offer = LocalJobOffer(LocalJobKind.ISSUE_IMPLEMENTATION, "owner/repo", 42, "attempt-1", "codex", "implement")
    entered = Event()
    finish = Event()
    results: list[DispatchResult] = []

    def invoke(_candidate: CandidateHandoff) -> AdapterOutcome:
        entered.set()
        assert finish.wait(5)
        return AdapterOutcome(DispatchOutcome.LOCAL_COMPLETED)

    worker = Thread(target=lambda: results.append(guard.dispatch_candidates(identity, [CandidateHandoff("codex", "local")], invoke)))
    worker.start()
    assert entered.wait(5)

    assert store.offer_issue(offer, identity, guard) is None
    assert store.get(offer.job_id) is None
    finish.set()
    worker.join(5)
    assert results and results[0].outcome is DispatchOutcome.LOCAL_COMPLETED


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
    artifact = store.persist_result_artifact(winner, InvocationOutcome.COMPLETED, "result output")
    assert artifact is not None
    assert store.record_result(winner, InvocationOutcome.COMPLETED, artifact.artifact_id)
    assert store.get(offer.job_id).result_reference == artifact.artifact_id  # type: ignore[union-attr]


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
    assert not store.record_result(claim, InvocationOutcome.COMPLETED, "result://nonexistent")
    assert store.get(offer.job_id).state is LocalJobState.RUNNING  # type: ignore[union-attr]
    artifact = store.persist_result_artifact(claim, InvocationOutcome.CANNOT_FIX, "CANNOT_FIX")
    assert artifact is not None
    restarted = LocalJobStore(store.path)
    assert restarted.get_result_artifact(artifact.artifact_id) == artifact
    assert artifact.output == "CANNOT_FIX"
    assert restarted.record_result(claim, InvocationOutcome.CANNOT_FIX, artifact.artifact_id)
    assert restarted.get(offer.job_id).state is LocalJobState.RESULT_RECORDED  # type: ignore[union-attr]
    assert restarted.mark_downstream_pending(claim)
    assert restarted.get(offer.job_id).state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING  # type: ignore[union-attr]
    assert restarted.settle(claim, "domain consumer settled")
    assert restarted.get(offer.job_id).state is LocalJobState.SETTLED  # type: ignore[union-attr]


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


def test_result_artifact_for_another_job_cannot_complete_claim(tmp_path: Path) -> None:
    store, guard, identity, _, offer = _issue_offer(tmp_path, "attempt-1", "first")
    first = store.offer_issue(offer, identity, guard)
    assert first is not None
    first_claim = store.claim(first.job_id)
    assert first_claim is not None and first_claim.acquired
    artifact = store.persist_result_artifact(first_claim, InvocationOutcome.COMPLETED, "first output")
    assert artifact is not None

    _, second_guard, second_identity, _, second_offer = _issue_offer(tmp_path, "attempt-2", "second")
    second = store.offer_issue(second_offer, second_identity, second_guard)
    assert second is not None
    second_claim = store.claim(second.job_id)
    assert second_claim is not None and second_claim.acquired

    assert not store.record_result(second_claim, InvocationOutcome.COMPLETED, artifact.artifact_id)
    assert store.get(second.job_id).state is LocalJobState.RUNNING  # type: ignore[union-attr]


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


def test_pr_backend_entry_and_confirmed_delivery_suppress_job_offer(tmp_path: Path) -> None:
    request = LocalReviewRepairRequest("owner/repo", 7, "owner/repo", "feature", "a" * 40, ("blocker-1",), "repair")
    repair_store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    repair_claim = repair_store.admit(request)
    ledger = RepairAllowanceLedger(tmp_path / "allowance.sqlite3")
    authority, _ = admit_local_repair_allowance(request, ledger)
    assert authority is not None
    authority.mark_invocation()
    assert repair_store.mark_invocation_entered(request, repair_claim)
    offer = LocalJobOffer(LocalJobKind.PR_REVIEW_CORRECTION, "owner/repo", 7, request.attempt_id, "codex", request.prompt)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")

    assert store.offer_pr_correction(offer, request, repair_store, ledger) is None
    assert store.get(offer.job_id) is None


def test_production_pr_executor_suppresses_job_offer_after_backend_entry(tmp_path: Path, monkeypatch) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repository, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True).stdout.strip()
    monkeypatch.chdir(repository)
    request = LocalReviewRepairRequest("owner/repo", 7, "owner/repo", "feature", head, ("blocker-1",), "repair")
    repair_store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    ledger = RepairAllowanceLedger(tmp_path / "allowance.sqlite3")
    authority, _ = admit_local_repair_allowance(request, ledger)
    assert authority is not None
    entered = Event()
    finish = Event()
    outcomes: list[LocalReviewRepairOutcome] = []

    def executor(_request: LocalReviewRepairRequest, _worktree: str) -> str:
        entered.set()
        assert finish.wait(5)
        return "ACTION_SUMMARY: no changes"

    worker = Thread(target=lambda: outcomes.append(execute_local_review_repair(request, store=repair_store, executor=executor, allowance_authority=authority)))
    worker.start()
    assert entered.wait(5)
    offer = LocalJobOffer(LocalJobKind.PR_REVIEW_CORRECTION, "owner/repo", 7, request.attempt_id, "codex", request.prompt)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")

    assert store.offer_pr_correction(offer, request, repair_store, ledger) is None
    assert store.get(offer.job_id) is None
    finish.set()
    worker.join(5)
    assert outcomes and outcomes[0].phase == "completed_no_change"


def test_pr_indeterminate_delivery_suppresses_job_offer(tmp_path: Path) -> None:
    request = LocalReviewRepairRequest("owner/repo", 7, "owner/repo", "feature", "a" * 40, ("blocker-1",), "repair")
    repair_store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    repair_store.admit(request)
    ledger = RepairAllowanceLedger(tmp_path / "allowance.sqlite3")
    authority, _ = admit_local_repair_allowance(request, ledger)
    assert authority is not None
    ledger.record_delivery_outcome(
        "https://api.github.com",
        request.repository,
        request.pr_number,
        "indeterminate-delivery",
        authority.epoch,
        authority.generation_id,
        DeliveryOutcome.INDETERMINATE,
        request.attempt_id,
        evidence="backend entry cannot be excluded",
    )
    offer = LocalJobOffer(LocalJobKind.PR_REVIEW_CORRECTION, "owner/repo", 7, request.attempt_id, "codex", request.prompt)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")

    assert store.offer_pr_correction(offer, request, repair_store, ledger) is None
    assert store.get(offer.job_id) is None


def test_pr_offer_cannot_commit_after_allowance_snapshot_becomes_delivered(tmp_path: Path) -> None:
    request = LocalReviewRepairRequest("owner/repo", 7, "owner/repo", "feature", "a" * 40, ("blocker-1",), "repair")
    repair_store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    repair_store.admit(request)
    ledger = RepairAllowanceLedger(tmp_path / "allowance.sqlite3")
    authority, _ = admit_local_repair_allowance(request, ledger)
    assert authority is not None
    offer = LocalJobOffer(LocalJobKind.PR_REVIEW_CORRECTION, "owner/repo", 7, request.attempt_id, "codex", request.prompt)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    inspected = Event()
    resume = Event()
    original = ledger.get_snapshot

    def paused_snapshot(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        inspected.set()
        assert resume.wait(5)
        return snapshot

    result: list[object] = []
    with patch.object(ledger, "get_snapshot", side_effect=paused_snapshot):
        worker = Thread(target=lambda: result.append(store.offer_pr_correction(offer, request, repair_store, ledger)))
        worker.start()
        assert inspected.wait(5)
        RepairAllowanceLedger(tmp_path / "allowance.sqlite3").record_delivery_outcome(
            "https://api.github.com",
            request.repository,
            request.pr_number,
            "concurrent-delivery",
            authority.epoch,
            authority.generation_id,
            DeliveryOutcome.CONFIRMED,
            request.attempt_id,
            evidence="concurrent backend entry",
        )
        resume.set()
        worker.join(5)

    assert result == [None]
    assert store.get(offer.job_id) is None
    assert store.claim(offer.job_id) is None
