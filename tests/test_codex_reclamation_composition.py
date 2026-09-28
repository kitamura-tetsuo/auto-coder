from pathlib import Path

from auto_coder.codex_retirement_observer import (
    CandidateSourceProvenance,
    CodexEvidenceState,
    CodexRetirementObservation,
    CodexTaskRetirementEvidence,
    OperationSettlementCertificate,
)
from auto_coder.codex_work_accounting import CodexWorkAccounting
from auto_coder.implementation_reclamation_scheduler import _collect_settle_and_retire_codex
from auto_coder.implementation_retirement import ImplementationPRObservation, PRTerminalState, RetirementStatus
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
