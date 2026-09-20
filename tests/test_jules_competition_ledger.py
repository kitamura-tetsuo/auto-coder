"""Tests for the durable Jules competition-generation ledger (GitHub Issue #2070).

Covers:
- AS-001: Genuine selection contention (REQ-003..REQ-006).
- AS-002: Retirement precedes late discovery (REQ-002, REQ-006, REQ-011).
- AS-003: Stale validation is not a new candidate (REQ-004, REQ-007).
- AS-004: Atomic failure/outbox persistence (REQ-003, REQ-008, REQ-009).
- AS-005: Storage and repository isolation (REQ-001, REQ-003, REQ-011, REQ-012).
- AS-006: Source invalidation with merge uncertainty (REQ-008..REQ-010).

Per Issue #2070's own scoping ("This stage supplies the bounded state model
and durable transitions... downstream stages test how production
observations produce these records"), these tests exercise the real
store/transition API directly with distinct client instances and process
restart (a fresh ``JulesCompetitionLedger`` pointed at the same on-disk
database), not only dataclass construction.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from auto_coder.jules_competition_ledger import (
    AcceptanceRecord,
    CandidateAuthorityState,
    CandidateBindingObservation,
    GenerationLifecycleState,
    GenerationRetirementSource,
    JulesCompetitionIdempotencyConflictError,
    JulesCompetitionLedger,
    JulesCompetitionPersistenceError,
    JulesCompetitionUnavailableError,
    MergeOutcome,
    ObligationKind,
    ObligationStatus,
    ReconciliationStatus,
    SpeculativeGenerationBundle,
    StaleJulesCompetitionEpochError,
    UnknownCandidateReferenceError,
    UnknownGenerationReferenceError,
)

REPO = "kitamura-tetsuo/auto-coder"
ISSUE = 2070


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "jules_competition.db"


@pytest.fixture()
def store(db_path: Path) -> JulesCompetitionLedger:
    return JulesCompetitionLedger(db_path=db_path)


def _bundle(candidate_ids=("cand-a", "cand-b", "cand-c"), attempt=1, fingerprint="oracle-fp-1", branch="jules/attempt-1") -> SpeculativeGenerationBundle:
    return SpeculativeGenerationBundle(
        source_attempt_number=attempt,
        candidate_ids=tuple(candidate_ids),
        issue_oracle_snapshot="issue body snapshot v1",
        issue_oracle_fingerprint=fingerprint,
        source_branch=branch,
    )


def _create_generation(store: JulesCompetitionLedger, *, issue_number: int = ISSUE, candidate_ids=("cand-a", "cand-b", "cand-c"), op="op-create") -> tuple[str, int]:
    epoch = store.get_current_epoch(REPO, issue_number)
    result = store.create_generation(REPO, issue_number, op, epoch, _bundle(candidate_ids=candidate_ids))
    assert result.admitted, result.denial_reason
    assert result.generation_id is not None
    return result.generation_id, result.snapshot.epoch


def _bind(store: JulesCompetitionLedger, generation_id: str, epoch: int, candidate_id: str, pr_number: int, head_sha: str, base_sha: str = "base-sha", op_suffix: str = "") -> int:
    result = store.record_candidate_binding(
        REPO,
        ISSUE,
        generation_id,
        candidate_id,
        f"op-bind-{candidate_id}-{pr_number}-{op_suffix}",
        epoch,
        CandidateBindingObservation(pr_repository=REPO, pr_number=pr_number, head_sha=head_sha, base_sha=base_sha),
    )
    assert result.applied
    return result.snapshot.epoch


def _acceptance(generation_id: str, candidate_id: str, pr_number: int, head_sha: str, revision: int, base_sha: str = "base-sha", fingerprint: str = "oracle-fp-1") -> AcceptanceRecord:
    return AcceptanceRecord(
        repository=REPO,
        generation_id=generation_id,
        candidate_id=candidate_id,
        pr_repository=REPO,
        pr_number=pr_number,
        head_sha=head_sha,
        base_sha=base_sha,
        issue_oracle_fingerprint=fingerprint,
        expected_binding_revision=revision,
        validation_revision=1,
        invalidation_revision=1,
    )


# ---------------------------------------------------------------------------
# REQ-001: generation identity, fixed candidate set, single active generation
# ---------------------------------------------------------------------------


def test_req001_generation_identity_and_single_active_slot(store: JulesCompetitionLedger) -> None:
    generation_id, epoch = _create_generation(store)
    snapshot = store.get_namespace_snapshot(REPO, ISSUE)
    generation = snapshot.get_generation(generation_id)
    assert generation is not None
    assert generation.candidate_count == 3
    assert set(generation.candidate_ids) == {"cand-a", "cand-b", "cand-c"}
    assert generation.lifecycle_state == GenerationLifecycleState.ACTIVE

    # A second admission attempt while one is active is denied.
    second = store.create_generation(REPO, ISSUE, "op-create-second", epoch, _bundle(candidate_ids=("x", "y")))
    assert not second.admitted
    assert second.denial_reason is not None and "active" in second.denial_reason.lower()
    # Denial never resizes or replaces the existing candidate set.
    unchanged = store.get_namespace_snapshot(REPO, ISSUE).get_generation(generation_id)
    assert unchanged is not None
    assert unchanged.candidate_count == 3


def test_req001_resuming_a_generation_does_not_resize_candidates(store: JulesCompetitionLedger) -> None:
    epoch = store.get_current_epoch(REPO, ISSUE)
    bundle = _bundle()
    first = store.create_generation(REPO, ISSUE, "op-resume", epoch, bundle)
    assert first.admitted
    # "Resuming" replays the same operation_id/bundle; it must be a no-op.
    second = store.create_generation(REPO, ISSUE, "op-resume", epoch, bundle)
    assert second.admitted
    assert second.generation_id == first.generation_id
    snapshot = store.get_namespace_snapshot(REPO, ISSUE)
    assert len(snapshot.generations) == 1
    assert snapshot.get_generation(first.generation_id).candidate_count == 3


def test_req001_rejects_malformed_candidate_sets(store: JulesCompetitionLedger) -> None:
    epoch = store.get_current_epoch(REPO, ISSUE)
    with pytest.raises(ValueError):
        store.create_generation(REPO, ISSUE, "op-empty", epoch, _bundle(candidate_ids=()))
    with pytest.raises(ValueError):
        store.create_generation(REPO, ISSUE, "op-dup", epoch, _bundle(candidate_ids=("a", "a")))


# ---------------------------------------------------------------------------
# REQ-002: durable candidate authority states
# ---------------------------------------------------------------------------


def test_req002_candidate_authority_state_machine(store: JulesCompetitionLedger) -> None:
    generation_id, epoch = _create_generation(store)

    result = store.claim_candidate_submission(REPO, ISSUE, generation_id, "cand-a", "op-claim-a", epoch)
    assert result.applied
    epoch = result.snapshot.epoch
    candidate = result.snapshot.get_generation(generation_id).get_candidate("cand-a")
    assert candidate.authority_state == CandidateAuthorityState.SUBMISSION_CLAIMED

    result = store.record_candidate_accepted(REPO, ISSUE, generation_id, "cand-a", "op-accept-a", epoch, provider_id="jules", session_id="sess-a")
    assert result.applied
    epoch = result.snapshot.epoch
    candidate = result.snapshot.get_generation(generation_id).get_candidate("cand-a")
    assert candidate.authority_state == CandidateAuthorityState.ACCEPTED
    assert candidate.provider_id == "jules"
    assert candidate.session_id == "sess-a"

    result = store.record_candidate_not_accepted(REPO, ISSUE, generation_id, "cand-b", "op-notacc-b", epoch)
    assert result.applied
    epoch = result.snapshot.epoch
    candidate_b = result.snapshot.get_generation(generation_id).get_candidate("cand-b")
    assert candidate_b.authority_state == CandidateAuthorityState.DEFINITELY_NOT_ACCEPTED

    result = store.record_candidate_outcome_unknown(REPO, ISSUE, generation_id, "cand-c", "op-unknown-c", epoch)
    assert result.applied
    epoch = result.snapshot.epoch
    candidate_c = result.snapshot.get_generation(generation_id).get_candidate("cand-c")
    assert candidate_c.authority_state == CandidateAuthorityState.SUBMISSION_OUTCOME_UNKNOWN


def test_req002_unavailable_observation_does_not_erase_a_more_informative_state(store: JulesCompetitionLedger) -> None:
    generation_id, epoch = _create_generation(store)
    result = store.record_candidate_accepted(REPO, ISSUE, generation_id, "cand-a", "op-accept", epoch, provider_id="jules", session_id="sess-1")
    epoch = result.snapshot.epoch

    result = store.record_candidate_outcome_unknown(REPO, ISSUE, generation_id, "cand-a", "op-unknown", epoch)
    assert not result.applied
    candidate = result.snapshot.get_generation(generation_id).get_candidate("cand-a")
    assert candidate.authority_state == CandidateAuthorityState.ACCEPTED
    assert candidate.session_id == "sess-1"


def test_req002_unknown_generation_or_candidate_reference_raises(store: JulesCompetitionLedger) -> None:
    generation_id, epoch = _create_generation(store)
    with pytest.raises(UnknownGenerationReferenceError):
        store.claim_candidate_submission(REPO, ISSUE, "jgen_does_not_exist", "cand-a", "op-x", epoch)
    with pytest.raises(UnknownCandidateReferenceError):
        store.claim_candidate_submission(REPO, ISSUE, generation_id, "cand-does-not-exist", "op-y", epoch)


# ---------------------------------------------------------------------------
# REQ-003: serialization, restart durability, fail-closed corruption handling
# ---------------------------------------------------------------------------


def test_req003_state_survives_a_fresh_store_instance(store: JulesCompetitionLedger, db_path: Path) -> None:
    generation_id, epoch = _create_generation(store)
    store.record_candidate_binding(REPO, ISSUE, generation_id, "cand-a", "op-bind", epoch, CandidateBindingObservation(pr_repository=REPO, pr_number=101, head_sha="h1", base_sha="b1"))

    restarted = JulesCompetitionLedger(db_path=db_path)
    snapshot = restarted.get_namespace_snapshot(REPO, ISSUE)
    generation = snapshot.get_generation(generation_id)
    assert generation is not None
    candidate = generation.get_candidate("cand-a")
    assert candidate.latest_binding().pr_number == 101


def test_req003_stale_epoch_is_rejected(store: JulesCompetitionLedger) -> None:
    generation_id, epoch = _create_generation(store)
    with pytest.raises(StaleJulesCompetitionEpochError):
        store.claim_candidate_submission(REPO, ISSUE, generation_id, "cand-a", "op-stale", epoch + 5)


def test_req003_idempotency_conflict_on_reused_operation_id(store: JulesCompetitionLedger) -> None:
    generation_id, epoch = _create_generation(store)
    store.claim_candidate_submission(REPO, ISSUE, generation_id, "cand-a", "op-reuse", epoch)
    with pytest.raises(JulesCompetitionIdempotencyConflictError):
        store.claim_candidate_submission(REPO, ISSUE, generation_id, "cand-b", "op-reuse", epoch)


def test_req003_corrupt_database_denies_effects(store: JulesCompetitionLedger, db_path: Path) -> None:
    _create_generation(store)
    with open(db_path, "r+b") as fh:
        fh.seek(16)
        fh.write(b"\xff" * 64)
    corrupted = JulesCompetitionLedger(db_path=db_path)
    with pytest.raises(JulesCompetitionUnavailableError):
        corrupted.get_namespace_snapshot(REPO, ISSUE)
    with pytest.raises(JulesCompetitionUnavailableError):
        corrupted.create_generation(REPO, ISSUE, "op-after-corrupt", 0, _bundle())


def test_req003_simulated_write_failure_rolls_back(store: JulesCompetitionLedger) -> None:
    epoch = store.get_current_epoch(REPO, ISSUE)
    store._simulate_failure_before_commit = True
    with pytest.raises(JulesCompetitionPersistenceError):
        store.create_generation(REPO, ISSUE, "op-fail", epoch, _bundle())
    store._simulate_failure_before_commit = False
    snapshot = store.get_namespace_snapshot(REPO, ISSUE)
    assert len(snapshot.generations) == 0
    # A clean retry with the same epoch succeeds (nothing was left half-written).
    result = store.create_generation(REPO, ISSUE, "op-retry", epoch, _bundle())
    assert result.admitted


# ---------------------------------------------------------------------------
# AS-001: genuine selection contention
# ---------------------------------------------------------------------------


def test_as001_genuine_selection_contention(store: JulesCompetitionLedger, db_path: Path) -> None:
    generation_id, epoch = _create_generation(store)
    epoch = _bind(store, generation_id, epoch, "cand-a", pr_number=1, head_sha="ha")
    epoch = _bind(store, generation_id, epoch, "cand-b", pr_number=2, head_sha="hb")

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}
    errors: list[Exception] = []

    def attempt(candidate_id: str, pr_number: int, head_sha: str) -> None:
        client = JulesCompetitionLedger(db_path=db_path)
        try:
            barrier.wait(timeout=5)
            current_epoch = client.get_current_epoch(REPO, ISSUE)
            result = client.select_winner(
                REPO,
                ISSUE,
                generation_id,
                candidate_id,
                f"op-select-{candidate_id}",
                current_epoch,
                _acceptance(generation_id, candidate_id, pr_number, head_sha, revision=1),
            )
            results[candidate_id] = result
        except StaleJulesCompetitionEpochError:
            results[candidate_id] = None
        except Exception as exc:  # pragma: no cover - surfaced via assertion below
            errors.append(exc)

    threads = [
        threading.Thread(target=attempt, args=("cand-a", 1, "ha")),
        threading.Thread(target=attempt, args=("cand-b", 2, "hb")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors

    # One racer may see a stale epoch (None) if it read before the other's
    # commit; retry it exactly as a real caller would.
    for candidate_id, pr_number, head_sha in (("cand-a", 1, "ha"), ("cand-b", 2, "hb")):
        if results[candidate_id] is None:
            current_epoch = store.get_current_epoch(REPO, ISSUE)
            results[candidate_id] = store.select_winner(REPO, ISSUE, generation_id, candidate_id, f"op-select-retry-{candidate_id}", current_epoch, _acceptance(generation_id, candidate_id, pr_number, head_sha, revision=1))

    selected_outcomes = [r.selected for r in results.values()]
    assert selected_outcomes.count(True) == 1
    assert selected_outcomes.count(False) == 1

    final_snapshot = store.get_namespace_snapshot(REPO, ISSUE)
    generation = final_snapshot.get_generation(generation_id)
    assert generation.has_winner()
    winner_id = generation.winner_candidate_id
    obligations = [o for o in final_snapshot.obligations if o.kind == ObligationKind.ADOPTION]
    assert len(obligations) == 1
    assert obligations[0].adoption_payload().candidate_id == winner_id

    # Restart and resubmit both acceptance records again: the same winner
    # remains and no new obligation appears (idempotent continuation).
    restarted = JulesCompetitionLedger(db_path=db_path)
    for candidate_id, pr_number, head_sha in (("cand-a", 1, "ha"), ("cand-b", 2, "hb")):
        current_epoch = restarted.get_current_epoch(REPO, ISSUE)
        restarted.select_winner(REPO, ISSUE, generation_id, candidate_id, f"op-select-after-restart-{candidate_id}", current_epoch, _acceptance(generation_id, candidate_id, pr_number, head_sha, revision=1))

    final_snapshot_2 = restarted.get_namespace_snapshot(REPO, ISSUE)
    assert final_snapshot_2.get_generation(generation_id).winner_candidate_id == winner_id
    obligations_2 = [o for o in final_snapshot_2.obligations if o.kind == ObligationKind.ADOPTION]
    assert len(obligations_2) == 1


# ---------------------------------------------------------------------------
# AS-002: retirement precedes late discovery
# ---------------------------------------------------------------------------


def test_as002_retirement_precedes_late_discovery(store: JulesCompetitionLedger, db_path: Path) -> None:
    generation_id, epoch = _create_generation(store)
    epoch = _bind(store, generation_id, epoch, "cand-a", pr_number=1, head_sha="ha")

    result = store.record_candidate_accepted(REPO, ISSUE, generation_id, "cand-b", "op-accept-b", epoch, provider_id="jules", session_id="sess-b")
    epoch = result.snapshot.epoch
    result = store.record_candidate_outcome_unknown(REPO, ISSUE, generation_id, "cand-c", "op-unknown-c", epoch)
    epoch = result.snapshot.epoch

    result = store.select_winner(REPO, ISSUE, generation_id, "cand-a", "op-select-a", epoch, _acceptance(generation_id, "cand-a", 1, "ha", revision=1))
    assert result.selected
    epoch = result.snapshot.epoch

    # Restart before discovering B's PR / reconciling C's session.
    restarted = JulesCompetitionLedger(db_path=db_path)
    epoch = restarted.get_current_epoch(REPO, ISSUE)

    # Discover B's PR late: identity remains inspectable, but binding it
    # does not grant selection authority (B is already RETIRED).
    result = restarted.record_candidate_binding(REPO, ISSUE, generation_id, "cand-b", "op-bind-b-late", epoch, CandidateBindingObservation(pr_repository=REPO, pr_number=2, head_sha="hb", base_sha="base-sha"))
    assert result.applied
    epoch = result.snapshot.epoch
    snapshot = restarted.get_namespace_snapshot(REPO, ISSUE)
    candidate_b = snapshot.get_generation(generation_id).get_candidate("cand-b")
    assert candidate_b.authority_state == CandidateAuthorityState.RETIRED
    assert candidate_b.latest_binding().pr_number == 2  # inspectable

    # Reconcile C's accepted session: identity recorded, authority not restored.
    result = restarted.record_candidate_accepted(REPO, ISSUE, generation_id, "cand-c", "op-accept-c-late", epoch, provider_id="jules", session_id="sess-c")
    assert not result.applied
    epoch = result.snapshot.epoch
    candidate_c = result.snapshot.get_generation(generation_id).get_candidate("cand-c")
    assert candidate_c.authority_state == CandidateAuthorityState.RETIRED
    assert candidate_c.session_id == "sess-c"  # inspectable

    # Neither B nor C can regain selection authority.
    result_b = restarted.select_winner(REPO, ISSUE, generation_id, "cand-b", "op-select-b-late", epoch, _acceptance(generation_id, "cand-b", 2, "hb", revision=1))
    assert not result_b.selected
    result_c = restarted.select_winner(REPO, ISSUE, generation_id, "cand-c", "op-select-c-late", result_b.snapshot.epoch, _acceptance(generation_id, "cand-c", 3, "hc", revision=0))
    assert not result_c.selected

    final_snapshot = restarted.get_namespace_snapshot(REPO, ISSUE)
    assert final_snapshot.get_generation(generation_id).winner_candidate_id == "cand-a"


# ---------------------------------------------------------------------------
# AS-003: stale validation is not a new candidate
# ---------------------------------------------------------------------------


def test_as003_stale_binding_revision_cannot_win(store: JulesCompetitionLedger) -> None:
    generation_id, epoch = _create_generation(store)
    epoch = _bind(store, generation_id, epoch, "cand-a", pr_number=1, head_sha="h1", op_suffix="rev1")
    # A newer head arrives before selection: revision bumps to 2.
    epoch = _bind(store, generation_id, epoch, "cand-a", pr_number=1, head_sha="h2", op_suffix="rev2")

    stale_result = store.select_winner(REPO, ISSUE, generation_id, "cand-a", "op-select-stale", epoch, _acceptance(generation_id, "cand-a", 1, "h1", revision=1))
    assert not stale_result.selected
    assert "stale" in stale_result.denial_reason.lower() or "match" in stale_result.denial_reason.lower()

    fresh_result = store.select_winner(REPO, ISSUE, generation_id, "cand-a", "op-select-fresh", stale_result.snapshot.epoch, _acceptance(generation_id, "cand-a", 1, "h2", revision=2))
    assert fresh_result.selected


def test_as003_old_record_cannot_authorize_a_further_effect_after_selection(store: JulesCompetitionLedger) -> None:
    generation_id, epoch = _create_generation(store)
    epoch = _bind(store, generation_id, epoch, "cand-a", pr_number=1, head_sha="h1")
    result = store.select_winner(REPO, ISSUE, generation_id, "cand-a", "op-select", epoch, _acceptance(generation_id, "cand-a", 1, "h1", revision=1))
    assert result.selected
    epoch = result.snapshot.epoch

    # A fresh record for the SAME winner (matching current state) is idempotent.
    same_result = store.select_winner(REPO, ISSUE, generation_id, "cand-a", "op-select-again", epoch, _acceptance(generation_id, "cand-a", 1, "h1", revision=1))
    assert same_result.selected
    assert same_result.winner_candidate_id == "cand-a"


@pytest.mark.parametrize(
    ("acceptance", "expected_reason"),
    [
        (_acceptance("placeholder", "cand-a", 1, "h1", base_sha="different-base", revision=1), "different selected winner"),
        (_acceptance("placeholder", "cand-a", 1, "h1", fingerprint="different-fingerprint", revision=1), "fingerprint"),
        (_acceptance("placeholder", "cand-a", 1, "h1", revision=2), "revision"),
    ],
)
def test_as003_changed_acceptance_record_is_not_an_idempotent_selection(
    store: JulesCompetitionLedger,
    acceptance: AcceptanceRecord,
    expected_reason: str,
) -> None:
    generation_id, epoch = _create_generation(store)
    epoch = _bind(store, generation_id, epoch, "cand-a", pr_number=1, head_sha="h1")
    selected = store.select_winner(
        REPO,
        ISSUE,
        generation_id,
        "cand-a",
        "op-select",
        epoch,
        _acceptance(generation_id, "cand-a", 1, "h1", revision=1),
    )
    obligations_before = selected.snapshot.obligations

    changed_acceptance = replace(acceptance, generation_id=generation_id)
    repeated = store.select_winner(
        REPO,
        ISSUE,
        generation_id,
        "cand-a",
        f"op-select-changed-{expected_reason}",
        selected.snapshot.epoch,
        changed_acceptance,
    )

    assert not repeated.selected
    assert expected_reason in repeated.denial_reason.lower()
    assert repeated.winner_candidate_id == "cand-a"
    assert repeated.snapshot.generations[0].winner_candidate_id == "cand-a"
    assert repeated.snapshot.obligations == obligations_before


# ---------------------------------------------------------------------------
# AS-004: atomic failure/outbox persistence
# ---------------------------------------------------------------------------


def test_as004_aggregate_failure_requires_full_exhaustion(store: JulesCompetitionLedger) -> None:
    generation_id, epoch = _create_generation(store, candidate_ids=("cand-a", "cand-b", "cand-c"))

    result = store.record_candidate_not_accepted(REPO, ISSUE, generation_id, "cand-a", "op-fail-a", epoch)
    epoch = result.snapshot.epoch
    result = store.record_candidate_not_accepted(REPO, ISSUE, generation_id, "cand-b", "op-fail-b", epoch)
    epoch = result.snapshot.epoch
    result = store.record_candidate_outcome_unknown(REPO, ISSUE, generation_id, "cand-c", "op-unknown-c", epoch)
    epoch = result.snapshot.epoch

    # Candidate C's submission outcome is unknown: aggregate failure is refused.
    denied = store.record_aggregate_failure(REPO, ISSUE, generation_id, "op-aggfail-1", epoch, reason="all candidates exhausted")
    assert not denied.recorded
    assert "unknown" in denied.denial_reason.lower()
    epoch = denied.snapshot.epoch

    result = store.record_candidate_not_accepted(REPO, ISSUE, generation_id, "cand-c", "op-fail-c", epoch)
    epoch = result.snapshot.epoch

    recorded = store.record_aggregate_failure(REPO, ISSUE, generation_id, "op-aggfail-2", epoch, reason="all candidates exhausted")
    assert recorded.recorded
    assert recorded.obligation_id is not None
    generation = recorded.snapshot.get_generation(generation_id)
    assert generation.lifecycle_state == GenerationLifecycleState.RETIRED

    # Duplicate reports do not create additional obligations.
    duplicate = store.record_aggregate_failure(REPO, ISSUE, generation_id, "op-aggfail-3", recorded.snapshot.epoch, reason="reported again")
    assert duplicate.recorded
    assert duplicate.obligation_id == recorded.obligation_id
    obligations = [o for o in duplicate.snapshot.obligations if o.kind == ObligationKind.AGGREGATE_FAILURE]
    assert len(obligations) == 1


def test_as004_obligation_delivery_crash_and_idempotent_acknowledgement(store: JulesCompetitionLedger, db_path: Path) -> None:
    generation_id, epoch = _create_generation(store, candidate_ids=("cand-a", "cand-b"))
    for cand in ("cand-a", "cand-b"):
        result = store.record_candidate_not_accepted(REPO, ISSUE, generation_id, cand, f"op-fail-{cand}", epoch)
        epoch = result.snapshot.epoch

    # Crash before delivery: simulate by creating a new client after the
    # aggregate failure commits but before any consumer observes it.
    recorded = store.record_aggregate_failure(REPO, ISSUE, generation_id, "op-aggfail", epoch, reason="exhausted")
    assert recorded.recorded
    obligation_id = recorded.obligation_id

    restarted = JulesCompetitionLedger(db_path=db_path)
    pending = restarted.list_pending_obligations(REPO, ISSUE, kind=ObligationKind.AGGREGATE_FAILURE)
    assert len(pending) == 1
    assert pending[0].obligation_id == obligation_id
    assert pending[0].status == ObligationStatus.PENDING

    delivered = restarted.mark_obligation_delivered(obligation_id, "op-deliver")
    assert delivered.status == ObligationStatus.DELIVERED

    # Crash after the consumer's observable effect but before acknowledgement.
    restarted_again = JulesCompetitionLedger(db_path=db_path)
    still_pending_for_ack = restarted_again.list_pending_obligations(REPO, ISSUE)
    assert any(o.obligation_id == obligation_id and o.status == ObligationStatus.DELIVERED for o in still_pending_for_ack)

    acked = restarted_again.acknowledge_obligation(obligation_id, "op-ack")
    assert acked.status == ObligationStatus.ACKNOWLEDGED
    # Idempotent completion: acknowledging again is a no-op, not an error.
    acked_again = restarted_again.acknowledge_obligation(obligation_id, "op-ack-again")
    assert acked_again.status == ObligationStatus.ACKNOWLEDGED

    final_pending = restarted_again.list_pending_obligations(REPO, ISSUE)
    assert all(o.obligation_id != obligation_id for o in final_pending)


def test_as004_aggregate_failure_refused_while_unselected_candidate_eligible(store: JulesCompetitionLedger) -> None:
    generation_id, epoch = _create_generation(store, candidate_ids=("cand-a", "cand-b"))
    result = store.record_candidate_not_accepted(REPO, ISSUE, generation_id, "cand-a", "op-fail-a", epoch)
    epoch = result.snapshot.epoch
    # cand-b is still NEVER_SUBMITTED (eligible).
    denied = store.record_aggregate_failure(REPO, ISSUE, generation_id, "op-aggfail", epoch, reason="premature")
    assert not denied.recorded
    assert "eligible" in denied.denial_reason.lower()


def test_as004_aggregate_failure_refused_after_confirmed_merge(store: JulesCompetitionLedger) -> None:
    generation_id, epoch = _create_generation(store, candidate_ids=("cand-a", "cand-b"))
    epoch = _bind(store, generation_id, epoch, "cand-a", pr_number=1, head_sha="h1")
    result = store.select_winner(REPO, ISSUE, generation_id, "cand-a", "op-select", epoch, _acceptance(generation_id, "cand-a", 1, "h1", revision=1))
    assert result.selected
    epoch = result.snapshot.epoch

    snapshot = store.record_merge_outcome(REPO, ISSUE, generation_id, "op-merged", epoch, MergeOutcome.MERGED)
    epoch = snapshot.epoch

    denied = store.record_aggregate_failure(REPO, ISSUE, generation_id, "op-aggfail-after-merge", epoch, reason="too late")
    assert not denied.recorded
    assert "merged" in denied.denial_reason.lower()


# ---------------------------------------------------------------------------
# AS-005: storage and repository isolation
# ---------------------------------------------------------------------------


def test_as005_failed_writes_and_corruption_deny_authority(store: JulesCompetitionLedger) -> None:
    epoch = store.get_current_epoch(REPO, ISSUE)
    store._simulate_failure_before_commit = True
    with pytest.raises(JulesCompetitionPersistenceError):
        store.create_generation(REPO, ISSUE, "op-inject-fail", epoch, _bundle())
    store._simulate_failure_before_commit = False
    assert len(store.get_namespace_snapshot(REPO, ISSUE).generations) == 0


def test_as005_same_issue_number_in_different_repositories_do_not_collide(store: JulesCompetitionLedger) -> None:
    repo_a = "org/repo-a"
    repo_b = "org/repo-b"
    epoch_a = store.get_current_epoch(repo_a, ISSUE)
    epoch_b = store.get_current_epoch(repo_b, ISSUE)

    result_a = store.create_generation(repo_a, ISSUE, "op-a", epoch_a, _bundle(candidate_ids=("a1", "a2")))
    result_b = store.create_generation(repo_b, ISSUE, "op-b", epoch_b, _bundle(candidate_ids=("b1", "b2", "b3")))
    assert result_a.admitted and result_b.admitted
    assert result_a.generation_id != result_b.generation_id

    snapshot_a = store.get_namespace_snapshot(repo_a, ISSUE)
    snapshot_b = store.get_namespace_snapshot(repo_b, ISSUE)
    assert len(snapshot_a.generations) == 1
    assert len(snapshot_b.generations) == 1
    assert snapshot_a.get_generation(result_a.generation_id).candidate_count == 2
    assert snapshot_b.get_generation(result_b.generation_id).candidate_count == 3

    # Retiring/selecting in one namespace never touches the other.
    epoch_a2 = store.get_current_epoch(repo_a, ISSUE)
    store.retire_generation(repo_a, ISSUE, result_a.generation_id, "op-retire-a", epoch_a2, reason="closed", source=GenerationRetirementSource.ISSUE_CLOSED)
    assert store.get_namespace_snapshot(repo_b, ISSUE).get_generation(result_b.generation_id).lifecycle_state == GenerationLifecycleState.ACTIVE


def test_as005_module_does_not_import_legacy_single_session_state(store: JulesCompetitionLedger) -> None:
    """The store never reads/writes cloud.csv or other legacy bindings (REQ-011)."""
    import auto_coder.jules_competition_ledger as module

    source_names = set(dir(module))
    assert "cloud_manager" not in source_names
    assert "attempt_manager" not in source_names
    assert "implementation_slots" not in source_names


def test_as005_missing_namespace_reads_as_empty_not_an_error(store: JulesCompetitionLedger) -> None:
    snapshot = store.get_namespace_snapshot("org/never-touched", 9999)
    assert snapshot.epoch == 0
    assert snapshot.generations == ()


# ---------------------------------------------------------------------------
# AS-006: source invalidation with merge uncertainty
# ---------------------------------------------------------------------------


def test_as006_source_invalidation_with_merge_uncertainty(store: JulesCompetitionLedger) -> None:
    generation_id, epoch = _create_generation(store, candidate_ids=("cand-a", "cand-b"))
    epoch = _bind(store, generation_id, epoch, "cand-a", pr_number=1, head_sha="h1")
    result = store.select_winner(REPO, ISSUE, generation_id, "cand-a", "op-select", epoch, _acceptance(generation_id, "cand-a", 1, "h1", revision=1))
    assert result.selected
    epoch = result.snapshot.epoch

    # Retire the source generation after an oracle change; merge outcome is
    # still UNKNOWN, so a reconciliation record must appear.
    retirement = store.retire_generation(REPO, ISSUE, generation_id, "op-retire", epoch, reason="Issue oracle replaced", source=GenerationRetirementSource.ORACLE_REPLACED)
    assert retirement.retired
    snapshot = retirement.snapshot
    assert snapshot.get_generation(generation_id).lifecycle_state == GenerationLifecycleState.RETIRED
    assert snapshot.has_pending_reconciliation()
    reconciliation = next(r for r in snapshot.reconciliations if r.generation_id == generation_id)
    assert reconciliation.status == ReconciliationStatus.PENDING

    # Late results remain fenced: cand-b (a loser) can never win in this
    # retired generation.
    epoch = snapshot.epoch
    late = store.select_winner(REPO, ISSUE, generation_id, "cand-b", "op-select-late", epoch, _acceptance(generation_id, "cand-b", 2, "h2", revision=0))
    assert not late.selected
    epoch = late.snapshot.epoch

    # A replacement generation can be created...
    replacement = store.create_generation(REPO, ISSUE, "op-create-replacement", epoch, _bundle(candidate_ids=("cand-x", "cand-y"), attempt=2, branch="jules/attempt-2"))
    assert replacement.admitted
    replacement_id = replacement.generation_id
    epoch = replacement.snapshot.epoch
    epoch = _bind(store, replacement_id, epoch, "cand-x", pr_number=3, head_sha="hx")

    # ...but it cannot authorize a competing merge until the original
    # outcome is established.
    blocked = store.select_winner(REPO, ISSUE, replacement_id, "cand-x", "op-select-replacement", epoch, _acceptance(replacement_id, "cand-x", 3, "hx", revision=1))
    assert not blocked.selected
    assert "reconciliation" in blocked.denial_reason.lower()
    epoch = blocked.snapshot.epoch

    # Establishing the original outcome resolves the reconciliation...
    resolved_snapshot = store.record_merge_outcome(REPO, ISSUE, generation_id, "op-merge-outcome", epoch, MergeOutcome.MERGED)
    assert not resolved_snapshot.has_pending_reconciliation()
    epoch = resolved_snapshot.epoch

    # ...and now the replacement can authorize its own merge.
    unblocked = store.select_winner(REPO, ISSUE, replacement_id, "cand-x", "op-select-replacement-2", epoch, _acceptance(replacement_id, "cand-x", 3, "hx", revision=1))
    assert unblocked.selected


def test_as006_retiring_a_generation_does_not_fail_individual_candidates(store: JulesCompetitionLedger) -> None:
    generation_id, epoch = _create_generation(store, candidate_ids=("cand-a", "cand-b"))
    retirement = store.retire_generation(REPO, ISSUE, generation_id, "op-retire", epoch, reason="Issue closed", source=GenerationRetirementSource.ISSUE_CLOSED)
    assert retirement.retired
    generation = retirement.snapshot.get_generation(generation_id)
    for candidate in generation.candidates:
        assert candidate.authority_state == CandidateAuthorityState.NEVER_SUBMITTED


# ---------------------------------------------------------------------------
# REQ-012: readable state distinctions
# ---------------------------------------------------------------------------


def test_req012_readable_state_distinguishes_retired_candidate_from_stopped_session(store: JulesCompetitionLedger) -> None:
    generation_id, epoch = _create_generation(store, candidate_ids=("cand-a", "cand-b"))
    epoch = _bind(store, generation_id, epoch, "cand-a", pr_number=1, head_sha="h1")
    result = store.select_winner(REPO, ISSUE, generation_id, "cand-a", "op-select", epoch, _acceptance(generation_id, "cand-a", 1, "h1", revision=1))
    assert result.selected

    snapshot = result.snapshot
    generation = snapshot.get_generation(generation_id)
    loser = generation.get_candidate("cand-b")
    assert loser.authority_state == CandidateAuthorityState.RETIRED
    assert loser.retirement_reason is not None and "cand-a" in loser.retirement_reason
    winner = generation.get_candidate("cand-a")
    assert winner.authority_state == CandidateAuthorityState.ACCEPTED or winner.authority_state == CandidateAuthorityState.NEVER_SUBMITTED
    # Selection alone does not force the winner's own authority state to a
    # provider-observed value; that is a separate, explicit observation.
    assert generation.winner_candidate_id == "cand-a"
