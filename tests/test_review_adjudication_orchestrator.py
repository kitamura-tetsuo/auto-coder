"""Tests for applying authorized review adjudications to a PR's lifecycle.

Snapshots are built through the actual production model and GitHub-boundary
modules (``review_adjudication``/``review_adjudication_github``) -- the same
``new_context``/``reconcile_thread`` path #2018's own tests use -- rather than
constructing an ``AdjudicationResult`` by hand, so these regressions exercise
the real decision graph an authorized reply produces.
"""

import uuid
from pathlib import Path

from auto_coder.adversarial_validator import format_test_oracle_gap_comment
from auto_coder.review_adjudication import AdjudicationStatus, Decision, render_decision
from auto_coder.review_adjudication_github import AdjudicationContextStore, AdjudicationSnapshot, IssueEvidence, PullRequestBinding, build_issue_contracts, new_context, reconcile_thread
from auto_coder.review_adjudication_orchestrator import AdjudicationEffectStore, extract_test_oracle_gap_id, plan_adjudication_effects
from auto_coder.reviewer_session_registry import TestOracleGap
from auto_coder.util.gh_cache import ReviewThread, ReviewThreadComment

BODY = """## Objective

Keep the boundary exact.

## Requirements

REQ-001: Preserve the raw value.
"""

ADJUDICATOR_ID = 8
ROOT_AUTHOR_ID = 7
HEAD_SHA = "a" * 40


def _contracts():
    return build_issue_contracts([IssueEvidence(90, 9, "title", BODY)])


