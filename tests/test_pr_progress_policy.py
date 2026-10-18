from dataclasses import replace

import pytest

pytestmark = pytest.mark.pr_repair_contract

from auto_coder.ci_observation import (
    CIConclusion,
    CIObservationSnapshot,
    ObservationAvailability,
    ObservationRequest,
    ObservationSubject,
    WorkflowExecutionIdentity,
    WorkflowObservation,
)
from auto_coder.pr_progress_policy import (
    CIEvidenceState,
    ProgressPhase,
    ProgressRoute,
    PRProgressInputs,
    PRProgressScope,
    select_pr_progress,
)

REPOSITORY = "owner/repo"
PR_NUMBER = 2503
HEAD = "a" * 40
SCOPE = PRProgressScope(REPOSITORY, PR_NUMBER, HEAD)


def _snapshot(availability: ObservationAvailability, conclusion: CIConclusion | None = None, *, repository=REPOSITORY, pr_number=PR_NUMBER, head=HEAD):
    facts = ()
    if conclusion is not None:
        facts = (WorkflowObservation(WorkflowExecutionIdentity("pr-tests", "17", 1), conclusion),)
    return CIObservationSnapshot(
        ObservationSubject("https://api.github.test", repository, pr_number, head),
        ObservationRequest("github-actions", "checks+workflows"),
        "cycle-1",
        1,
        availability,
        facts,
        unavailable_reason="provider unavailable" if availability is ObservationAvailability.UNAVAILABLE else None,
    )


@pytest.mark.parametrize(
    ("snapshot", "route", "state"),
    [
        (_snapshot(ObservationAvailability.KNOWN, CIConclusion.SUCCESS), ProgressRoute.ENTER_REVIEW, CIEvidenceState.PASSING),
        (_snapshot(ObservationAvailability.KNOWN, CIConclusion.PENDING), ProgressRoute.WAIT_FOR_CI, CIEvidenceState.PENDING),
        (_snapshot(ObservationAvailability.KNOWN, CIConclusion.FAILURE), ProgressRoute.REPAIR_FAILED_CI, CIEvidenceState.FAILING),
        (_snapshot(ObservationAvailability.KNOWN_EMPTY), ProgressRoute.WAIT_FOR_CI, CIEvidenceState.KNOWN_EMPTY),
        (_snapshot(ObservationAvailability.UNAVAILABLE), ProgressRoute.REPORT_CI_UNAVAILABLE, CIEvidenceState.UNAVAILABLE),
        (_snapshot(ObservationAvailability.PARTIAL), ProgressRoute.REPORT_CI_UNAVAILABLE, CIEvidenceState.UNAVAILABLE),
        (_snapshot(ObservationAvailability.SUPERSEDED), ProgressRoute.REPORT_CI_UNAVAILABLE, CIEvidenceState.UNAVAILABLE),
        (None, ProgressRoute.REPORT_CI_UNAVAILABLE, CIEvidenceState.MISSING),
        (_snapshot(ObservationAvailability.KNOWN, CIConclusion.SUCCESS, head="b" * 40), ProgressRoute.WAIT_FOR_CI, CIEvidenceState.SUBJECT_MISMATCH),
    ],
)
def test_handler_decision_table_preserves_ci_evidence_semantics(snapshot, route, state):
    decision = select_pr_progress(
        PRProgressInputs(
            SCOPE,
            ProgressPhase.IN_HANDLER,
            snapshot,
            True,
            True,
            ci_success=state is CIEvidenceState.PASSING,
            ci_pending=state is CIEvidenceState.PENDING,
        )
    )

    assert decision.route is route
    assert decision.ci_state is state


@pytest.mark.parametrize(
    ("enabled", "exhausted", "route"),
    [
        (True, False, ProgressRoute.REMEDIATE_CONFLICT),
        (False, False, ProgressRoute.WAIT_FOR_CI),
        (True, True, ProgressRoute.BLOCK_EXHAUSTED_CONFLICT),
    ],
)
def test_handler_conflict_precedes_pending_ci_only_when_authorized(enabled, exhausted, route):
    decision = select_pr_progress(
        PRProgressInputs(
            SCOPE,
            ProgressPhase.IN_HANDLER,
            _snapshot(ObservationAvailability.KNOWN, CIConclusion.PENDING),
            False,
            enabled,
            exhausted,
            ci_pending=True,
        )
    )

    assert decision.route is route


def test_candidate_and_handler_keep_distinct_known_empty_projection():
    empty = _snapshot(ObservationAvailability.KNOWN_EMPTY)
    candidate = select_pr_progress(PRProgressInputs(SCOPE, ProgressPhase.CANDIDATE_COLLECTION, empty, True, True))
    handler = select_pr_progress(PRProgressInputs(SCOPE, ProgressPhase.IN_HANDLER, empty, True, True))
    conflicting_candidate = select_pr_progress(PRProgressInputs(SCOPE, ProgressPhase.CANDIDATE_COLLECTION, empty, False, True))

    assert candidate.route is ProgressRoute.EXCLUDE_CANDIDATE
    assert handler.route is ProgressRoute.WAIT_FOR_CI
    assert conflicting_candidate.route is ProgressRoute.ADMIT_CANDIDATE


def test_scope_mismatch_cannot_carry_green_authority_to_new_head():
    green_h1 = _snapshot(ObservationAvailability.KNOWN, CIConclusion.SUCCESS)
    h2 = replace(SCOPE, head_sha="b" * 40)

    decision = select_pr_progress(PRProgressInputs(h2, ProgressPhase.IN_HANDLER, green_h1, True, True))

    assert decision.route is ProgressRoute.WAIT_FOR_CI
    assert decision.ci_state is CIEvidenceState.SUBJECT_MISMATCH


@pytest.mark.parametrize("obsolete", [CIConclusion.FAILURE, CIConclusion.PENDING])
def test_interpreted_success_ignores_obsolete_raw_attempt(obsolete):
    current = _snapshot(ObservationAvailability.KNOWN, CIConclusion.SUCCESS)
    old = WorkflowObservation(WorkflowExecutionIdentity("pr-tests", "17", 1), obsolete)
    newest = WorkflowObservation(WorkflowExecutionIdentity("pr-tests", "17", 2), CIConclusion.SUCCESS)
    snapshot = replace(current, facts=(old, newest))

    decision = select_pr_progress(PRProgressInputs(SCOPE, ProgressPhase.IN_HANDLER, snapshot, True, True, ci_success=True))

    assert decision.route is ProgressRoute.ENTER_REVIEW
    assert decision.ci_state is CIEvidenceState.PASSING
