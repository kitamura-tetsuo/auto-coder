from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from auto_coder.ci_observation import (
    CheckExecutionIdentity,
    CheckObservation,
    CIConclusion,
    CIObservationSnapshot,
    ObservationAvailability,
    ObservationRequest,
    ObservationSubject,
)
from auto_coder.jules_candidate_selection import (
    CandidateAcceptanceEvidence,
    CandidateEvaluationDisposition,
    CandidateTarget,
    IndependentValidationVerdict,
    evaluate_candidate,
    select_accepted_candidate,
    selected_pair_authorized,
)
from auto_coder.jules_competition_ledger import (
    CandidateBindingObservation,
    JulesCompetitionLedger,
    SpeculativeGenerationBundle,
)

REPO = "kitamura-tetsuo/auto-coder"


def _evidence() -> CandidateAcceptanceEvidence:
    target = CandidateTarget(
        repository=REPO,
        issue_number=2074,
        generation_id="gen",
        candidate_id="candidate-a",
        pr_repository=REPO,
        pr_number=71,
        head_sha="head-a",
        base_sha="base-a",
        base_ref="main",
        captured_base_sha="base-a",
        captured_base_ref="main",
        issue_oracle_fingerprint="requirements-v1",
        binding_revision=1,
        generation_active=True,
        provenance_verified=True,
        pr_open=True,
        effective_diff_files=2,
    )
    ci = CIObservationSnapshot(
        ObservationSubject("https://api.github.com", REPO, 71, "head-a"),
        ObservationRequest("github-actions", "checks+workflows"),
        "cycle-1",
        4,
        ObservationAvailability.KNOWN,
        (CheckObservation(CheckExecutionIdentity("app", "check"), CIConclusion.SUCCESS),),
    )
    validation = IndependentValidationVerdict(
        result="PASS",
        head_sha="head-a",
        issue_oracle_fingerprint="requirements-v1",
        validation_revision=4,
        complete_requirement_coverage=True,
    )
    return CandidateAcceptanceEvidence(target, ci, True, False, True, validation, 4)


def test_acceptance_requires_positive_current_ci_and_exact_independent_pass() -> None:
    accepted = evaluate_candidate(_evidence())
    assert accepted.disposition is CandidateEvaluationDisposition.PASS
    assert accepted.acceptance is not None
    assert accepted.acceptance.head_sha == "head-a"
    assert accepted.acceptance.validation_revision == 4
    assert accepted.acceptance.invalidation_revision == 4

    empty = replace(_evidence(), ci=replace(_evidence().ci, availability=ObservationAvailability.KNOWN_EMPTY, facts=()))
    assert evaluate_candidate(empty).disposition is CandidateEvaluationDisposition.PENDING
    stale = replace(_evidence(), invalidation_revision=5)
    assert evaluate_candidate(stale).disposition is CandidateEvaluationDisposition.PENDING
    wrong_oracle = replace(_evidence(), validation=replace(_evidence().validation, issue_oracle_fingerprint="old"))
    assert evaluate_candidate(wrong_oracle).disposition is CandidateEvaluationDisposition.PENDING


def test_acceptance_rejects_repository_and_captured_base_mismatches() -> None:
    wrong_branch = replace(_evidence(), target=replace(_evidence().target, base_ref="release"))
    branch_result = evaluate_candidate(wrong_branch)
    assert branch_result.disposition is CandidateEvaluationDisposition.PENDING
    assert branch_result.acceptance is None

    wrong_base = replace(_evidence(), target=replace(_evidence().target, base_sha="other-base"))
    base_result = evaluate_candidate(wrong_base)
    assert base_result.disposition is CandidateEvaluationDisposition.PENDING
    assert base_result.acceptance is None

    wrong_repository = replace(_evidence(), target=replace(_evidence().target, pr_repository="other/project"))
    repository_result = evaluate_candidate(wrong_repository)
    assert repository_result.disposition is CandidateEvaluationDisposition.PENDING
    assert repository_result.acceptance is None


def test_only_definitive_artifact_failures_retire() -> None:
    empty_diff = replace(_evidence(), target=replace(_evidence().target, effective_diff_files=0))
    assert evaluate_candidate(empty_diff).disposition is CandidateEvaluationDisposition.RETIRE

    failed_ci = replace(
        _evidence(),
        ci=replace(
            _evidence().ci,
            facts=(CheckObservation(CheckExecutionIdentity("app", "check"), CIConclusion.FAILURE),),
        ),
        ci_success=False,
    )
    assert evaluate_candidate(failed_ci).disposition is CandidateEvaluationDisposition.RETIRE
    error = replace(_evidence(), validation=replace(_evidence().validation, result="ERROR"))
    assert evaluate_candidate(error).disposition is CandidateEvaluationDisposition.PENDING


def test_first_serialized_pass_selects_and_fences_sibling(tmp_path: Path) -> None:
    ledger = JulesCompetitionLedger(tmp_path / "competition.db")
    created = ledger.create_generation(
        REPO,
        2074,
        "create",
        0,
        SpeculativeGenerationBundle(
            candidate_ids=("candidate-a", "candidate-b"),
            issue_oracle_snapshot="requirements",
            issue_oracle_fingerprint="requirements-v1",
            source_branch="main",
        ),
    )
    generation = created.generation_id
    assert generation is not None
    bound = ledger.record_candidate_binding(
        REPO,
        2074,
        generation,
        "candidate-a",
        "bind-a",
        created.snapshot.epoch,
        CandidateBindingObservation(REPO, 71, "head-a", "base-a"),
    )
    evidence = replace(_evidence(), target=replace(_evidence().target, generation_id=generation))
    evaluation, selection = select_accepted_candidate(
        ledger,
        evidence,
        operation_id="select-a",
        expected_epoch=bound.snapshot.epoch,
    )
    assert evaluation.disposition is CandidateEvaluationDisposition.PASS
    assert selection is not None and selection.selected
    assert selected_pair_authorized(ledger, evidence.target, current_invalidation_revision=4)
    assert not selected_pair_authorized(ledger, evidence.target, current_invalidation_revision=5)
    committed = selection.snapshot.get_generation(generation)
    assert committed is not None
    assert committed.winner_validation_revision == 4
    assert committed.winner_invalidation_revision == 4
    sibling = selection.snapshot.get_generation(generation).get_candidate("candidate-b")
    assert sibling is not None and not sibling.is_eligible()


def test_selected_authority_rejects_head_or_oracle_change(tmp_path: Path) -> None:
    ledger = JulesCompetitionLedger(tmp_path / "competition.db")
    created = ledger.create_generation(
        REPO,
        2074,
        "create",
        0,
        SpeculativeGenerationBundle(candidate_ids=("candidate-a",), issue_oracle_snapshot="r", issue_oracle_fingerprint="requirements-v1", source_branch="main"),
    )
    assert created.generation_id
    bound = ledger.record_candidate_binding(REPO, 2074, created.generation_id, "candidate-a", "bind", created.snapshot.epoch, CandidateBindingObservation(REPO, 71, "head-a", "base-a"))
    evidence = replace(_evidence(), target=replace(_evidence().target, generation_id=created.generation_id))
    _, selection = select_accepted_candidate(ledger, evidence, operation_id="select", expected_epoch=bound.snapshot.epoch)
    assert selection is not None and selection.selected
    assert not selected_pair_authorized(ledger, replace(evidence.target, head_sha="head-b"), current_invalidation_revision=4)
    assert not selected_pair_authorized(ledger, replace(evidence.target, issue_oracle_fingerprint="requirements-v2"), current_invalidation_revision=4)
