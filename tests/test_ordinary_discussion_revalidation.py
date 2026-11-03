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
    from auto_coder.pr_processor import _assert_ordinary_discussion_current, _OrdinaryDiscussionChanged

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
    assert all(blocker.disposition is expected for blocker in snapshot.blockers), snapshot.blockers


@pytest.mark.parametrize("both_changed", [False, True])
@pytest.mark.parametrize("next_status", ["STILL_VALID", "ADDRESSED"])
def test_two_accepted_ordinary_sources_survive_selected_invalidation(flow_env, monkeypatch, both_changed, next_status):
    from auto_coder.util.github_action import GitHubActionsStatusResult
    from tests.test_effective_decision_pr_flow import Flow

    monkeypatch.setattr("auto_coder.pr_processor._two_tier_gate_inputs", lambda *args, **kwargs: None)

    def accept_root(flow, label):
        flow.production_thread_collection = True
        flow.post_ci = GitHubActionsStatusResult(success=False, in_progress=True, ids=[2])
        flow.resolve_on_approve = True
        raw = json.loads(ordinary_response("unused", result="NEEDS_FIX"))
        raw["thread_dispositions"] = []
        raw["requirement_coverage"][0]["status"] = "VIOLATED"
        finding = _demonstrated_finding_with_anchor(40)
        finding["requirement_id"] = "#2401/REQ-001"
        finding["label"] = label
        raw["findings"] = [finding]
        flow.model_responses = [json.dumps(raw)]
        flow.run(force=True)
        roots = [root for group in flow.router.review_roots.values() for root in group]
        assert len(roots) == 1
        flow.root_id, flow.root_body = roots[0]["id"], roots[0]["body"]
        flow.thread_id = f"PRRT_new_{flow.root_id}"
        _repair_to(flow, label)
        flow.thread_replies.append(ReviewThreadComment(database_id=flow.root_id + 1, author_id=702, author_login="implementation-agent[bot]", body=f"Repair implemented.\n{REVIEW_ADDRESSED_MARKER}", in_reply_to_id=flow.root_id))
        flow.model_responses = [ordinary_response(flow.thread_id, status="ADDRESSED", evidence="tests/test_state.py independently asserts this corrected invariant")]
        flow.run()
        assert flow.thread_resolved

    first = make_flow(flow_env, monkeypatch, 7471, "cloud", saved_status=None, auto_status=True)
    accept_root(first, "first-ordinary-repair")

    class TwoRootFlow(Flow):
        def threads(self):
            return (first.threads()[-1],) + super().threads()

        def _set_thread_resolved(self, thread, resolved):
            return first._set_thread_resolved(thread, resolved) if thread == first.thread_id else super()._set_thread_resolved(thread, resolved)

        def _reply_to_thread(self, repository, pr_number, root_id, body):
            return first._reply_to_thread(repository, pr_number, root_id, body) if root_id == first.root_id else super()._reply_to_thread(repository, pr_number, root_id, body)

        def _update_thread_comment(self, repository, comment_id, body):
            if any(item.database_id == comment_id for item in first.thread_replies):
                return first._update_thread_comment(repository, comment_id, body)
            return super()._update_thread_comment(repository, comment_id, body)

    second = TwoRootFlow(first.env, monkeypatch, first.pr, origin="cloud", saved_status=None, auto_status=True, head=first.head, root_id=10000)
    second.router.next_root_id = 10000
    accept_root(second, "second-ordinary-repair")
    retained = EffectiveDecisionStore(REPO).load(first.pr)
    assert retained is not None and retained.ordinary_correction is not None
    assert {item.thread_id for binding in retained.prior_discussion_bindings for item in binding.restore().thread_dispositions} == {first.thread_id}
    assert {item.thread_id for item in retained.ordinary_correction.restore().thread_dispositions} == {second.thread_id}

    first_change = "First accepted discussion now identifies a failing transition."
    second_change = "Second accepted discussion now identifies another failing transition."
    first.thread_replies.append(ReviewThreadComment(database_id=11000, author_id=703, author_login="reviewer[bot]", body=first_change, in_reply_to_id=first.root_id))
    if both_changed:
        second.thread_replies.append(ReviewThreadComment(database_id=12000, author_id=703, author_login="reviewer[bot]", body=second_change, in_reply_to_id=second.root_id))
    second.env = second.env.reconstructed()

    def reevaluate():
        response = json.loads(ordinary_response(first.thread_id, status=next_status, evidence="tests/test_state.py independently exercises the current transition"))
        response["thread_dispositions"].extend(json.loads(ordinary_response(second.thread_id, status=next_status, evidence="tests/test_state.py independently exercises the second transition"))["thread_dispositions"])
        second.model_responses = [json.dumps(response)]
        calls = second.model_calls
        actions = second.run()
        assert second.model_calls == calls + 1, actions
        assert first_change in second.validation_sections[-1]
        assert first.thread_id in second.validation_sections[-1] and second.thread_id in second.validation_sections[-1]
        snapshot = second.env.ledger.get_snapshot("https://api.github.com", REPO, first.pr, require_retained_state=True)
        expected = BlockerDisposition.OPEN if next_status == "STILL_VALID" else BlockerDisposition.VERIFIED_CORRECTION
        assert len(snapshot.blockers) == 2
        assert all(blocker.disposition is expected for blocker in snapshot.blockers)
        assert second.merge.call_count == 0

    reevaluate()
    if not both_changed:
        second.thread_replies.append(ReviewThreadComment(database_id=12000, author_id=703, author_login="reviewer[bot]", body=second_change, in_reply_to_id=second.root_id))
        second.env = second.env.reconstructed()
        reevaluate()
    assert second_change in second.validation_sections[-1]
