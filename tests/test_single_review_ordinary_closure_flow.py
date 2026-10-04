"""Production-path regressions for one combined ordinary review per repaired head (issue #2407).

Every scenario starts at ``_handle_pr_merge`` with the real strong acceptance
store, the real ``run_adversarial_validation`` prompt/parse path, the real
attempt registry, retained-evidence store, review-cycle owner and the real
``GitHubAppReviewer`` over a recording HTTP transport. Only the model, GitHub
and merge endpoints are controlled. Every scenario counts actual reviewer
invocations, not calls to any named closure helper.
"""

from __future__ import annotations

import pytest

from auto_coder.adversarial_validation_attempts import AdversarialValidationAttemptRepository
from auto_coder.ordinary_closure_evidence import EvidenceState, OrdinaryClosureEvidence, OrdinaryClosureEvidenceRepository
from auto_coder.pr_review_cycle import PrReviewCycleRepository
from tests.test_accepted_finding_bridge import REPO, Env, env  # noqa: F401  (env is a shared fixture)
from tests.test_effective_decision_pr_flow import flow_env  # noqa: F401  (shared fixture)
from tests.test_effective_decision_pr_flow import (
    ClosureScript,
    SimulatedCrash,
    _repair_to,
    make_flow,
    ordinary_response,
)

ADDRESSED = ordinary_response("PRRT_accepted", status="ADDRESSED", evidence="tests/test_state.py asserts the invariant")


def _repaired_flow(flow_env: Env, monkeypatch: pytest.MonkeyPatch, pr: int, origin: str = "cloud", **kwargs):
    """A published strong finding at H0 followed by a bounded repair H2 with no review of H2 yet."""
    flow = make_flow(flow_env, monkeypatch, pr, origin, **kwargs)
    flow.accept_finding()
    flow.model_responses = [ordinary_response(flow.thread_id)]
    flow.run()  # the upheld finding is handed to its originating route
    flow.h2 = _repair_to(flow, "repair-head")  # type: ignore[attr-defined]
    flow.saved_status = None
    flow.model_responses = [ADDRESSED]
    return flow


@pytest.mark.parametrize("origin", ["cloud", "local"])
@pytest.mark.parametrize("pr", [5442, 31877, 4090123])  # generated identities, not a hard-coded Outliner number
def test_repaired_head_uses_exactly_one_ordinary_invocation_and_reaches_the_merge_endpoint(flow_env: Env, monkeypatch: pytest.MonkeyPatch, origin: str, pr: int) -> None:
    flow = _repaired_flow(flow_env, monkeypatch, pr, origin, resolve_on_approve=True)
    calls_before = flow.model_calls
    script = ClosureScript(status="FIXED")

    actions = flow.run(closure=script)

    assert flow.model_calls == calls_before + 1, actions  # one ordinary invocation after repair reentry, none for closure or renewed strong
    assert len(script.calls) == 1 and script.closure_only_calls == []
    supplied = script.calls[0]
    assert supplied.head_sha == flow.h2 and supplied.attempt_sequence > 0 and supplied.repository == REPO and supplied.pr_number == pr
    assert [item.finding_id for item in supplied.findings] == ["finding-a"]  # the durable bundle, regardless of the thread view
    assert "cumulative repair" in supplied.diff_evidence

    snapshot = flow_env.cycle.snapshot(pr)
    assert snapshot.open_findings == () and snapshot.accepted_closure is not None and snapshot.accepted_closure.head_sha == flow.h2
    assert snapshot.accepted_closure.publication_status == "ACKNOWLEDGED"  # the exact closure publication was confirmed

    attempts = AdversarialValidationAttemptRepository(REPO)
    source = OrdinaryClosureEvidenceRepository(REPO).inspect(snapshot.accepted_closure.source_identity).record
    assert source is not None and source.state is EvidenceState.ACCEPTED
    assert source.attempt_sequence == attempts.latest_sequence(pr, flow.h2)  # the retained source is the one ordinary attempt

    closure_posts = [review for review in flow.reviews if "Ordinary closure evidence" in review["body"]]
    assert len(closure_posts) == 1
    assert source.attempt_id in closure_posts[0]["body"] and "without an additional model execution" in closure_posts[0]["body"]
    assert source.reviewer_provenance in closure_posts[0]["body"]
    assert any("no additional model execution" in action for action in actions)
    assert flow.merge.call_count == 1, actions  # every independent gate passed: the actual merge path is reached


