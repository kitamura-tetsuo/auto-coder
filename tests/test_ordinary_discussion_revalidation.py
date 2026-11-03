"""Issue2678: resolved stale discussion must survive ordinary-only resumption."""

import json
from dataclasses import replace

import pytest

from auto_coder.canonical_pr_blocker_ledger import BlockerDisposition
from auto_coder.effective_decision_application import DecisionRetentionError, EffectiveDecisionStore, RetainedDecision
from auto_coder.review_feedback_marker import REVIEW_ADDRESSED_MARKER
from auto_coder.util.gh_cache import ReviewThreadComment
from tests.test_accepted_finding_bridge import REPO, env  # noqa: F401
from tests.test_adversarial_validator import _demonstrated_finding_with_anchor
from tests.test_effective_decision_pr_flow import _repair_to, flow_env, make_flow, ordinary_response  # noqa: F401


@pytest.mark.parametrize("next_status", ["STILL_VALID", "ADDRESSED"])
@pytest.mark.parametrize("changed_head", [False, True])
def test_ordinary_only_coordinator_reacquires_resolved_changed_discussion(flow_env, monkeypatch, next_status, changed_head):
    monkeypatch.setattr("auto_coder.pr_processor._two_tier_gate_inputs", lambda *args, **kwargs: None)
    flow = make_flow(flow_env, monkeypatch, 7469, "cloud", saved_status=None, auto_status=True)
    flow.production_thread_collection = True
    flow.resolve_on_approve = True
    raw = json.loads(ordinary_response("unused", result="NEEDS_FIX"))
    raw["thread_dispositions"] = []
    raw["requirement_coverage"][0]["status"] = "VIOLATED"
    finding = _demonstrated_finding_with_anchor(40)
    finding["requirement_id"] = "#2401/REQ-001"
    raw["findings"] = [finding]
    flow.model_responses = [json.dumps(raw)]
    first = flow.run()
    assert flow.model_calls == 1, first
    roots = [root for group in flow.router.review_roots.values() for root in group]
    assert len(roots) == 1, first
    root = roots[0]
    flow.root_id = root["id"]
    flow.root_body = root["body"]
    flow.thread_id = f"PRRT_new_{flow.root_id}"
    _repair_to(flow, "ordinary-repair-before-discussion-race")
    flow.thread_replies.append(ReviewThreadComment(database_id=flow.root_id + 1, author_id=702, author_login="implementation-agent[bot]", body=f"Bounded repair implemented.\n{REVIEW_ADDRESSED_MARKER}", in_reply_to_id=flow.root_id))
    counterexample = "A later same-scope transition still discards state."

    def discussion_changes_after_input():
        flow.thread_replies.append(ReviewThreadComment(database_id=flow.root_id + 2, author_id=703, author_login="reviewer[bot]", body=counterexample, in_reply_to_id=flow.root_id))
        flow.thread_resolved = True  # Authoritative external resolution is not proof of freshness.

    flow.model_responses = [ordinary_response(flow.thread_id, status="ADDRESSED", evidence="tests/test_state.py independently asserts the corrected invariant")]
    stale = flow.run(on_review=discussion_changes_after_input)
    assert counterexample not in flow.validation_sections[-1]
    assert any("discussion-freshness" in action for action in stale), stale
    assert flow.merge.call_count == 0
    snapshot = flow.env.ledger.get_snapshot("https://api.github.com", REPO, flow.pr, require_retained_state=True)
    assert all(blocker.disposition is BlockerDisposition.OPEN for blocker in snapshot.blockers)

    # Reconstruct production stores; no Strong policy/closure extension exists.
    flow.env = flow.env.reconstructed()
    retained_before = EffectiveDecisionStore(REPO).load(flow.pr)
    assert retained_before is not None
    assert retained_before.ordinary_correction is None
    assert retained_before.discussion_revalidation_thread_ids == [flow.thread_id]
    complete_threads = flow.threads
    with monkeypatch.context() as unavailable:
        unavailable.setattr(flow, "threads", lambda: tuple(replace(thread, comments_truncated=True) for thread in complete_threads()))
        calls = flow.model_calls
        refused = flow.run(public_origin=True)
        assert any("truncated" in action for action in refused), refused
        assert flow.model_calls == calls
        assert flow.merge.call_count == 0
        assert EffectiveDecisionStore(REPO).load(flow.pr).discussion_revalidation_thread_ids == [flow.thread_id]
    if changed_head:
        _repair_to(flow, "later-ordinary-discussion-repair")
    before = flow.model_calls
    flow.model_responses = [ordinary_response(flow.thread_id, status=next_status, evidence="tests/test_state.py independently asserts the corrected invariant")]
    current = flow.run()
    assert flow.model_calls == before + 1, current
    assert counterexample in flow.validation_sections[-1]
    snapshot = flow.env.ledger.get_snapshot("https://api.github.com", REPO, flow.pr, require_retained_state=True)
    if next_status == "STILL_VALID":
        assert all(blocker.disposition is BlockerDisposition.OPEN for blocker in snapshot.blockers)
        assert flow.merge.call_count == 0
    else:
        assert all(blocker.disposition is BlockerDisposition.VERIFIED_CORRECTION for blocker in snapshot.blockers)
        retained = EffectiveDecisionStore(REPO).load(flow.pr)
        assert retained is not None
        assert retained.discussion_revalidation_thread_ids == []
        calls = flow.model_calls
        flow.run()
        assert flow.model_calls == calls  # A valid owned receipt does not churn.
        merged = flow.merge.call_count
        later = "An additional concrete transition fails after the retained acceptance."
        flow.thread_replies.append(ReviewThreadComment(database_id=flow.root_id + 100, author_id=703, author_login="reviewer[bot]", body=later, in_reply_to_id=flow.root_id))
        flow.model_responses = [ordinary_response(flow.thread_id, status="STILL_VALID", evidence="tests/test_state.py independently reproduces the later transition")]
        actions = flow.run()
        assert flow.model_calls == calls + 1, actions
        assert later in flow.validation_sections[-1]
        assert flow.merge.call_count == merged


