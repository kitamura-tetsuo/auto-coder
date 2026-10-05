"""Strong findings reach correction before another ordinary review is admitted."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from auto_coder.adversarial_validation_attempts import AdversarialValidationAttemptRepository
from auto_coder.automation_config import PRProcessingOutcome
from auto_coder.effective_decision_application import HANDOFF_DISPATCHED
from auto_coder.pr_processor import TwoTierGateInputs
from tests.test_accepted_finding_bridge import CONTRACT, POLICY, REPO, Env, env  # noqa: F401
from tests.test_effective_decision_pr_flow import ClosureScript, flow_env, make_flow, ordinary_response  # noqa: F401


def configure_gate(flow, monkeypatch):
    monkeypatch.setattr(
        "auto_coder.pr_processor._two_tier_gate_inputs",
        lambda _client, _repo, data: TwoTierGateInputs(flow.gate_inputs.gate, CONTRACT, POLICY, data["head"]["sha"], data["base"]["sha"]),
    )


@pytest.mark.parametrize("origin", ["cloud", "local"])
@pytest.mark.parametrize("gap", [True, False])
@pytest.mark.parametrize("saved_status", ["PASS", None, "NEEDS_TESTS"])
@pytest.mark.parametrize("force", [False, True])
def test_strong_findings_are_repaired_before_same_head_validation(flow_env: Env, monkeypatch: pytest.MonkeyPatch, origin, gap, saved_status, force):
    flow = make_flow(flow_env, monkeypatch, 7521, origin, saved_status=saved_status)
    flow.accept_finding(gap=gap)
    configure_gate(flow, monkeypatch)
    attempts = AdversarialValidationAttemptRepository(REPO)
    before = attempts.latest_sequence(flow.pr, flow.head)

    with patch("auto_coder.pr_processor._record_pr_stage") as stages:
        actions = flow.run(force=force)

    assert flow.model_calls == 0, actions
    assert attempts.latest_sequence(flow.pr, flow.head) == before
    assert flow.handoffs == 1
    assert "finding-a" in flow.handoff_text()
    assert flow.reviews == []
    assert flow.merge.call_count == 0
    assert flow.status.outcome is PRProcessingOutcome.DEFERRED
    retained = flow.retained()
    assert retained is not None and retained.handoff == HANDOFF_DISPATCHED
    assert retained.status == ("NEEDS_TESTS" if gap else "NEEDS_FIX")
    assert len(retained.blocker_ids) == 1 and retained.handoff_generation == 1
    assert any(call.args[1] == "pr.repair-delegation" and call.args[4].get("effect") == "strong-findings-before-validation" for call in stages.call_args_list)


@pytest.mark.parametrize("route_available", [True, False])
def test_cloud_reentry_waits_for_correction_without_another_review(flow_env: Env, monkeypatch: pytest.MonkeyPatch, route_available):
    flow = make_flow(flow_env, monkeypatch, 7522, "cloud")
    flow.accept_finding()
    flow.origin_available = route_available
    configure_gate(flow, monkeypatch)

    flow.run()
    actions = flow.run(force=True)

    assert flow.model_calls == 0, actions
    assert flow.handoffs == (1 if route_available else 0)
    assert flow.status.outcome is PRProcessingOutcome.DEFERRED
    assert flow_env.cycle.snapshot(flow.pr).open_findings


def test_completed_same_head_correction_admits_one_combined_review(flow_env: Env, monkeypatch: pytest.MonkeyPatch):
    flow = make_flow(flow_env, monkeypatch, 7523, "cloud", resolve_on_approve=True)
    flow.accept_finding()
    configure_gate(flow, monkeypatch)
    flow.run()
    flow.provider.completed_at = datetime(2026, 10, 5, tzinfo=timezone.utc)
    flow.model_responses = [ordinary_response(flow.thread_id, status="ADDRESSED", evidence="Exact regression evidence")]
    script = ClosureScript()

    actions = flow.run(closure=script)

    assert flow.model_calls == 1, actions
    assert len(script.calls) == 1 and script.closure_only_calls == []
    assert flow.handoffs == 1
    assert flow_env.cycle.snapshot(flow.pr).accepted_closure is not None


def test_failed_handoff_retention_does_not_admit_validation_or_dispatch(flow_env: Env, monkeypatch: pytest.MonkeyPatch):
    from auto_coder.effective_decision_application import DecisionRetentionError

    flow = make_flow(flow_env, monkeypatch, 7524, "cloud")
    flow.accept_finding()
    configure_gate(flow, monkeypatch)
    with patch("auto_coder.pr_processor.EffectiveDecisionStore.retain", side_effect=DecisionRetentionError("storage unavailable")):
        actions = flow.run(force=True)

    assert flow.model_calls == 0, actions
    assert flow.handoffs == 0 and flow.reviews == []
    assert flow.status.outcome is PRProcessingOutcome.DEFERRED
    assert flow_env.cycle.snapshot(flow.pr).open_findings


def test_quota_deadline_is_retained_before_validation(flow_env: Env, monkeypatch: pytest.MonkeyPatch):
    from auto_coder.pr_processor import PRActionList

    flow = make_flow(flow_env, monkeypatch, 7525, "cloud")
    flow.accept_finding()
    configure_gate(flow, monkeypatch)
    deferred = PRActionList(["Deferred adversarial correction feedback: quota exhausted"])
    deferred.quota_deferred = True
    deferred.retry_not_before = 4_000_000_000.0
    with patch("auto_coder.pr_processor._send_adversarial_validation_feedback_to_cloud_task", return_value=deferred):
        actions = flow.run(force=True)

    assert flow.model_calls == 0 and flow.handoffs == 0, actions
    assert flow.status.outcome is PRProcessingOutcome.DEFERRED
    assert flow.status.retry_not_before == 4_000_000_000.0


def test_unpublished_strong_findings_defer_before_review_and_repair(flow_env: Env, monkeypatch: pytest.MonkeyPatch):
    from tests.test_accepted_finding_bridge import accept_strong, finding_json

    flow = make_flow(flow_env, monkeypatch, 7526, "cloud")
    flow.gate_inputs = accept_strong(flow_env, flow.pr, [finding_json("finding-a")])
    configure_gate(flow, monkeypatch)
    with patch("auto_coder.pr_processor._consume_pending_two_tier_publication", return_value=(False, "publication unavailable")) as publication:
        actions = flow.run(force=True)

    assert publication.call_count == 1
    assert flow.model_calls == 0 and flow.handoffs == 0, actions
    assert flow.status.outcome is PRProcessingOutcome.DEFERRED
    assert flow_env.cycle.snapshot(flow.pr).pending_effect