def test_finding_absent_from_the_claimed_thread_view_is_still_supplied(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = _repaired_flow(flow_env, monkeypatch, 7301)
    flow.root_body = ""  # the accepted finding's thread is not visible to the reviewer-session view
    script = ClosureScript(status="FIXED")

    flow.run(closure=script)

    assert [item.finding_id for item in script.calls[0].findings] == ["finding-a"]
    assert flow_env.cycle.snapshot(7301).accepted_closure is not None


def test_legacy_ordinary_pass_without_assessment_triggers_one_combined_review(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = _repaired_flow(flow_env, monkeypatch, 7302)
    flow.saved_status = "PASS"  # a genuine old ordinary PASS at H2 with no retained assessment
    calls_before = flow.model_calls
    script = ClosureScript(status="FIXED")

    actions = flow.run(closure=script)

    assert flow.model_calls == calls_before + 1, actions  # one combined review: not two, and not zero followed by fabricated closure
    assert len(script.calls) == 1 and script.closure_only_calls == []
    assert flow_env.cycle.snapshot(7302).accepted_closure is not None


def test_missing_assessment_never_manufactures_a_closure_only_call(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = _repaired_flow(flow_env, monkeypatch, 7303)
    calls_before = flow.model_calls
    script = ClosureScript(omit_assessment=True)

    actions = flow.run(closure=script)

    assert flow.model_calls == calls_before + 1 and script.closure_only_calls == []
    snapshot = flow_env.cycle.snapshot(7303)
    assert snapshot.accepted_closure is None and [item.finding_id for item in snapshot.open_findings] == ["finding-a"]
    assert any("closure evidence" in action.lower() and "unavailable" in action.lower() for action in actions)  # reported, not hidden as success
    assert flow.merge.call_count == 0


@pytest.mark.parametrize("scope", ["EXPANDED", "UNKNOWN"])
def test_unbounded_scope_requires_a_renewed_strong_audit_and_never_authorizes_merge(flow_env: Env, monkeypatch: pytest.MonkeyPatch, scope: str) -> None:
    flow = _repaired_flow(flow_env, monkeypatch, 7304 if scope == "EXPANDED" else 7305, resolve_on_approve=True)
    script = ClosureScript(status="FIXED", scope=scope)

    flow.run(closure=script)

    assert "cumulative repair" in script.calls[0].diff_evidence  # full cumulative H0-to-H2 context was supplied
    snapshot = flow_env.cycle.snapshot(flow.pr)
    assert snapshot.requires_new_strong_round is True and snapshot.completion is None
    assert flow.merge.call_count == 0


def test_omitted_or_open_disposition_never_authorizes_closure(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = _repaired_flow(flow_env, monkeypatch, 7306)
    calls_before = flow.model_calls
    script = ClosureScript(status="STILL_VALID")

    flow.run(closure=script)

    assert flow.model_calls == calls_before + 1 and script.closure_only_calls == []
    snapshot = flow_env.cycle.snapshot(7306)
    assert snapshot.accepted_closure is None and [item.finding_id for item in snapshot.open_findings] == ["finding-a"]
    assert flow.merge.call_count == 0


def test_interruption_between_retention_and_application_resumes_without_a_model_call(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = _repaired_flow(flow_env, monkeypatch, 7307, resolve_on_approve=True)
    real_apply = OrdinaryClosureEvidence.apply

    def crash(self, *_args, **_kwargs):
        raise SimulatedCrash()

    monkeypatch.setattr(OrdinaryClosureEvidence, "apply", crash)
    with pytest.raises(SimulatedCrash):
        flow.run(closure=ClosureScript(status="FIXED"))
    monkeypatch.setattr(OrdinaryClosureEvidence, "apply", real_apply)
    calls_after_crash = flow.model_calls
    assert flow_env.cycle.snapshot(7307).accepted_closure is None  # retained, not yet applied

    restarted = ClosureScript(status="FIXED")
    actions = flow.run(closure=restarted)  # a reconstructed controller, same head, no commit, no store reset

    assert flow.model_calls == calls_after_crash and restarted.calls == [], actions
    assert flow_env.cycle.snapshot(7307).accepted_closure is not None and flow_env.cycle.snapshot(7307).open_findings == ()


def test_a_newer_ordinary_attempt_prevents_the_older_result_from_closing_findings(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = _repaired_flow(flow_env, monkeypatch, 7308)
    script = ClosureScript(status="FIXED")
    script.on_review = lambda: AdversarialValidationAttemptRepository(REPO).start(7308, flow.h2)  # another participant registers a newer attempt first

    flow.run(closure=script)

    snapshot = flow_env.cycle.snapshot(7308)
    assert snapshot.accepted_closure is None and [item.finding_id for item in snapshot.open_findings] == ["finding-a"]
    assert flow.merge.call_count == 0


def test_nonpassing_independent_gate_blocks_merge_after_valid_bounded_closure(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from auto_coder.util.github_action import GitHubActionsStatusResult

    flow = _repaired_flow(flow_env, monkeypatch, 7309, resolve_on_approve=True)
    flow.post_ci = GitHubActionsStatusResult(success=False, in_progress=True, ids=[2])

    actions = flow.run(closure=ClosureScript(status="FIXED"))

    assert flow_env.cycle.snapshot(7309).accepted_closure is not None
    assert flow.merge.call_count == 0 and any("post-validation CI" in action for action in actions)  # the remaining gate is named, not hidden


def test_unresolved_thread_blocks_merge_after_valid_bounded_closure(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = _repaired_flow(flow_env, monkeypatch, 7310, resolve_on_approve=False)  # the accepted thread is never confirmed resolved

    actions = flow.run(closure=ClosureScript(status="FIXED"))

    assert flow_env.cycle.snapshot(7310).accepted_closure is not None
    assert flow.merge.call_count == 0 and any("unresolved review threads remain" in action for action in actions)


def test_authority_state_is_the_owning_cycle_not_a_helper_flag(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The positive scenario above is observed through a freshly reconstructed owning store."""
    flow = _repaired_flow(flow_env, monkeypatch, 7311, resolve_on_approve=True)
    flow.run(closure=ClosureScript(status="FIXED"))

    reconstructed = PrReviewCycleRepository(REPO).snapshot(7311)
    assert reconstructed.completion is not None and reconstructed.completion.basis == "ORDINARY_CLOSURE"
    assert reconstructed.accepted_closure is not None and reconstructed.accepted_closure.source_identity