def _thread(root_body: str = "finding", root_id: int = 10, thread_id: str = "T1") -> ReviewThread:
    return ReviewThread(id=thread_id, comments=[ReviewThreadComment(root_id, root_body, "bot", ROOT_AUTHOR_ID, "Bot", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")])


def _reply(decision: Decision, comment_id: int, created_at: str = "2026-01-02T00:00:00Z") -> ReviewThreadComment:
    return ReviewThreadComment(comment_id, render_decision(decision), "human", ADJUDICATOR_ID, "User", created_at, created_at, in_reply_to_id=10)


def _snapshot_after_decision(store: AdjudicationContextStore, thread: ReviewThread, verdict: str, directive: str, decision_id: str = "") -> AdjudicationSnapshot:
    contracts = _contracts()
    binding = PullRequestBinding(3, "o/r", 4, HEAD_SHA, "b" * 40, "main")
    context = new_context(binding, thread, contracts)
    decision = Decision(
        decision_id=decision_id or str(uuid.uuid4()),
        context_id=context.context_id,
        head_sha=HEAD_SHA,
        contract_digest=context.contract_digest,
        verdict=verdict,
        directive=directive,
        supersedes=(),
        rationale="Remove the accidental startup-path change; keep the worker correction.",
        source="chatgpt-assisted",
    )
    thread.comments.append(_reply(decision, 11))
    ledger = store.register(context, "r1")
    result = reconcile_thread(ledger, thread, [ADJUDICATOR_ID], [ROOT_AUTHOR_ID])
    assert result.status is AdjudicationStatus.APPLICABLE
    assert result.verdict == verdict
    return AdjudicationSnapshot(ledger.context, thread.comments[0].body, (9,), ROOT_AUTHOR_ID, result.source_comment_id, result, "r1")


def test_uphold_plans_bounded_repair_once_per_generation(tmp_path: Path) -> None:
    store = AdjudicationContextStore(tmp_path / "state.sqlite")
    effects = AdjudicationEffectStore(tmp_path / "effects.sqlite")
    snapshot = _snapshot_after_decision(store, _thread(), "UPHOLD", "FIX")

    plan = plan_adjudication_effects([snapshot], effects)
    assert len(plan.upholds) == 1
    assert not plan.overrules and not plan.reopens
    upheld = plan.upholds[0]
    assert upheld.rationale == "Remove the accidental startup-path change; keep the worker correction."

    effects.begin(upheld.context_id, "o/r", 4, upheld.decision_id, upheld.head_sha, upheld.contract_digest, "UPHOLD")
    effects.finish(upheld.context_id, "delivered")

    # Same generation, already delivered: no repeated plan.
    plan_again = plan_adjudication_effects([snapshot], effects)
    assert not plan_again.upholds
    assert not effects.force_revalidation_needed("o/r", 4)


def test_uphold_retries_while_delivery_is_unconfirmed(tmp_path: Path) -> None:
    store = AdjudicationContextStore(tmp_path / "state.sqlite")
    effects = AdjudicationEffectStore(tmp_path / "effects.sqlite")
    snapshot = _snapshot_after_decision(store, _thread(), "UPHOLD", "FIX")
    plan = plan_adjudication_effects([snapshot], effects)
    upheld = plan.upholds[0]
    effects.begin(upheld.context_id, "o/r", 4, upheld.decision_id, upheld.head_sha, upheld.contract_digest, "UPHOLD")
    effects.finish(upheld.context_id, "pending")

    assert effects.force_revalidation_needed("o/r", 4)
    plan_again = plan_adjudication_effects([snapshot], effects)
    assert len(plan_again.upholds) == 1


def test_conflict_and_undecided_produce_no_effect(tmp_path: Path) -> None:
    store = AdjudicationContextStore(tmp_path / "state.sqlite")
    effects = AdjudicationEffectStore(tmp_path / "effects.sqlite")
    contracts = _contracts()
    binding = PullRequestBinding(3, "o/r", 4, HEAD_SHA, "b" * 40, "main")
    thread = _thread()
    context = new_context(binding, thread, contracts)
    undecided = Decision(str(uuid.uuid4()), context.context_id, HEAD_SHA, context.contract_digest, "UNDECIDED", "NONE", (), "still investigating", "chatgpt-assisted")
    thread.comments.append(_reply(undecided, 11))
    ledger = store.register(context, "r1")
    result = reconcile_thread(ledger, thread, [ADJUDICATOR_ID], [ROOT_AUTHOR_ID])
    assert result.status is AdjudicationStatus.UNDECIDED
    snapshot = AdjudicationSnapshot(ledger.context, thread.comments[0].body, (9,), ROOT_AUTHOR_ID, result.source_comment_id, result, "r1")

    plan = plan_adjudication_effects([snapshot], effects)
    assert not plan
    assert not effects.force_revalidation_needed("o/r", 4)


def test_overrule_retires_gap_only_when_it_is_the_sole_owner(tmp_path: Path) -> None:
    store = AdjudicationContextStore(tmp_path / "state.sqlite")
    effects = AdjudicationEffectStore(tmp_path / "effects.sqlite")
    gap = TestOracleGap(
        gap_id="TOG-abc123",
        requirement_id="REQ-001",
        requirement_text="Preserve the raw value.",
        authoritative_boundary="boundary",
        invariant="invariant",
        plausible_incorrect_implementation="impl",
        why_tests_still_pass="reason",
        material_consequence="consequence",
        focused_regression_scenario="scenario",
        anchor_path="src/a.py",
    )
    root_body = format_test_oracle_gap_comment(gap)
    assert extract_test_oracle_gap_id(root_body) == "TOG-abc123"

    snapshot = _snapshot_after_decision(store, _thread(root_body=root_body), "OVERRULE", "NO_CHANGE")
    plan = plan_adjudication_effects([snapshot], effects)
    assert len(plan.overrules) == 1
    overrule = plan.overrules[0]
    assert overrule.gap_id == "TOG-abc123"
    assert overrule.gap_still_contributed_elsewhere is False


def test_overrule_leaves_shared_gap_open_while_another_finding_still_contributes(tmp_path: Path) -> None:
    store = AdjudicationContextStore(tmp_path / "state.sqlite")
    effects = AdjudicationEffectStore(tmp_path / "effects.sqlite")
    gap = TestOracleGap(
        gap_id="TOG-shared01",
        requirement_id="REQ-001",
        requirement_text="Preserve the raw value.",
        authoritative_boundary="boundary",
        invariant="invariant",
        plausible_incorrect_implementation="impl",
        why_tests_still_pass="reason",
        material_consequence="consequence",
        focused_regression_scenario="scenario",
        anchor_path="src/a.py",
    )
    root_body = format_test_oracle_gap_comment(gap)

    overruled_snapshot = _snapshot_after_decision(store, _thread(root_body=root_body, root_id=10, thread_id="T1"), "OVERRULE", "NO_CHANGE")

    # A second, still-open root thread contributes to the exact same gap and
    # has no adjudication decision at all (AdjudicationStatus.NONE) -- it
    # remains a live contribution (AS-007).
    contracts = _contracts()
    binding = PullRequestBinding(3, "o/r", 4, HEAD_SHA, "b" * 40, "main")
    other_thread = _thread(root_body=root_body, root_id=20, thread_id="T2")
    other_context = new_context(binding, other_thread, contracts)
    other_ledger = store.register(other_context, "r1")
    other_result = reconcile_thread(other_ledger, other_thread, [ADJUDICATOR_ID], [ROOT_AUTHOR_ID])
    assert other_result.status is AdjudicationStatus.NONE
    other_snapshot = AdjudicationSnapshot(other_ledger.context, other_thread.comments[0].body, (9,), ROOT_AUTHOR_ID, None, other_result, "r1")

    plan = plan_adjudication_effects([overruled_snapshot, other_snapshot], effects)
    assert len(plan.overrules) == 1
    assert plan.overrules[0].gap_still_contributed_elsewhere is True


def test_superseded_overrule_plans_a_reopen(tmp_path: Path) -> None:
    store = AdjudicationContextStore(tmp_path / "state.sqlite")
    effects = AdjudicationEffectStore(tmp_path / "effects.sqlite")
    thread = _thread()
    contracts = _contracts()
    binding = PullRequestBinding(3, "o/r", 4, HEAD_SHA, "b" * 40, "main")
    context = new_context(binding, thread, contracts)
    overrule = Decision(str(uuid.uuid4()), context.context_id, HEAD_SHA, context.contract_digest, "OVERRULE", "NO_CHANGE", (), "not a real defect", "chatgpt-assisted")
    thread.comments.append(_reply(overrule, 11, "2026-01-02T00:00:00Z"))
    ledger = store.register(context, "r1")
    first_result = reconcile_thread(ledger, thread, [ADJUDICATOR_ID], [ROOT_AUTHOR_ID])
    assert first_result.status is AdjudicationStatus.APPLICABLE

    first_snapshot = AdjudicationSnapshot(ledger.context, thread.comments[0].body, (9,), ROOT_AUTHOR_ID, first_result.source_comment_id, first_result, "r1")
    plan = plan_adjudication_effects([first_snapshot], effects)
    overrule_effect = plan.overrules[0]
    effects.begin(overrule_effect.context_id, "o/r", 4, overrule_effect.decision_id, overrule_effect.head_sha, overrule_effect.contract_digest, "OVERRULE", gap_id="")
    effects.finish(overrule_effect.context_id, "retired")

    # An applicable UPHOLD supersedes the OVERRULE: the prior override loses
    # its authority even though its thread is still resolved on GitHub
    # (REQ-007, AS-003/AS-005 style supersession).
    upheld = Decision(str(uuid.uuid4()), context.context_id, HEAD_SHA, context.contract_digest, "UPHOLD", "FIX", (overrule.decision_id,), "actually needs a fix", "chatgpt-assisted")
    thread.comments.append(_reply(upheld, 12, "2026-01-03T00:00:00Z"))
    second_result = reconcile_thread(ledger, thread, [ADJUDICATOR_ID], [ROOT_AUTHOR_ID])
    assert second_result.status is AdjudicationStatus.APPLICABLE
    assert second_result.verdict == "UPHOLD"
    second_snapshot = AdjudicationSnapshot(ledger.context, thread.comments[0].body, (9,), ROOT_AUTHOR_ID, second_result.source_comment_id, second_result, "r2")

    plan_after = plan_adjudication_effects([second_snapshot], effects)
    assert len(plan_after.reopens) == 1
    assert plan_after.reopens[0].context_id == overrule_effect.context_id
    # The new authority (UPHOLD) is planned independently of the reversal.
    assert len(plan_after.upholds) == 1
