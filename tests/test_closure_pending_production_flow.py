"""Explicit regression-deliverable closure through the production same-attempt closure boundary (issue #2436).

Unlike the bridge-level regressions, every scenario starts at ``_handle_pr_merge`` with the real Strong
acceptance, publication, ordinary prompt/parser, accepted-gap reconciliation, explicit-deliverable
normalization, effective decision, retained same-attempt closure evidence, owning review cycle and the
real ``GitHubAppReviewer`` over a recording transport. The Issue's Requirement explicitly mandates a
regression test and the accepted finding is a regression-gap representation whose original prose
asserts that the deliverable is absent, so the conversion that caused the defect is reached.
"""

from __future__ import annotations

from typing import Any

import pytest

from auto_coder.adversarial_validator import AdversarialValidationContext, IssueRequirement
from auto_coder.ordinary_closure_evidence import OrdinaryClosureEvidenceRepository
from tests import test_accepted_finding_bridge as bridge_tests
from tests import test_effective_decision_pr_flow as flow_tests
from tests.test_accepted_finding_bridge import REPO, Env, env  # noqa: F401  (env is a shared fixture)
from tests.test_effective_decision_pr_flow import flow_env  # noqa: F401  (shared fixture)
from tests.test_effective_decision_pr_flow import ClosureScript, _repair_to, make_flow, ordinary_response

DELIVERABLE = "Add a regression test that builds and installs a wheel and reads the installed runtime."
ABSENT = "No committed test builds a wheel; tests only copy source."
ADDRESSED = ordinary_response("PRRT_accepted", status="ADDRESSED", evidence="tests/test_build_provenance.py:43-65 builds and installs a wheel")


