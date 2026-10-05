"""A closure-pending accepted Strong gap is a lifecycle wait, not a current-head violation.

Every scenario starts at the production Strong acceptance path, then runs the real
ordinary ``run_adversarial_validation`` (prompt, parser, accepted-gap
reconciliation, explicit-deliverable normalization) with a controlled model
response, and finally derives the effective decision from the result.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.accepted_finding_bridge import OrdinaryDisposition
from auto_coder.adversarial_validator import AdversarialValidationContext, IssueRequirement, run_adversarial_validation
from auto_coder.automation_config import AutomationConfig
from auto_coder.effective_decision_application import HANDOFF_NOT_REQUIRED, HANDOFF_PENDING, build_retained_record, derive_application, raw_ordinary_clear, settle_accepted_gaps
from auto_coder.effective_review_decision import EffectiveNextAction
from auto_coder.review_thread_validation import ClaimedReviewThread
from auto_coder.two_tier_pr_gate import TwoTierPrGate
from tests.test_accepted_finding_bridge import Env, _close, _commit, _only, accept_strong, env, finding_json, published_roots, save_empty_session  # noqa: F401

REQ = "#2401/REQ-001"
REQ_RUNTIME = "#2401/REQ-002"
DELIVERABLE = "Add a regression test that builds and installs a wheel and reads the installed runtime."
RUNTIME = "Preserve provenance fields on malformed input."
ABSENT = "No committed test builds a wheel; tests only copy source."


def _context(*, deliverable: bool = True) -> AdversarialValidationContext:
    return AdversarialValidationContext(
        pr_diff="diff --git a/src/state.py b/src/state.py\n+guard = True",
        all_changed_files=["src/state.py"],
        issue_context="Issue #2401.",
        issue_requirements=[IssueRequirement(REQ, DELIVERABLE if deliverable else RUNTIME), IssueRequirement(REQ_RUNTIME, RUNTIME)],
    )


def _response(*, coverage: str = "VERIFIED", findings: Optional[list[dict[str, Any]]] = None, gaps: Optional[list[dict[str, Any]]] = None, threads: Optional[list[dict[str, Any]]] = None, result: str = "PASS") -> str:
    return json.dumps(
        {
            "result": result,
            "summary": "Production behavior is correct.",
            "requirement_coverage": [
                {"requirement_id": REQ, "status": coverage, "evidence": "Reviewed."},
                {"requirement_id": REQ_RUNTIME, "status": "VERIFIED", "evidence": "Reviewed."},
            ],
            "findings": findings or [],
            "test_oracle_gaps": gaps or [],
            "thread_dispositions": threads or [],
        }
    )


def _run(env: Env, pr: int, response: str, *, deliverable: bool = True, claimed: tuple[ClaimedReviewThread, ...] = (), head: str = ""):
    manager = MagicMock()
    manager.get_current_backend_identity.return_value = ("reviewer", "codex", "strong")
    manager._last_session_id = "provider-session"
    manager._last_continue_session_resumed = True
    manager.continue_session.return_value = response
    with patch("auto_coder.adversarial_validator.build_adversarial_validation_context", return_value=_context(deliverable=deliverable)), patch("auto_coder.adversarial_validator.run_llm_prompt", return_value=response):
        return run_adversarial_validation(REPO, {"number": pr, "head": {"sha": head or env.head}, "base": {"sha": env.base}}, AutomationConfig(), backend_manager=manager, session_registry=env.registry, claimed_review_threads=claimed, accepted_finding_bridge=env.bridge())


REPO = "owner/repo"


def _accept(env: Env, pr: int, *, requirement: str = REQ) -> str:
    save_empty_session(env, pr)
    accept_strong(env, pr, [finding_json("wheel", requirement=requirement) | {"why_tests_admit_it": ABSENT, "expected_behavior": DELIVERABLE}])
    return _only(env.bridge().project(env.target(pr)), "wheel").known_gap_id


def _thread(gap_id: str, status: str) -> tuple[ClaimedReviewThread, list[dict[str, Any]]]:
    thread = ClaimedReviewThread(thread_id="T1", original_finding=f"Gap identity: `{gap_id}`\n`{REQ}`: {DELIVERABLE}")
    disposition = {"thread_id": "T1", "status": status, "rationale": "Independently inspected the repaired fixture.", "evidence": "tests/test_build_provenance.py:43-65 builds and installs a wheel; `pytest tests/test_build_provenance.py` passes."}
    return thread, [disposition]


@pytest.mark.parametrize("pr", [2436, 41])
@pytest.mark.parametrize("assessment", ["addressed", "unobserved"])
def test_pending_closure_is_not_a_current_violation(env: Env, pr: int, assessment: str) -> None:
    gap_id = _accept(env, pr)
    claimed: tuple[ClaimedReviewThread, ...] = ()
    threads = None
    if assessment == "addressed":
        thread, threads = _thread(gap_id, "ADDRESSED")
        claimed = (thread,)
    result = _run(env, pr, _response(gaps=[{"gap_id": gap_id, "status": "OPEN"}], threads=threads), claimed=claimed)

    # The retained obligation is not rewritten into a synthesized implementation finding.
    assert result.findings == []
    assert {entry.requirement_id: entry.status for entry in result.requirement_coverage} == {REQ: "VERIFIED", REQ_RUNTIME: "VERIFIED"}
    assert [gap.gap_id for gap in result.open_test_oracle_gaps] == [gap_id]
    assert result.result == "NEEDS_TESTS"
    assert "absent" not in (result.diagnostic_reason or "")

    # The effective decision stays nonapproving, requests no new implementation repair and keeps the original identity.
    application = derive_application(result, result.accepted_finding_projection)
    assert not application.decision.approval_eligible
    assert application.result.findings == []
    assert len(env.ledger.get_snapshot("https://api.github.com", REPO, pr).blockers) == 1

    # The ordinary attempt's own evidence stays complete, so same-attempt closure acceptance remains reachable.
    assert raw_ordinary_clear(settle_accepted_gaps(result, result.accepted_finding_projection))


def test_independently_upheld_absence_remains_a_genuine_violation(env: Env) -> None:
    pr = 2437
    gap_id = _accept(env, pr)
    thread, threads = _thread(gap_id, "STILL_VALID")
    result = _run(env, pr, _response(gaps=[{"gap_id": gap_id, "status": "OPEN"}], threads=threads, result="NEEDS_TESTS"), claimed=(thread,))
    assert result.result == "NEEDS_FIX"
    assert [finding.correction_identity for finding in result.findings] == [gap_id]
    assert {entry.requirement_id: entry.status for entry in result.requirement_coverage}[REQ] == "VIOLATED"


def test_pending_closure_does_not_erase_an_independent_defect_on_the_same_requirement(env: Env) -> None:
    pr = 2438
    gap_id = _accept(env, pr)
    thread, threads = _thread(gap_id, "ADDRESSED")
    other = {
        "requirement_id": REQ,
        "finding_identity": "other-defect",
        "correction_identity": "other-defect",
        "violated_requirement": DELIVERABLE,
        "counterexample": "A malformed wheel metadata file drops the build tag.",
        "required_behavior": DELIVERABLE,
        "actual_behavior": "The build tag is dropped.",
        "evidence": "src/state.py:9",
        "reachability": "src/state.py:build_tag",
        "anchor_path": "src/state.py",
        "anchor_line": 9,
        "evidence_classification": "DEMONSTRATED",
    }
    result = _run(env, pr, _response(coverage="VIOLATED", findings=[other], gaps=[{"gap_id": gap_id, "status": "OPEN"}], threads=threads, result="NEEDS_FIX"), claimed=(thread,))
    assert result.result == "NEEDS_FIX"
    assert [finding.finding_identity for finding in result.findings] == ["other-defect"]
    assert gap_id not in {finding.correction_identity for finding in result.findings}
    assert {entry.requirement_id: entry.status for entry in result.requirement_coverage}[REQ] == "VIOLATED"
    assert [gap.gap_id for gap in result.open_test_oracle_gaps] == [gap_id]


def test_missing_test_under_runtime_only_requirement_stays_a_test_gap(env: Env) -> None:
    pr = 2439
    gap_id = _accept(env, pr)
    result = _run(env, pr, _response(gaps=[{"gap_id": gap_id, "status": "OPEN"}]), deliverable=False)
    assert result.findings == []
    assert {entry.requirement_id: entry.status for entry in result.requirement_coverage}[REQ] == "VERIFIED"
    assert result.result == "NEEDS_TESTS"


def test_completed_closure_rederives_pass_and_pending_and_upheld_route_differently(env: Env) -> None:
    pr = 2440
    gap_id = _accept(env, pr)
    inputs_strong = env.cycle.snapshot(pr)
    assert inputs_strong.accepted_strong_round is not None

    # Pending: nonapproving, and no repair handoff is requested for the retained obligation.
    pending = _run(env, pr, _response(gaps=[{"gap_id": gap_id, "status": "OPEN"}]))
    addressed = OrdinaryDisposition(status="ADDRESSED", rationale="Independently inspected the repaired fixture.", evidence="tests/test_build_provenance.py:43-65 builds and installs a wheel", finding_id="wheel")
    addressed_projection = env.bridge().project(env.target(pr), dispositions=[addressed])
    application = derive_application(pending, addressed_projection)
    assert application.decision.status == "BLOCKED" and application.decision.next_action is EffectiveNextAction.CLOSURE_ACCEPTANCE
    assert not application.decision.approval_eligible
    pending_record = build_retained_record(application.decision, application.projection, env.head, env.base)
    assert pending_record.handoff == HANDOFF_NOT_REQUIRED
    assert application.result.findings == []

    # Upheld: a genuine violation routes to a code/test repair under the original identity.
    thread, threads = _thread(gap_id, "STILL_VALID")
    upheld = _run(env, pr, _response(gaps=[{"gap_id": gap_id, "status": "OPEN"}], threads=threads, result="NEEDS_TESTS"), claimed=(thread,))
    upheld_application = derive_application(upheld, upheld.accepted_finding_projection)
    assert not upheld_application.decision.approval_eligible and upheld_application.decision.next_action in {EffectiveNextAction.IMPLEMENTATION_REPAIR, EffectiveNextAction.FOCUSED_TEST_REPAIR}
    assert build_retained_record(upheld_application.decision, upheld_application.projection, env.head, env.base).handoff == HANDOFF_PENDING

    # Completed closure: the owning cycle accepts FIXED/BOUNDED at the repair head; the next
    # ordinary attempt (same production path) rederives an approval-eligible PASS with no obligations.
    h2 = _commit(env.worktree, "repair-head")
    from auto_coder.pr_processor import TwoTierGateInputs
    from tests.test_accepted_finding_bridge import CONTRACT, POLICY

    _close(env, pr, TwoTierGateInputs(TwoTierPrGate(REPO, env.cycle), CONTRACT, POLICY, h2, env.base), h2, {"wheel": "FIXED"})
    done = _run(env, pr, _response(), head=h2)
    done_application = derive_application(done, done.accepted_finding_projection)
    assert done_application.decision.status == "PASS" and done_application.decision.approval_eligible
    assert done_application.result.findings == [] and done_application.result.open_test_oracle_gaps == []
    assert build_retained_record(done_application.decision, done_application.projection, h2, env.base).handoff == HANDOFF_NOT_REQUIRED


def test_upheld_deliverable_is_associated_through_the_native_strong_root(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The published Strong root carries no ordinary gap prose; association is by its exact native root."""
    pr = 2441
    gap_id = _accept(env, pr)
    strong = env.cycle.snapshot(pr).accepted_strong_round
    assert strong is not None
    TwoTierPrGate(REPO, env.cycle).state.acknowledge_publication(pr, strong.round_id)
    observation = published_roots(env, pr, monkeypatch, root_ids={"wheel": 4242})
    assert _only(env.bridge().project(env.target(pr), observation), "wheel").root_comment_ids == (4242,)

    thread = ClaimedReviewThread(thread_id="T-native", root_comment_database_id=4242, original_finding="### wheel\n\n**Requirements:** #2401/REQ-001")
    threads = [{"thread_id": "T-native", "status": "STILL_VALID", "rationale": "Independently inspected.", "evidence": "tests/test_build_provenance.py still only copies source; no wheel is built."}]
    result = _run(env, pr, _response(gaps=[{"gap_id": gap_id, "status": "OPEN"}], threads=threads, result="NEEDS_TESTS"), claimed=(thread,))
    assert result.result == "NEEDS_FIX"
    assert [finding.correction_identity for finding in result.findings] == [gap_id]
    assert {entry.requirement_id: entry.status for entry in result.requirement_coverage}[REQ] == "VIOLATED"

    # The effective decision keeps the original identity but routes it as an implementation repair, without a second gap obligation.
    application = derive_application(result, result.accepted_finding_projection)
    assert application.decision.status == "NEEDS_FIX" and application.decision.next_action is EffectiveNextAction.IMPLEMENTATION_REPAIR
    assert [finding.correction_identity for finding in application.result.findings] == [gap_id]
    assert application.result.open_test_oracle_gaps == []
