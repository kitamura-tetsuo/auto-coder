"""Saved review replay must reach current-target admission through the public PR entry."""

import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from auto_coder.adversarial_validation_attempts import AdversarialValidationAttemptRepository
from auto_coder.canonical_pr_blocker_ledger import CanonicalPRBlockerLedger
from auto_coder.effective_decision_application import EffectiveDecisionStore, RetainedDecision
from auto_coder.execution_trace import Outcome
from auto_coder.pr_processor import ClaimedReviewThreadGateState
from auto_coder.pr_review_resumption import SavedReviewResumptionService
from auto_coder.util.gh_cache import ReviewThread, ReviewThreadComment
from tests.test_accepted_finding_bridge import REPO, Env, env  # noqa: F401
from tests.test_effective_decision_pr_flow import Flow, flow_env, make_flow, ordinary_response  # noqa: F401

FINDING = "### Auto-Coder adversarial finding\n\n**Violated requirement**\n\n`#2401/REQ-001`: preserve state"


def legacy_flow(flow_env: Env, monkeypatch: pytest.MonkeyPatch, verdict: str, *, actionable: bool = False) -> Flow:
    flow = make_flow(flow_env, monkeypatch, 2759, "cloud", saved_status=verdict, auto_status=True)
    flow.extra_threads = (
        ReviewThread(id="resolved", is_resolved=not actionable, comments=[ReviewThreadComment(database_id=1, author_login="auto-coder-reviewer[bot]", body=FINDING)]),
        ReviewThread(id="foreign", is_resolved=False, comments=[ReviewThreadComment(database_id=2, author_login="someone-else", body="Non-actionable discussion")]),
    )
    # A pre-upgrade, published same-head result has no typed correction to resume.
    attempt = AdversarialValidationAttemptRepository(REPO).start(flow.pr, flow.head)
    repository = AdversarialValidationAttemptRepository(REPO)
    repository.finish(attempt.attempt_id, verdict)
    repository.mark_published(attempt.attempt_id)
    EffectiveDecisionStore(REPO).save(RetainedDecision(repository=REPO, pr_number=flow.pr, head_sha=flow.head, base_sha=flow.env.base, status=verdict, publication="CONFIRMED", attempt_id=attempt.attempt_id, attempt_sequence=attempt.sequence))
    CanonicalPRBlockerLedger().initialize_namespace("https://api.github.com", REPO, flow.pr)
    flow.env = flow.env.reconstructed()
    assert flow.retained().ordinary_correction is None
    flow.model_responses = ['{"result":"BLOCKED","summary":"Independent reviewer could not verify the requirement","requirement_coverage":[{"requirement_id":"#2401/REQ-001","status":"UNVERIFIED","evidence":"The required external oracle is unavailable"}],"findings":[],"test_oracle_gaps":[]}']
    return flow


@pytest.mark.parametrize("verdict", ["NEEDS_FIX", "NEEDS_TESTS"])
@pytest.mark.parametrize("fresh_verdict", ["PASS", "BLOCKED"])
def test_ordinary_reassessment_admits_one_current_target_review_after_reconstruction(flow_env: Env, monkeypatch: pytest.MonkeyPatch, verdict: str, fresh_verdict: str) -> None:
    flow = legacy_flow(flow_env, monkeypatch, verdict)
    if fresh_verdict == "PASS":
        payload = json.loads(ordinary_response(""))
        payload["thread_dispositions"] = []
        flow.model_responses = [json.dumps(payload)]
    replays = []
    original = SavedReviewResumptionService.replay_nonpass

    def replay(service, request, **kwargs):
        result = original(service, request, **kwargs)
        replays.append(result.requires_reassessment)
        assert flow.merge.call_count == 0 and flow.handoffs == 0 and flow.model_calls == 0
        return result

    monkeypatch.setattr(SavedReviewResumptionService, "replay_nonpass", replay)
    with patch("auto_coder.pr_processor._record_pr_stage") as stages, patch("auto_coder.pr_processor.record_reused") as reused:
        actions = flow.run(public_origin=True, thread_state=ClaimedReviewThreadGateState())
    assert replays == [True]
    assert flow.model_calls == 1 and flow.handoffs == 0, actions
    assert AdversarialValidationAttemptRepository(REPO).latest_sequence(flow.pr, flow.head) == 2
    assert len(flow.reviews) == 1 and flow._published_status() == fresh_verdict
    assert flow.reviews[0]["event"] == ("APPROVE" if fresh_verdict == "PASS" else "COMMENT")
    if fresh_verdict == "BLOCKED":
        assert flow.merge.call_count == 0
    flow.client.resolve_review_thread.assert_not_called()
    reused.assert_not_called()
    requests = [call for call in stages.call_args_list if call.args[4].get("effect") == "saved-review-reassessment"]
    assert len(requests) == 1 and requests[0].args[3] == Outcome.DEFERRED
    assert requests[0].args[4]["examined_head"] == flow.head
    assert not any(call.args[1] == "pr.repair-delegation" for call in stages.call_args_list)
    flow.env = flow.env.reconstructed()
    again = flow.run(public_origin=True, thread_state=ClaimedReviewThreadGateState())
    assert flow.model_calls == 1 and flow.handoffs == 0 and len(flow.reviews) == 1, again
    assert len(replays) == 1
    if fresh_verdict == "BLOCKED":
        assert flow.merge.call_count == 0


