"""Production-path regressions for one combined ordinary review per repaired head (issue #2407).

Every scenario starts at ``_handle_pr_merge`` with the real strong acceptance
store, the real ``run_adversarial_validation`` prompt/parse path, the real
attempt registry, retained-evidence store, review-cycle owner and the real
``GitHubAppReviewer`` over a recording HTTP transport. Only the model, GitHub
and merge endpoints are controlled. Every scenario counts actual reviewer
invocations, not calls to any named closure helper.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from auto_coder.adversarial_validation_attempts import AdversarialValidationAttemptRepository
from auto_coder.automation_config import PRProcessingOutcome
from auto_coder.canonical_pr_blocker_ledger import BlockerAdmissionPayload, BlockerAlias, BlockerDisposition, CorrectionScope, QualifiedRequirement
from auto_coder.ordinary_closure_evidence import EvidenceState, OrdinaryClosureEvidence, OrdinaryClosureEvidenceRepository
from auto_coder.pr_processor import _record_pr_stage
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


def test_renewed_pass_at_repair_head_closes_retained_findings_without_another_repair(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    from auto_coder.github_app_reviewer import ReviewerAppConfig
    from auto_coder.pr_processor import _consume_pending_two_tier_publication
    from auto_coder.pr_review_cycle import RoundProvenance
    from tests.test_accepted_finding_bridge import CONTRACT, POLICY
    from tests.test_effective_decision_pr_flow import reviewer_for

    flow = _repaired_flow(flow_env, monkeypatch, 7352, resolve_on_approve=True)
    provenance = RoundProvenance(flow.h2, flow_env.base)
    flow_env.cycle.record_ordinary_pass(flow.pr, provenance, CONTRACT)
    claim = flow_env.cycle.claim_strong_audit(flow.pr, provenance, CONTRACT, POLICY)
    renewed = flow_env.cycle.record_strong_result(flow.pr, claim.claim_id, "PASS", "strong/model")
    reviewer = reviewer_for(flow_env.tmp, monkeypatch, flow.router)
    config = ReviewerAppConfig("4765828", "client", flow_env.tmp / "reviewer.pem")
    with patch("auto_coder.pr_processor.load_reviewer_app_config", return_value=config), patch("auto_coder.pr_processor.GitHubAppReviewer", return_value=reviewer):
        published, reason = _consume_pending_two_tier_publication(REPO, flow.pr, replace(flow.gate_inputs, head_sha=flow.h2))
    assert published, reason
    handoffs_before = flow.handoffs
    script = ClosureScript(status="FIXED")

    actions = flow.run(closure=script)

    assert len(script.calls) == 1, actions
    assert script.calls[0].round_id == renewed.round_id
    assert flow.handoffs == handoffs_before
    assert flow_env.cycle.snapshot(flow.pr).open_findings == ()
    assert flow.merge.call_count == 1, actions


def test_base_advanced_before_review_renews_audit_without_erasing_findings(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import patch

    from auto_coder.pr_processor import _record_pr_stage

    flow = _repaired_flow(flow_env, monkeypatch, 7351)
    flow.current_base = "d" * 40
    initial_pr_data = flow.pr_data
    monkeypatch.setattr(flow, "pr_data", lambda: {**initial_pr_data(), "base": {"ref": "main", "sha": flow.current_base}})
    script = ClosureScript(status="FIXED")

    with patch("auto_coder.pr_processor._record_pr_stage", wraps=_record_pr_stage) as stages:
        actions = flow.run(closure=script)

    snapshot = flow_env.cycle.snapshot(flow.pr)
    assert snapshot.ordinary_pass_base_sha == flow.current_base, actions
    assert snapshot.attempt_error_reason == "strong reviewer is unavailable", actions
    assert [item.finding_id for item in snapshot.open_findings] == ["finding-a"]
    assert snapshot.accepted_closure is None
    assert script.calls == []
    assert flow.merge.call_count == 0
    assert any("Renewed stale strong audit" in action for action in actions)
    audit_events = [call.args for call in stages.call_args_list if call.args[1] == "pr.strong-audit"]
    assert len(audit_events) == 1
    assert audit_events[0][3].value == "deferred"
    assert audit_events[0][4]["head"] == flow.h2
    assert audit_events[0][4]["base"] == flow.current_base
    assert audit_events[0][4]["renewed_strong_required"] is True


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
    closure_replies = [call.args for call in flow.client.reply_to_review_thread.call_args_list if "auto-coder-two-tier-closure:v1:" in call.args[3]]
    assert len(closure_replies) == 1
    assert closure_replies[0][:3] == (REPO, pr, flow.root_id)
    assert f"**FIXED** against `{flow.h2}`" in closure_replies[0][3]
    flow.client.resolve_review_thread.assert_any_call(flow.thread_id)
    assert flow.threads()[0].is_resolved is True  # GitHub retains the exact root after resolution.

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


@pytest.mark.parametrize("matching_concerns", [True, False])
def test_canonical_concern_ids_reach_ordinary_review_and_control_merge(flow_env: Env, monkeypatch: pytest.MonkeyPatch, matching_concerns: bool) -> None:
    """A saved PASS's native root must receive the scope its resolver enforces."""
    flow = make_flow(flow_env, monkeypatch, 7437, "cloud", resolve_on_approve=False)
    flow.root_body = "### Auto-Coder adversarial finding\nThe state invariant is violated."
    snapshot = flow_env.ledger.initialize_namespace("https://api.github.com", REPO, flow.pr)
    _, snapshot = flow_env.ledger.admit_blocker(
        "https://api.github.com",
        REPO,
        flow.pr,
        operation_id="accept-native-finding",
        expected_ledger_revision=snapshot.ledger_revision,
        review_observation_identity="state-invariant-finding",
        payload=BlockerAdmissionPayload(
            category="IMPLEMENTATION",
            qualified_requirements=(QualifiedRequirement(2401, "REQ-001"),),
            authoritative_boundary="src/state.py",
            incorrect_behavior_or_missing_invariant="The state invariant is violated.",
            required_correction_outcome="Preserve state across the transition.",
            evidence_needed="A regression exercises the state transition.",
            accepted_scope=CorrectionScope(description="Correct the original state transition.", concern_ids=("native-state-transition-correction",)),
            aliases=(BlockerAlias(alias_type="github_root_comment", alias_value=str(flow.root_id)),),
            reviewed_head_sha=flow.head,
            observation_identity="state-invariant-finding",
        ),
    )
    snapshot = flow_env.ledger.get_snapshot("https://api.github.com", REPO, flow.pr)
    owners = snapshot.get_blockers_for_alias("github_root_comment", str(flow.root_id))
    assert len(owners) == 1
    blocker = owners[0]
    concerns = blocker.concern_ids or blocker.accepted_scope.concern_ids
    assert concerns
    response = json.loads(ordinary_response(flow.thread_id, status="ADDRESSED", evidence="tests/test_state.py exercises the repaired state invariant"))
    response["thread_dispositions"][0]["concern_ids"] = list(concerns) if matching_concerns else ["invented-correction-name"]
    flow.model_responses = [json.dumps(response)]
    # Resolve only when the real closure executor requests the GitHub mutation.
    original_set_resolved = flow._set_thread_resolved
    monkeypatch.setattr(flow, "_set_thread_resolved", lambda thread, resolved: original_set_resolved(thread, True))
    calls_before = flow.model_calls

    with patch("auto_coder.pr_processor._record_pr_stage", wraps=_record_pr_stage) as stages:
        actions = flow.run()

    section = flow.validation_sections[-1]
    assert f"Canonical blocker identity: {blocker.blocker_id}" in section
    assert f"Owned concrete concern IDs: {', '.join(concerns)}" in section
    assert f"Accepted original correction scope: {blocker.accepted_scope.description}" in section
    assert f"Authoritative production boundary: {blocker.authoritative_boundary}" in section
    assert flow.model_calls == calls_before + 1
    if matching_concerns:
        assert flow.thread_resolved is True, actions
        assert flow.merge.call_count == 1, actions
        assert flow.status.error is None
        # Reprocessing the same head consumes completion without another model call.
        flow.auto_status = True
        flow.run()
        assert flow.model_calls == calls_before + 1
        assert flow.status.outcome is PRProcessingOutcome.SUCCESS
    else:
        flow.client.resolve_review_thread.assert_not_called()
        flow.merge.assert_not_called()
        assert flow.status.outcome is PRProcessingOutcome.FAILED, actions
        assert "Review-thread closure remains unfinished" in flow.status.error
        assert any("Partial correction:" in action for action in actions)
        assert not any("Adversarial validation passed" in action for action in actions)
        closure_stages = [call for call in stages.call_args_list if call.args[1] == "pr.review-thread-closure"]
        assert len(closure_stages) == 1
        facts = closure_stages[0].args[4]
        assert facts["confirmed_count"] == 0 and facts["unfinished_count"] == 1
        assert facts["unfinished"][0]["thread_id"] == flow.thread_id
        assert facts["unfinished"][0]["phase"] == "independent-decision"
        assert facts["unfinished"][0]["effect_state"] == "NOT_ATTEMPTED"
        assert not any(call.args[1] == "pr.merge-completion" for call in stages.call_args_list)
    assert flow.provider.followups == []