@pytest.mark.parametrize("invalid", [None, "PRRT_1", [""], [1], ["PRRT_1", "PRRT_1"], [{}]])
def test_corrupt_discussion_checkpoint_never_becomes_reusable_authority(flow_env, invalid):
    store = EffectiveDecisionStore(REPO)
    store.save(RetainedDecision(pr_number=7469, head_sha="prior", discussion_revalidation_thread_ids=["PRRT_1"]))
    saved = json.loads(store.storage_path.read_text())
    saved["7469"]["discussion_revalidation_thread_ids"] = invalid
    store.storage_path.write_text(json.dumps(saved))
    with pytest.raises(DecisionRetentionError, match="discussion identities"):
        EffectiveDecisionStore(REPO).load(7469)

@pytest.mark.parametrize("next_status", ["STILL_VALID", "ADDRESSED"])
def test_ordinary_closure_binding_survives_unrelated_forced_pass(flow_env, monkeypatch, next_status):
    monkeypatch.setattr("auto_coder.pr_processor._two_tier_gate_inputs", lambda *args, **kwargs: None)
    flow = make_flow(flow_env, monkeypatch, 7470, "cloud", saved_status=None, auto_status=True)
    flow.production_thread_collection = True
    flow.resolve_on_approve = True
    raw = json.loads(ordinary_response("unused", result="NEEDS_FIX"))
    raw["thread_dispositions"] = []
    raw["requirement_coverage"][0]["status"] = "VIOLATED"
    finding = _demonstrated_finding_with_anchor(40)
    finding["requirement_id"] = "#2401/REQ-001"
    raw["findings"] = [finding]
    flow.model_responses = [json.dumps(raw)]
    flow.run()
    root = [root for group in flow.router.review_roots.values() for root in group][0]
    flow.root_id, flow.root_body = root["id"], root["body"]
    flow.thread_id = f"PRRT_new_{flow.root_id}"
    _repair_to(flow, "ordinary-repair")
    flow.thread_replies.append(ReviewThreadComment(database_id=flow.root_id + 1, author_id=702, author_login="implementation-agent[bot]", body=f"Repair implemented.\n{REVIEW_ADDRESSED_MARKER}", in_reply_to_id=flow.root_id))
    flow.model_responses = [ordinary_response(flow.thread_id, status="ADDRESSED", evidence="tests/test_state.py independently asserts the corrected invariant")]
    flow.run()
    assert flow.thread_resolved
    raw_pass = json.loads(ordinary_response(flow.thread_id))
    raw_pass["thread_dispositions"] = []
    flow.model_responses = [json.dumps(raw_pass)]
    calls = flow.model_calls
    flow.run(force=True)
    assert flow.model_calls == calls + 1
    assert flow.thread_id not in flow.validation_sections[-1]
    changed = "A later discussion identifies a concrete remaining transition."
    flow.thread_replies.append(ReviewThreadComment(database_id=flow.root_id + 100, author_id=703, author_login="reviewer[bot]", body=changed, in_reply_to_id=flow.root_id))
    flow.env = flow.env.reconstructed()
    from auto_coder.pr_processor import _OrdinaryDiscussionChanged, _assert_ordinary_discussion_current

    retained = EffectiveDecisionStore(REPO).load(flow.pr)
    assert retained is not None and retained.prior_discussion_bindings
    with pytest.raises(_OrdinaryDiscussionChanged):
        _assert_ordinary_discussion_current(flow.client, REPO, flow.pr, retained.prior_discussion_bindings[0].restore())
    calls = flow.model_calls
    flow.model_responses = [ordinary_response(flow.thread_id, status=next_status, evidence="tests/test_state.py independently checks the transition")]
    actions = flow.run()
    assert flow.model_calls == calls + 1, actions
    assert changed in flow.validation_sections[-1]
    snapshot = flow.env.ledger.get_snapshot("https://api.github.com", REPO, flow.pr, require_retained_state=True)
    expected = BlockerDisposition.OPEN if next_status == "STILL_VALID" else BlockerDisposition.VERIFIED_CORRECTION
    assert all(blocker.disposition is expected for blocker in snapshot.blockers)