@pytest.mark.parametrize("early_route", [False, True])
def test_actionable_saved_feedback_does_not_request_review(flow_env: Env, monkeypatch: pytest.MonkeyPatch, early_route: bool) -> None:
    flow = legacy_flow(flow_env, monkeypatch, "NEEDS_FIX", actionable=True)
    live = (flow.extra_threads[0],)
    state = ClaimedReviewThreadGateState(unresolved=live, blocking_unresolved=live, has_blocking_unresolved=True) if early_route else ClaimedReviewThreadGateState()
    with patch("auto_coder.pr_processor._send_adversarial_validation_feedback_to_cloud_task", return_value=["Delivered actionable correction"]) as send:
        actions = flow.run(public_origin=True, thread_state=state)
    send.assert_called_once()
    assert send.call_args.args[5] == (FINDING,)
    assert flow.model_calls == 0 and flow.reviews == [] and flow.merge.call_count == 0, actions
    flow.client.resolve_review_thread.assert_not_called()


def test_early_route_reassessment_still_admits_review(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    flow = legacy_flow(flow_env, monkeypatch, "NEEDS_TESTS")
    # The gate snapshot can precede the replay's fresh observation of resolution.
    live = (replace(flow.extra_threads[0], is_resolved=False),)
    state = ClaimedReviewThreadGateState(unresolved=live, blocking_unresolved=live, has_blocking_unresolved=True)
    actions = flow.run(public_origin=True, thread_state=state)
    assert flow.model_calls == 1 and flow.handoffs == 0 and len(flow.reviews) == 1, actions
    assert flow.merge.call_count == 0
    flow.client.resolve_review_thread.assert_not_called()


def test_reassessment_does_not_override_admission_refusal(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from auto_coder.adversarial_validation_scheduler import AdversarialValidationScheduler
    from auto_coder.pr_processor import ClaimedReviewThreadGateState, process_pull_request

    flow = legacy_flow(flow_env, monkeypatch, "NEEDS_FIX")
    scheduler = AdversarialValidationScheduler()

    def public_with_scheduler(*args, **kwargs):
        return process_pull_request(*args, adversarial_validation_scheduler=scheduler, **kwargs)

    with scheduler.admit(REPO, flow.pr), patch("tests.test_effective_decision_pr_flow.process_pull_request", side_effect=public_with_scheduler):
        actions = flow.run(public_origin=True, thread_state=ClaimedReviewThreadGateState())
    assert flow.model_calls == 0 and flow.handoffs == 0 and flow.reviews == []
    assert AdversarialValidationAttemptRepository(REPO).latest_sequence(flow.pr, flow.head) == 1
    assert flow.merge.call_count == 0 and flow.retained().status == "NEEDS_FIX"
    assert any("Skipped duplicate local adversarial-validation trigger" in action for action in actions)
    flow.client.resolve_review_thread.assert_not_called()