def test_unavailable_canonical_closure_scope_does_not_start_a_reviewer(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = make_flow(flow_env, monkeypatch, 7438, "cloud")
    flow.root_body = "### Auto-Coder adversarial finding\nThe state transition loses data."

    def unavailable(*_args: object) -> None:
        raise OSError("ledger unavailable")

    monkeypatch.setattr("auto_coder.pr_processor.CanonicalPRBlockerLedger.initialize_namespace", unavailable)

    actions = flow.run()

    assert flow.model_calls == 0
    assert flow.merge.call_count == 0
    assert flow.provider.followups == []
    assert any("canonical thread-closure scope is unavailable: ledger unavailable" in action for action in actions)


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
@pytest.mark.parametrize("verdict", ["PASS", "INCONCLUSIVE"])
def test_unbounded_scope_requires_a_renewed_strong_audit_and_never_authorizes_merge(flow_env: Env, monkeypatch: pytest.MonkeyPatch, scope: str, verdict: str) -> None:
    flow = _repaired_flow(flow_env, monkeypatch, 7304 if scope == "EXPANDED" else 7305, resolve_on_approve=True)
    script = ClosureScript(status="FIXED", scope=scope, verdict=verdict)

    with patch("auto_coder.pr_processor._record_pr_stage", wraps=_record_pr_stage) as stages:
        flow.run(closure=script)

    assert "cumulative repair" in script.calls[0].diff_evidence  # full cumulative H0-to-H2 context was supplied
    snapshot = flow_env.cycle.snapshot(flow.pr)
    assert snapshot.requires_new_strong_round is True and snapshot.completion is None
    assert snapshot.open_findings == () and len(snapshot.closures) == 1
    record = OrdinaryClosureEvidenceRepository(REPO).inspect(snapshot.closures[0].source_identity).record
    assert record is not None and record.state is EvidenceState.ACCEPTED and record.verdict == verdict
    events = [call.args for call in stages.call_args_list if call.args[1] == "pr.ordinary-closure" and call.args[4].get("effect") == "effective-decision-closure"]
    assert len(events) == 1
    assert events[0][4]["evidence_status"] == "ACCEPTED"
    assert events[0][4]["renewed_strong_required"] is True
    assert events[0][4]["additional_model_execution"] is False
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
    assert flow.merge.call_count == 1, actions  # the retained result's ordinary publication and thread effects completed


def test_a_newer_ordinary_attempt_prevents_the_older_result_from_closing_findings(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = _repaired_flow(flow_env, monkeypatch, 7308)
    script = ClosureScript(status="FIXED")
    script.on_review = lambda: AdversarialValidationAttemptRepository(REPO).start(7308, flow.h2)  # another participant registers a newer attempt first

    flow.run(closure=script)

    snapshot = flow_env.cycle.snapshot(7308)
    assert snapshot.accepted_closure is None and [item.finding_id for item in snapshot.open_findings] == ["finding-a"]
    assert flow.merge.call_count == 0


def test_retained_nonbounded_convergence_recovers_a_closed_ledger_and_open_cycle_without_another_review(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = _repaired_flow(flow_env, monkeypatch, 7318)
    real_apply = OrdinaryClosureEvidence.apply

    def crash(self, *_args, **_kwargs):
        raise SimulatedCrash()

    monkeypatch.setattr(OrdinaryClosureEvidence, "apply", crash)
    with pytest.raises(SimulatedCrash):
        flow.run(closure=ClosureScript(status="FIXED", scope="EXPANDED", verdict="INCONCLUSIVE"))
    monkeypatch.setattr(OrdinaryClosureEvidence, "apply", real_apply)
    snapshot = flow_env.ledger.get_snapshot("https://api.github.com", REPO, flow.pr)
    assert len(snapshot.blockers) == 1
    blocker = snapshot.blockers[0]
    flow_env.ledger.record_transition(
        "https://api.github.com",
        REPO,
        flow.pr,
        operation_id="historical-ledger-only-closure",
        expected_ledger_revision=snapshot.ledger_revision,
        blocker_id=blocker.blocker_id,
        target_disposition=BlockerDisposition.VERIFIED_CORRECTION,
        evidence="independent current-head correction proof",
        transition_reason="historical ledger-only closure",
        reviewed_head_sha=flow.h2,
    )
    target = flow_env.target(flow.pr, head=flow.h2)
    before = flow_env.bridge().project(target)
    assert [diagnostic.code for diagnostic in before.diagnostics] == ["cross_store_disagreement"]
    calls = flow.model_calls

    restarted = ClosureScript(status="FIXED", scope="EXPANDED", verdict="INCONCLUSIVE")
    flow.run(closure=restarted)

    after = flow_env.reconstructed().bridge().project(target)
    assert after.complete and after.diagnostics == ()
    assert [record.accepted_state for record in after.records] == ["FIXED"]
    cycle = flow_env.cycle.snapshot(flow.pr)
    assert cycle.open_findings == () and cycle.requires_new_strong_round
    assert len(cycle.closures) == 1 and cycle.completion is None
    assert flow.model_calls == calls and flow.merge.call_count == 0
    assert restarted.calls == []


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

    snapshot = flow_env.cycle.snapshot(7310)
    assert snapshot.accepted_closure is not None and snapshot.accepted_closure.publication_status == "PENDING"
    assert snapshot.completion is None
    assert flow.merge.call_count == 0 and any("accepted finding thread resolution remains unfinished" in action for action in actions)
    assert flow.threads()[0].is_resolved is False


def test_authority_state_is_the_owning_cycle_not_a_helper_flag(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The positive scenario above is observed through a freshly reconstructed owning store."""
    flow = _repaired_flow(flow_env, monkeypatch, 7311, resolve_on_approve=True)
    flow.run(closure=ClosureScript(status="FIXED"))

    reconstructed = PrReviewCycleRepository(REPO).snapshot(7311)
    assert reconstructed.completion is not None and reconstructed.completion.basis == "ORDINARY_CLOSURE"
    assert reconstructed.accepted_closure is not None and reconstructed.accepted_closure.source_identity


def test_base_advanced_during_the_review_refuses_the_stale_assessment(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = _repaired_flow(flow_env, monkeypatch, 7312)
    script = ClosureScript(status="FIXED")
    script.on_review = lambda: setattr(flow, "current_base", "d" * 40)  # the PR is retargeted while the reviewer runs; H2 is unchanged

    flow.run(closure=script)

    snapshot = flow_env.cycle.snapshot(7312)
    assert snapshot.accepted_closure is None and [item.finding_id for item in snapshot.open_findings] == ["finding-a"]
    assert not any("Ordinary closure evidence" in review["body"] for review in flow.reviews)  # no dependent publication
    assert flow.merge.call_count == 0
    sources = OrdinaryClosureEvidenceRepository(REPO).pending_for_pr(7312)
    assert sources == ()  # the stale source was rejected, not left resumable


def test_retained_unapplied_source_is_refused_when_the_base_changed_before_restart(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = _repaired_flow(flow_env, monkeypatch, 7313)
    outage = ClosureScript(status="FIXED")
    outage.on_review = lambda: setattr(outage, "observable", False)  # the observation fails when the evidence is applied: retained, unapplied
    flow.run(closure=outage)
    assert len(OrdinaryClosureEvidenceRepository(REPO).pending_for_pr(7313)) == 1

    flow.current_base = "e" * 40  # the base advances before the restart; the head is identical
    restarted = ClosureScript(status="FIXED")
    flow.run(closure=restarted)

    assert OrdinaryClosureEvidenceRepository(REPO).pending_for_pr(7313) == ()
    snapshot = flow_env.cycle.snapshot(7313)
    assert snapshot.accepted_closure is None and [item.finding_id for item in snapshot.open_findings] == ["finding-a"]
    assert flow.merge.call_count == 0


def test_newer_unresolved_attempt_after_certification_defers_publication_and_merge_until_authority_is_current(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    import auto_coder.pr_processor as pr_processor

    flow = _repaired_flow(flow_env, monkeypatch, 7314, resolve_on_approve=True)
    real_consume = pr_processor._consume_pending_two_tier_publication
    monkeypatch.setattr(pr_processor, "_consume_pending_two_tier_publication", lambda *_a, **_k: (False, "publication uncertain: lookup unavailable"))
    flow.run(closure=ClosureScript(status="FIXED"))
    pending = flow_env.cycle.snapshot(7314)
    assert pending.accepted_closure is not None and pending.accepted_closure.publication_status == "PENDING"  # certified; publication unconfirmed
    monkeypatch.setattr(pr_processor, "_consume_pending_two_tier_publication", real_consume)

    newer = AdversarialValidationAttemptRepository(REPO).start(7314, flow.h2)  # another participant registers attempt A+1 at the identical head
    flow.saved_status = "PASS"
    calls_before = flow.model_calls
    actions = flow.run(closure=ClosureScript(status="FIXED"))  # reconstructed processing with a saved PASS

    after = flow_env.cycle.snapshot(7314)
    assert after.accepted_closure is not None and after.accepted_closure.publication_status == "PENDING"  # retained, not acknowledged under A's authority
    assert not any("Ordinary closure evidence" in review["body"] for review in flow.reviews)
    assert flow.merge.call_count == 0 and flow.model_calls == calls_before
    assert any("ordinary-attempt authority" in action for action in actions)

    AdversarialValidationAttemptRepository(REPO).finish(newer.attempt_id, "PASS")  # the newer attempt ends clean
    actions = flow.run(closure=ClosureScript(status="FIXED"))

    assert flow_env.cycle.snapshot(7314).accepted_closure.publication_status == "ACKNOWLEDGED"
    assert len([review for review in flow.reviews if "Ordinary closure evidence" in review["body"]]) == 1
    assert flow.merge.call_count == 1, actions


@pytest.mark.parametrize("origin", ["cloud", "local"])
def test_retained_result_after_a_target_outage_completes_publication_and_threads_without_a_reviewer_or_repair(flow_env: Env, monkeypatch: pytest.MonkeyPatch, origin: str) -> None:
    flow = _repaired_flow(flow_env, monkeypatch, 7315 if origin == "cloud" else 7316, origin, resolve_on_approve=True)
    handoffs_before = flow.handoffs
    outage = ClosureScript(status="FIXED")
    outage.on_review = lambda: setattr(outage, "observable", False)  # the target becomes unreadable after the combined review returns
    flow.auto_status = True  # GitHub holds whatever review this run publishes
    flow.run(closure=outage)
    pending = flow_env.cycle.snapshot(flow.pr)
    assert pending.accepted_closure is None and [item.finding_id for item in pending.open_findings] == ["finding-a"]  # retained, unapplied
    assert [review["event"] for review in flow.reviews][-1] != "APPROVE"  # the effective BLOCKED/CLOSURE_ACCEPTANCE result was published
    calls_before = flow.model_calls
    reviews_before = len(flow.reviews)

    restored = ClosureScript(status="FIXED")
    actions = flow.run(closure=restored)  # normal same-head processing; the target is observable again and the thread is still unresolved

    assert flow.model_calls == calls_before and restored.calls == [] and restored.closure_only_calls == [], actions  # no reviewer
    assert flow.handoffs == handoffs_before, actions  # no further repair request for the now-closed finding
    snapshot = flow_env.cycle.snapshot(flow.pr)
    assert snapshot.open_findings == () and snapshot.accepted_closure is not None and snapshot.accepted_closure.publication_status == "ACKNOWLEDGED"
    new_reviews = flow.reviews[reviews_before:]
    assert [review["event"] for review in new_reviews if "Ordinary closure evidence" not in review["body"]] == ["APPROVE"]  # the ordinary publication completes
    assert len([review for review in flow.reviews if "Ordinary closure evidence" in review["body"]]) == 1  # exactly one closure publication
    assert flow.merge.call_count == 1, actions  # retained thread/publication effects done: the remaining gates pass