@pytest.fixture(autouse=True)
def explicit_deliverable_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """One consistent explicit regression-deliverable contract for the accepted finding and the ordinary review."""
    original = bridge_tests.finding_json

    def finding(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return original(*args, **kwargs) | {"why_tests_admit_it": ABSENT, "expected_behavior": DELIVERABLE}

    monkeypatch.setattr(flow_tests, "finding_json", finding)
    monkeypatch.setattr(
        bridge_tests,
        "_context",
        lambda: AdversarialValidationContext(
            pr_diff="diff --git a/src/state.py b/src/state.py\n+guard = True",
            all_changed_files=["src/state.py"],
            issue_context="Issue #2401 requires a regression deliverable.",
            issue_requirements=[IssueRequirement("#2401/REQ-001", DELIVERABLE)],
        ),
    )


def _upheld_then_repaired(flow_env: Env, monkeypatch: pytest.MonkeyPatch, pr: int, origin: str = "cloud", **kwargs: Any):
    flow = make_flow(flow_env, monkeypatch, pr, origin, **kwargs)
    flow.accept_finding()
    flow.model_responses = [ordinary_response(flow.thread_id)]
    flow.run()  # independently upheld at H0: the original correction is handed to its originating route
    assert flow.handoffs == 1 and [review["event"] for review in flow.reviews] == ["REQUEST_CHANGES"]
    flow.h2 = _repair_to(flow, "repair-head")  # type: ignore[attr-defined]
    flow.saved_status = None
    flow.model_responses = [ADDRESSED]
    return flow


@pytest.mark.parametrize("origin", ["cloud", "local"])
@pytest.mark.parametrize("pr", [5701, 880213])
def test_completed_closure_passes_without_a_synthesized_violation_repair_or_second_model_call(flow_env: Env, monkeypatch: pytest.MonkeyPatch, origin: str, pr: int) -> None:
    flow = _upheld_then_repaired(flow_env, monkeypatch, pr, origin, resolve_on_approve=True)
    handoffs_before, calls_before, reviews_before = flow.handoffs, flow.model_calls, len(flow.reviews)
    script = ClosureScript(status="FIXED")

    actions = flow.run(closure=script)

    assert flow.model_calls == calls_before + 1 and script.closure_only_calls == [], actions  # one combined ordinary review, no closure-only call
    snapshot = flow_env.cycle.snapshot(pr)
    assert snapshot.open_findings == () and snapshot.accepted_closure is not None and snapshot.accepted_closure.head_sha == flow.h2
    assert flow.handoffs == handoffs_before  # no further code/test repair for the closed finding
    published = [review["event"] for review in flow.reviews[reviews_before:] if "Ordinary closure evidence" not in review["body"]]
    assert "REQUEST_CHANGES" not in published and published[-1:] == ["APPROVE"]
    assert not any("explicitly required regression deliverable is absent" in review["body"] for review in flow.reviews[reviews_before:])
    assert flow.merge.call_count == 1, actions


def test_pending_closure_publishes_no_repair_then_the_retained_attempt_completes_without_a_model_call(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = _upheld_then_repaired(flow_env, monkeypatch, 5702, resolve_on_approve=True)
    handoffs_before, reviews_before = flow.handoffs, len(flow.reviews)
    outage = ClosureScript(status="FIXED")
    outage.on_review = lambda: setattr(outage, "observable", False)  # owner acceptance is unavailable after the combined review returns
    flow.auto_status = True

    flow.run(closure=outage)

    pending = flow_env.cycle.snapshot(flow.pr)
    assert pending.accepted_closure is None and [item.finding_id for item in pending.open_findings] == ["finding-a"]
    assert len(OrdinaryClosureEvidenceRepository(REPO).pending_for_pr(flow.pr)) == 1  # the same attempt's evidence is retained
    held = flow.reviews[reviews_before:]
    assert held and all(review["event"] != "REQUEST_CHANGES" for review in held)  # BLOCKED/COMMENT, never a new actionable root
    assert flow.handoffs == handoffs_before and flow.merge.call_count == 0  # zero repair dispatch while acceptance is unavailable
    assert not any("explicitly required regression deliverable is absent" in review["body"] for review in held)

    calls_before = flow.model_calls
    restored = ClosureScript(status="FIXED")
    actions = flow.run(closure=restored)  # authority restored: same head, no dummy commit, no store reset

    assert flow.model_calls == calls_before and restored.calls == [], actions
    done = flow_env.cycle.snapshot(flow.pr)
    assert done.open_findings == () and done.accepted_closure is not None
    assert flow.handoffs == handoffs_before and flow.merge.call_count == 1, actions


def test_still_missing_deliverable_remains_an_implementation_repair_under_the_original_identity(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = make_flow(flow_env, monkeypatch, 5703, "cloud")
    flow.accept_finding()
    blocker_before = flow_env.ledger.get_snapshot("https://api.github.com", REPO, 5703).blockers
    flow.model_responses = [ordinary_response(flow.thread_id)]

    flow.run()

    assert flow.handoffs == 1 and [review["event"] for review in flow.reviews] == ["REQUEST_CHANGES"]
    blockers = flow_env.ledger.get_snapshot("https://api.github.com", REPO, 5703).blockers
    assert [blocker.blocker_id for blocker in blockers] == [blocker.blocker_id for blocker in blocker_before]  # no new logical blocker
    assert flow.retained().status == "NEEDS_FIX"


def test_legacy_saved_violation_is_not_replayed_as_a_repair_and_admits_one_current_combined_review(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A saved NEEDS_FIX at the repaired head, backed by no retained decision or handoff generation, is only history."""
    flow = make_flow(flow_env, monkeypatch, 5704, "cloud")
    flow.accept_finding()  # H0: accepted and published, but no ordinary attempt ever retained a decision
    flow.h2 = _repair_to(flow, "repair-head")  # type: ignore[attr-defined]
    assert flow.retained() is None and flow.handoffs == 0
    blockers_before = [blocker.blocker_id for blocker in flow_env.ledger.get_snapshot("https://api.github.com", REPO, 5704).blockers]
    flow.saved_status = "NEEDS_FIX"  # a legacy headline for this very head
    flow.model_responses = [ADDRESSED]
    calls_before = flow.model_calls
    script = ClosureScript(status="FIXED")

    actions = flow.run(closure=script)

    assert flow.model_calls == calls_before + 1 and script.closure_only_calls == [], actions  # one fresh combined ordinary review
    assert flow.handoffs == 0, actions  # the historical report is never sent as a repair request
    snapshot = flow_env.cycle.snapshot(5704)
    assert snapshot.open_findings == () and snapshot.accepted_closure is not None
    assert [blocker.blocker_id for blocker in flow_env.ledger.get_snapshot("https://api.github.com", REPO, 5704).blockers] == blockers_before


def test_current_saved_violation_with_a_retained_repair_decision_is_still_replayed_without_a_model_call(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = make_flow(flow_env, monkeypatch, 5705, "cloud")
    flow.accept_finding()
    flow.model_responses = [ordinary_response(flow.thread_id)]
    flow.run()  # upheld at H0: the repair decision is retained for this head
    assert flow.handoffs == 1 and flow.retained().head_sha == flow.head
    calls_before = flow.model_calls
    flow.saved_status = "NEEDS_FIX"

    flow.run()

    assert flow.model_calls == calls_before  # unchanged current evidence: no repeated reviewer work
