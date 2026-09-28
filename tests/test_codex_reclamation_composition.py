from pathlib import Path

import pytest

from auto_coder.codex_retirement_observer import (
    CandidateSourceProvenance,
    CodexEvidenceState,
    CodexRetirementObservation,
    CodexTaskRetirementEvidence,
    OperationSettlementCertificate,
)
from auto_coder.codex_work_accounting import CodexWorkAccounting
from auto_coder.implementation_reclamation_scheduler import (
    RECLAMATION_RECHECK_SECONDS,
    ReclamationObligationStore,
    _collect_settle_and_retire_codex,
    run_due_reclamation_checks,
    schedule_reevaluation,
)
from auto_coder.implementation_retirement import ImplementationPRObservation, PRTerminalState, RetirementResult, RetirementStatus
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository


def test_terminal_codex_work_is_settled_and_retired_under_one_owner_guard(tmp_path: Path, monkeypatch) -> None:
    owner = ImplementationOwner("issue", 2336)
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    assert slots.reserve(owner)
    incarnation = slots.owner_incarnation(owner)
    assert incarnation is not None
    accounting = CodexWorkAccounting(slots)
    accounting.initialize_fresh(owner, incarnation)
    assert slots.record_implementation_pr(owner, 91)
    accounting.register(
        owner,
        incarnation,
        logical_operation_id="initial",
        kind="submission",
        source_request_id="request-1",
        task_id="task_e_terminal",
    )

    class Reconstructor:
        @staticmethod
        def consistency_ids(_sources: tuple[str, ...]) -> dict[str, str]:
            return {}

    monkeypatch.setattr(
        "auto_coder.codex_work_reconstruction.production_codex_reconstructor",
        lambda _repository, _issue: Reconstructor(),
    )

    def collect(_repository, _owner, _incarnation, _slots, snapshot, _runs, _github, *, wham=None, attributions=None):
        return CodexRetirementObservation(
            "owner/repo",
            owner,
            incarnation,
            snapshot.revision,
            snapshot.revision,
            (CodexTaskRetirementEvidence("task_e_terminal", 0, CodexEvidenceState.TERMINAL, "turn-terminal"),),
            (ImplementationPRObservation(91, PRTerminalState.MERGED, True),),
            (OperationSettlementCertificate("initial", "task_e_terminal", "turn-terminal", "request-1"),),
            (CandidateSourceProvenance("all-sources", "stable", True),),
        )

    monkeypatch.setattr("auto_coder.codex_retirement_observer.collect_codex_retirement_observation", collect)
    result = _collect_settle_and_retire_codex(owner, slots, object(), object(), object(), None)

    assert result.status is RetirementStatus.RELEASED
    assert slots.owner_incarnation(owner) is None
    retired = slots._read_retired()[incarnation]
    assert retired["implementation_prs"] == [91]
    assert retired["provider_sessions"] == ["task_e_terminal"]
    operation = retired["codex_work_accounting"]["operations"]["initial"]
    assert operation["phase"] == "settled"
    assert operation["settlement_evidence_id"] == "codex-retirement:turn-terminal"


def test_codex_provider_is_not_an_unsupported_provider() -> None:
    from auto_coder.implementation_retirement import (
        ImplementationRetirementObservation,
        ProviderSessionObservation,
        SessionTerminalState,
        evaluate_retirement_predicate,
    )

    result = evaluate_retirement_predicate(
        ImplementationRetirementObservation(
            "owner/repo",
            ImplementationOwner("issue", 2336),
            "incarnation",
            1,
            (ImplementationPRObservation(91, PRTerminalState.CLOSED),),
            provider_sessions=(ProviderSessionObservation("task_e_terminal", "codex-cloud", SessionTerminalState.ENDED),),
        )
    )
    assert result.status is RetirementStatus.RELEASED


def _due_codex_owner(tmp_path: Path) -> tuple[ImplementationSlotRepository, ImplementationOwner, ReclamationObligationStore]:
    owner = ImplementationOwner("issue", 2336)
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    assert slots.reserve(owner)
    incarnation = slots.owner_incarnation(owner)
    assert incarnation is not None
    CodexWorkAccounting(slots).initialize_fresh(owner, incarnation)
    store = ReclamationObligationStore.for_slots(slots)
    assert schedule_reevaluation(owner, slots, store, due_at=100.0)
    return slots, owner, store


def test_due_codex_release_clears_obligation_and_wakes_capacity(tmp_path: Path, monkeypatch) -> None:
    slots, _owner, store = _due_codex_owner(tmp_path)
    wakes: list[str] = []
    monkeypatch.setattr(
        "auto_coder.implementation_reclamation_scheduler._collect_settle_and_retire_codex",
        lambda *_args: RetirementResult(RetirementStatus.RELEASED),
    )

    released = run_due_reclamation_checks(
        slots,
        store,
        github_client=object(),
        on_capacity_freed=lambda: wakes.append("freed"),
        now=100.0,
    )

    assert released == 1
    assert wakes == ["freed"]
    assert store.all() == ()


@pytest.mark.parametrize(
    "status",
    (RetirementStatus.RETAINED_ACTIVE, RetirementStatus.RETAINED_UNKNOWN, RetirementStatus.STALE_OBSERVATION),
)
def test_due_codex_retention_reschedules_same_obligation(tmp_path: Path, monkeypatch, status: RetirementStatus) -> None:
    slots, owner, store = _due_codex_owner(tmp_path)
    incarnation = slots.owner_incarnation(owner)
    monkeypatch.setattr(
        "auto_coder.implementation_reclamation_scheduler._collect_settle_and_retire_codex",
        lambda *_args: RetirementResult(
            status,
            responsible_members=("task:active",),
            diagnostic="Codex task is active",
        ),
    )

    released = run_due_reclamation_checks(slots, store, github_client=object(), now=100.0)

    assert released == 0
    assert store.due(100.0) == ()
    pending = store.all()
    assert len(pending) == 1
    assert pending[0].owner == owner
    assert pending[0].incarnation == incarnation
    assert pending[0].next_due_at == 100.0 + RECLAMATION_RECHECK_SECONDS
    assert pending[0].last_reason == f"{status.value}|Codex task is active|task:active"
