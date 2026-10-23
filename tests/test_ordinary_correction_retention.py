"""Ordinary corrective evidence and semantic retry identity over the real stores."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from auto_coder.adversarial_validation_attempts import AdversarialValidationAttemptRepository
from auto_coder.adversarial_validator import AdversarialValidationFinding, AdversarialValidationResult, RequirementCoverageEntry
from auto_coder.canonical_pr_blocker_ledger import BlockerAdmissionPayload, EvidenceAvailability, QualifiedRequirement, StaleLedgerRevisionError
from auto_coder.effective_decision_application import (
    HANDOFF_DISPATCHED,
    HANDOFF_PENDING,
    PUBLICATION_CONFIRMED,
    PUBLICATION_PENDING,
    DecisionRetentionError,
    EffectiveDecisionStore,
    RetainedDecision,
    build_retained_record,
    derive_application,
    evidence_revision,
    retain_ordinary_correction,
)
from auto_coder.ordinary_correction_references import build_correction_reference_context
from tests.test_accepted_finding_bridge import (  # noqa: F401 (env is a shared fixture)
    REPO,
    Env,
    accept_strong,
    env,
    finding_json,
    published_roots,
)
from tests.test_effective_decision_pr_flow import flow_env  # noqa: F401 (shared production flow fixture)


def ordinary_violation(env: Env, pr: int) -> AdversarialValidationResult:
    attempt = AdversarialValidationAttemptRepository(REPO).start(pr, env.head)
    return AdversarialValidationResult(
        result="NEEDS_FIX",
        attempt_id=attempt.attempt_id,
        attempt_sequence=attempt.sequence,
        summary="JOIN ON rewrites a table name without rewriting its inner reference.",
        requirement_coverage=[RequirementCoverageEntry("#2401/REQ-001", "VIOLATED", "Rewritten JOIN ON raises a missing FROM-clause error.")],
        findings=[
            AdversarialValidationFinding(
                requirement_id="#2401/REQ-001",
                finding_identity="ordinary-join-on",
                correction_identity="new-join-on",
                counterexample="tasks_2 JOIN notes_2 ON tasks.id = notes_2.task_id",
                evidence="Execute the rewritten query against the destination tables.",
            )
        ],
    )


@pytest.mark.parametrize("closure_diagnostic", ["", "closure assessment is absent", "closure assessment contains contradictory identities"])
def test_normalized_ordinary_evidence_survives_restart_without_a_closure_extension(env: Env, closure_diagnostic: str) -> None:
    pr = 7101
    accept_strong(env, pr, [finding_json("historical-finding", gap=False)])
    projection = env.bridge().project(env.target(pr))
    raw = ordinary_violation(env, pr)
    raw.closure_assessment_diagnostic = closure_diagnostic
    context = build_correction_reference_context(env.ledger.get_snapshot("https://api.github.com", REPO, pr), env.head, env.base, "snapshot-1", "requirements-1")
    raw.correction_reference_context = context
    application = derive_application(raw, projection)
    evidence = retain_ordinary_correction(raw, projection)
    EffectiveDecisionStore(REPO).retain(build_retained_record(application.decision, projection, env.head, env.base, evidence))

    retained = EffectiveDecisionStore(REPO).load(pr)
    assert retained is not None and retained.ordinary_correction is not None
    restored = retained.ordinary_correction.restore()

    assert restored.result == "NEEDS_FIX"
    assert restored.findings == raw.findings
    assert isinstance(restored.findings[0], AdversarialValidationFinding)
    assert restored.requirement_coverage == raw.requirement_coverage
    assert restored.closure_assessment is None
    assert restored.closure_assessment_diagnostic == closure_diagnostic
    assert restored.correction_reference_context == context
    assert restored.accepted_finding_projection is None
    assert retained.ordinary_correction.accepted_sources == tuple(record.source_identity for record in projection.records)
    assert (retained.ordinary_correction.head_sha, retained.ordinary_correction.base_sha) == (env.head, env.base)


@pytest.mark.parametrize("problem", ["attempt_id", "attempt_sequence", "version", "payload"])
def test_retained_evidence_rejects_mismatched_attempt_and_unreadable_payload(env: Env, problem: str) -> None:
    pr = 7102
    raw = ordinary_violation(env, pr)
    evidence = retain_ordinary_correction(raw, env.bridge().project(env.target(pr)))
    if problem == "attempt_id":
        evidence = replace(evidence, attempt_id="another-attempt")
    elif problem == "attempt_sequence":
        evidence = replace(evidence, attempt_sequence=evidence.attempt_sequence + 1)
    elif problem == "version":
        evidence = replace(evidence, version=999)
    else:
        evidence = replace(evidence, payload=json.dumps({"result": "NEEDS_FIX", "attempt_id": "unbound"}))

    with pytest.raises(DecisionRetentionError):
        evidence.restore()


def test_semantic_retry_ignores_identical_observation_and_unrelated_ledger_mutations_but_keeps_cas(env: Env) -> None:
    pr = 7103
    accept_strong(env, pr, [finding_json("finding-a", gap=False)])
    before = env.bridge().project(env.target(pr))
    blocker_id = before.records[0].canonical_blocker_id
    revision = evidence_revision(before)
    arguments = dict(
        api_origin="https://api.github.com",
        repository=REPO,
        pr_number=pr,
        operation_id="same-observation",
        expected_ledger_revision=before.ledger_revision,
        blocker_id=blocker_id,
        evidence_availability=EvidenceAvailability.KNOWN,
        evidence="The accepted correction remains open.",
        reviewed_head_sha=env.head,
        reviewed_base_sha=env.base,
        review_attempt_id="ordinary-1",
        review_observation_identity="finding-observation-1",
    )
    observed = env.ledger.record_evidence(**arguments)
    assert observed.ledger_revision > before.ledger_revision
    replayed = env.ledger.record_evidence(**arguments)
    assert replayed.ledger_revision == observed.ledger_revision
    assert evidence_revision(env.reconstructed().bridge().project(env.target(pr))) == revision

    with pytest.raises(StaleLedgerRevisionError):
        env.ledger.record_evidence(**{**arguments, "operation_id": "stale-writer"})

    _, unrelated = env.ledger.admit_blocker(
        "https://api.github.com",
        REPO,
        pr,
        operation_id="unrelated-ordinary-owner",
        expected_ledger_revision=observed.ledger_revision,
        payload=BlockerAdmissionPayload(
            category="IMPLEMENTATION",
            qualified_requirements=(QualifiedRequirement(2401, "REQ-002"),),
            authoritative_boundary="src/unrelated.py",
            incorrect_behavior_or_missing_invariant="An independent counter is not persisted.",
            required_correction_outcome="Persist the independent counter.",
        ),
    )
    assert unrelated.ledger_revision > observed.ledger_revision
    assert evidence_revision(env.reconstructed().bridge().project(env.target(pr))) == revision


def test_real_association_change_after_spending_remains_unspent_even_when_bookkeeping_advances(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    pr = 7104
    inputs = accept_strong(env, pr, [finding_json("finding-a", gap=False)])
    before = env.bridge().project(env.target(pr))
    consumed = evidence_revision(before)
    store = EffectiveDecisionStore(REPO)
    store.save(RetainedDecision(repository=REPO, pr_number=pr, head_sha=env.head, base_sha=env.base, reconciliation_attempts=[consumed], evidence_revision=consumed))
    strong = env.cycle.snapshot(pr).accepted_strong_round
    assert strong is not None
    inputs.gate.state.acknowledge_publication(pr, strong.round_id)
    roots = published_roots(env, pr, monkeypatch, root_ids={"finding-a": 7199})
    after = env.reconstructed().bridge().project(env.target(pr), roots)
    newly_observed = evidence_revision(after)
    assert newly_observed != consumed
    assert after.ledger_revision > before.ledger_revision
    retained = EffectiveDecisionStore(REPO).load(pr)
    assert retained is not None
    assert newly_observed not in retained.reconciliation_attempts
    assert store.spend_reconciliation(pr, newly_observed, env.head, env.base)
    assert not EffectiveDecisionStore(REPO).spend_reconciliation(pr, newly_observed, env.head, env.base)


@pytest.mark.parametrize("target_field", ["head_sha", "base_sha", "contract_identity", "policy_identity"])
def test_semantic_retry_tracks_each_applicable_target_boundary(env: Env, target_field: str) -> None:
    pr = 7105
    accept_strong(env, pr, [finding_json("finding-a", gap=False)])
    target = env.target(pr)
    before = env.bridge().project(target)
    after = env.reconstructed().bridge().project(replace(target, **{target_field: "changed-authority"}))

    assert evidence_revision(after) != evidence_revision(before)


def test_stale_contract_cannot_fall_through_to_legacy_report_dispatch(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from auto_coder.pr_review_cycle import ContractSnapshot
    from tests.test_effective_decision_pr_flow import _historical_root_with_current_violation

    flow, script, _ = _historical_root_with_current_violation(flow_env, monkeypatch, "cloud", "missing")
    flow.quota_hold = True
    initial = flow.run(closure=script)
    assert flow.model_calls == 1 and flow.handoffs == 0 and len(flow.reviews) == 1, initial
    flow.quota_hold = False
    monkeypatch.setattr("tests.test_effective_decision_pr_flow.CONTRACT", ContractSnapshot(("#2401",), "Issue #2401 REQ-001: Preserve accepted findings under the revised contract."))
    flow.model_responses = []

    actions = flow.run(closure=script)

    assert flow.model_calls == 1 and len(flow.reviews) == 1 and flow.handoffs == 0, list(actions)
    assert actions.disposition.value == "RECONCILIATION_WAIT"
    assert flow.merge.call_count == 0


@pytest.mark.parametrize("legacy_envelope", [False, True], ids=["typed-evidence", "legacy-without-payload"])
def test_base_shift_spends_one_fresh_admission_before_review_and_keeps_history(flow_env: Env, monkeypatch: pytest.MonkeyPatch, legacy_envelope: bool) -> None:
    from tests.test_effective_decision_pr_flow import _historical_root_with_current_violation, _reconstruct_flow_stores, _seed_legacy_blocked_envelope

    flow, script, raw = _historical_root_with_current_violation(flow_env, monkeypatch, "cloud", "missing")
    flow.quota_hold = True
    initial = flow.run(closure=script)
    assert flow.model_calls == 1 and flow.handoffs == 0 and len(flow.reviews) == 1, initial
    if legacy_envelope:
        _seed_legacy_blocked_envelope(flow, retain_corrective_payload=False)
    original = flow.retained()
    assert original is not None and (original.ordinary_correction is None) is legacy_envelope
    flow.env = replace(flow.env, base="advanced-base")
    flow.current_base = flow.env.base
    flow.quota_hold = False
    raw["thread_dispositions"] = [{"thread_id": thread.id, "status": "STILL_VALID", "rationale": "The distinct correction remains required on the advanced base.", "evidence": "src/state.py:40 still drops state."} for thread in flow.threads() if not thread.is_resolved]
    flow.model_responses = [json.dumps(raw)]
    _reconstruct_flow_stores(flow)
    admissions: list[str] = []

    def assert_new_target_was_spent_before_review() -> None:
        retained = flow.retained()
        assert retained is not None and retained.base_sha == original.base_sha
        assert set(original.reconciliation_attempts).issubset(retained.reconciliation_attempts)
        keys = [key for key in retained.reconciliation_attempts if key.startswith("ordinary-correction-v1:")]
        assert len(keys) == 1
        admissions.extend(keys)

    script.on_review = assert_new_target_was_spent_before_review
    actions = flow.run(closure=script)
    _reconstruct_flow_stores(flow)
    flow.model_responses = []
    again = flow.run(closure=script)

    assert flow.model_calls == 2 and flow.handoffs == 1 and len(flow.reviews) == 2, (list(actions), list(again))
    retained = flow.retained()
    assert retained is not None and retained.base_sha == flow.current_base and retained.ordinary_correction is not None
    assert retained.ordinary_correction.base_sha == flow.current_base
    assert len(admissions) == 1 and admissions[0] in retained.reconciliation_attempts
    assert set(original.reconciliation_attempts).issubset(retained.reconciliation_attempts)
    assert not EffectiveDecisionStore(REPO).spend_reconciliation(flow.pr, admissions[0], flow.head, flow.current_base)
    assert flow.merge.call_count == 0


def test_baseless_closure_placeholder_does_not_suppress_target_bound_legacy_admission(env: Env) -> None:
    pr = 7106
    store = EffectiveDecisionStore(REPO)
    store.record_closure_attempt(pr, "previous-closure", env.head)
    target = env.bridge().project(env.target(pr))
    key = "ordinary-correction-v1:" + evidence_revision(target)

    assert store.spend_reconciliation(pr, key, env.head, env.base)
    retained = EffectiveDecisionStore(REPO).load(pr)
    assert retained is not None and retained.base_sha == ""
    assert retained.closure_attempts == ["previous-closure"] and retained.reconciliation_attempts == [key]
    assert not EffectiveDecisionStore(REPO).spend_reconciliation(pr, key, env.head, env.base)


def test_same_typed_producer_retains_resolved_ordinary_ids_and_confirmed_delivery(env: Env) -> None:
    pr = 7107
    projection = env.bridge().project(env.target(pr))
    raw = ordinary_violation(env, pr)
    evidence = retain_ordinary_correction(raw, projection)
    record = build_retained_record(derive_application(raw, projection).decision, projection, env.head, env.base, evidence)
    assert record.requires_handoff and record.blocker_ids == []
    store = EffectiveDecisionStore(REPO)
    store.retain(record)
    store.update(pr, blocker_ids=["blk_published-ordinary"], publication=PUBLICATION_CONFIRMED, handoff=HANDOFF_DISPATCHED, handoff_generation=1, handoff_observation="task:confirmed")

    resumed = store.retain(replace(record, evidence_revision="new-exact-root-association"))

    assert resumed.blocker_ids == ["blk_published-ordinary"]
    assert resumed.handoff == HANDOFF_DISPATCHED and resumed.publication == PUBLICATION_CONFIRMED
    assert (resumed.handoff_generation, resumed.handoff_observation) == (1, "task:confirmed")
    assert EffectiveDecisionStore(REPO).load(pr) == resumed


@pytest.mark.parametrize("changed_evidence", ["payload", "contract_identity", "attempt", "accepted_sources"])
def test_changed_typed_producer_does_not_inherit_prior_delivery_or_publication(env: Env, changed_evidence: str) -> None:
    pr = 7108
    projection = env.bridge().project(env.target(pr))
    raw = ordinary_violation(env, pr)
    evidence = retain_ordinary_correction(raw, projection)
    record = build_retained_record(derive_application(raw, projection).decision, projection, env.head, env.base, evidence)
    store = EffectiveDecisionStore(REPO)
    store.save(replace(record, blocker_ids=["blk_same-canonical-owner"], publication=PUBLICATION_CONFIRMED, handoff=HANDOFF_DISPATCHED, handoff_generation=1, handoff_observation="task:previous"))
    if changed_evidence == "payload":
        changed = retain_ordinary_correction(replace(raw, summary="A changed independently evaluated correction."), projection)
    elif changed_evidence == "attempt":
        changed = retain_ordinary_correction(ordinary_violation(env, pr), projection)
        record = replace(record, attempt_id=changed.attempt_id, attempt_sequence=changed.attempt_sequence)
    elif changed_evidence == "accepted_sources":
        changed = replace(evidence, accepted_sources=("new-round:source",))
    else:
        changed = replace(evidence, contract_identity="new-contract")

    retained = store.retain(replace(record, ordinary_correction=changed, blocker_ids=["blk_same-canonical-owner"]))

    assert retained.handoff == HANDOFF_PENDING and retained.publication == PUBLICATION_PENDING
    assert retained.handoff_generation == 1  # Historical delivery accounting cannot reuse an old closure key.
    assert retained.handoff_observation == ""


def test_superseded_fresh_dispatch_cannot_adopt_another_attempts_unpublished_evidence(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from auto_coder import pr_processor
    from tests.test_effective_decision_pr_flow import _historical_root_with_current_violation

    flow, script, _ = _historical_root_with_current_violation(flow_env, monkeypatch, "cloud", "missing")
    dispatch = pr_processor._dispatch_effective_correction
    newer_attempts: list[str] = []

    def supersede_after_publication(*args: object, **kwargs: object) -> object:
        store = EffectiveDecisionStore(REPO)
        saved = store.load(flow.pr)
        assert saved is not None and saved.ordinary_correction is not None and saved.publication == PUBLICATION_CONFIRMED
        newer = AdversarialValidationAttemptRepository(REPO).start(flow.pr, flow.head)
        raw = replace(saved.ordinary_correction.restore(), attempt_id=newer.attempt_id, attempt_sequence=newer.sequence)
        projection = flow.env.bridge().project(flow.env.target(flow.pr, flow.head))
        evidence = retain_ordinary_correction(raw, projection, authority=replace(saved.ordinary_correction, attempt_id=newer.attempt_id, attempt_sequence=newer.sequence))
        replacement = build_retained_record(derive_application(raw, projection).decision, projection, flow.head, flow.env.base, evidence)
        assert replacement.requires_handoff
        store.retain(replacement)
        newer_attempts.append(newer.attempt_id)
        return dispatch(*args, **kwargs)

    monkeypatch.setattr(pr_processor, "_dispatch_effective_correction", supersede_after_publication)

    actions = flow.run(closure=script)

    assert flow.model_calls == 1 and len(flow.reviews) == 1 and len(newer_attempts) == 1
    assert flow.handoffs == 0 and flow.merge.call_count == 0, list(actions)
    retained = flow.retained()
    assert retained is not None and retained.attempt_id == newer_attempts[0]
    assert retained.publication == PUBLICATION_PENDING and retained.handoff == HANDOFF_PENDING
    assert actions.disposition.value == "RECONCILIATION_WAIT"


@pytest.mark.parametrize("origin", ["cloud", "local"])
@pytest.mark.parametrize("unobservable_root", ["missing", "resolved"])
def test_partial_native_root_coverage_cannot_dispatch_or_spend_the_complete_correction_set(flow_env: Env, monkeypatch: pytest.MonkeyPatch, origin: str, unobservable_root: str) -> None:
    from auto_coder import pr_processor
    from auto_coder.durable_repair_allowance import RepairAllowanceLedger
    from tests.test_effective_decision_pr_flow import _historical_root_with_current_violation

    flow, script, raw = _historical_root_with_current_violation(flow_env, monkeypatch, origin, "missing")
    independent_finding = {
        **raw["findings"][0],
        "finding_identity": "concurrent-update-overwrite",
        "correction_identity": "independent-concurrent-update",
        "required_behavior": "Concurrent appends must retain every unique new value.",
        "actual_behavior": "Concurrent appends overwrite values introduced by the other writer.",
        "counterexample": "Two simultaneous append operations read the same version; the second write replaces the first appended value.",
        "evidence": "The concurrent append path writes the stale collection after another writer has added a distinct element.",
    }
    raw["findings"].append(independent_finding)
    flow.model_responses = [json.dumps(raw)]
    flow.real_local_allowance = origin == "local"
    before_allowance = RepairAllowanceLedger().initialize_namespace("https://api.github.com", REPO, flow.pr)
    selected_sets: list[tuple[str, ...]] = []
    send = pr_processor._send_adversarial_validation_feedback_to_cloud_task

    def hide_one_root_after_selecting_both_owners(*args: Any, **kwargs: Any) -> Any:
        selected = tuple(kwargs["canonical_blocker_ids"])
        assert len(selected) == 2
        selected_sets.append(selected)
        snapshot = flow.env.ledger.get_snapshot("https://api.github.com", REPO, flow.pr)
        owner = snapshot.get_blocker(selected[-1])
        assert owner is not None and owner.get_root_comment_ids()
        hidden_roots = set(owner.get_root_comment_ids())
        observed_threads = flow.threads

        def current_threads():
            threads = observed_threads()
            if unobservable_root == "missing":
                return tuple(thread for thread in threads if not thread.comments or thread.comments[0].database_id not in hidden_roots)
            return tuple(replace(thread, is_resolved=True) if thread.comments and thread.comments[0].database_id in hidden_roots else thread for thread in threads)

        monkeypatch.setattr(flow, "threads", current_threads)
        return send(*args, **kwargs)

    monkeypatch.setattr(pr_processor, "_send_adversarial_validation_feedback_to_cloud_task", hide_one_root_after_selecting_both_owners)

    actions = flow.run(closure=script)

    assert flow.model_calls == 1 and len(flow.reviews) == 1 and len(selected_sets) == 1
    assert flow.handoffs == 0 and not flow.provider.followups and not flow.local_executions, list(actions)
    retained = flow.retained()
    assert retained is not None and set(retained.blocker_ids) == set(selected_sets[0])
    assert retained.publication == PUBLICATION_CONFIRMED and retained.handoff != HANDOFF_DISPATCHED
    assert retained.handoff_generation == 0 and retained.handoff_observation == ""
    assert RepairAllowanceLedger().get_snapshot("https://api.github.com", REPO, flow.pr, require_retained_state=False).generations == before_allowance.generations == ()
    assert flow.env.ledger.get_repair_bundles_for_pr("https://api.github.com", REPO, flow.pr) == ()
    assert flow.merge.call_count == 0


def test_without_strong_policy_changed_requirements_cannot_dispatch_the_saved_correction(flow_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from auto_coder import pr_processor
    from auto_coder.util.gh_cache import PullRequestRepairMetadata
    from tests.test_effective_decision_pr_flow import _historical_root_with_current_violation, _reconstruct_flow_stores

    flow, _, _ = _historical_root_with_current_violation(flow_env, monkeypatch, "cloud", "missing")
    monkeypatch.setattr(pr_processor, "_two_tier_gate_inputs", lambda *_args, **_kwargs: None)
    flow.client.get_pull_request_repair_metadata_strict.return_value = PullRequestRepairMetadata(head_ref="feature-branch", head_sha=flow.head, base_ref="main")
    flow.quota_hold = True
    initial = flow.run()
    retained = flow.retained()
    assert flow.model_calls == 1 and len(flow.reviews) == 1 and flow.handoffs == 0, list(initial)
    assert retained is not None and retained.ordinary_correction is not None
    original = retained.ordinary_correction
    original_context = original.restore().correction_reference_context
    assert not original.requires_strong_policy
    assert original_context is not None and original_context.validation_snapshot and original_context.requirement_manifest_identity

    flow.requirement_text = "Preserve accepted findings until the operator explicitly removes the archived history."
    flow.quota_hold = False
    flow.model_responses = [
        json.dumps(
            {
                "result": "BLOCKED",
                "summary": "The changed requirement needs independent operator-removal evidence before a current correction can be established.",
                "requirement_coverage": [{"requirement_id": "#2401/REQ-001", "status": "UNVERIFIED", "evidence": "The earlier counterexample does not establish whether the operator explicitly removed this history."}],
                "findings": [],
                "test_oracle_gaps": [],
                "thread_dispositions": [],
            }
        )
    ]
    _reconstruct_flow_stores(flow)

    actions = flow.run()
    _reconstruct_flow_stores(flow)
    flow.model_responses = []
    again = flow.run()

    assert flow.model_calls == 2 and len(flow.reviews) == 2, (list(actions), list(again))
    assert flow.handoffs == 0 and not flow.provider.followups and flow.merge.call_count == 0
    assert again.disposition.value == "RECONCILIATION_WAIT"
    current = flow.retained()
    assert current is not None and current.ordinary_correction is not None
    assert (current.head_sha, current.base_sha) == (original.head_sha, original.base_sha)
    current_context = current.ordinary_correction.restore().correction_reference_context
    assert current_context is not None and current_context.validation_snapshot != original_context.validation_snapshot
    assert current_context.requirement_manifest_identity != original_context.requirement_manifest_identity
    assert current.ordinary_correction.attempt_id != original.attempt_id
    assert current.handoff != HANDOFF_DISPATCHED and current.handoff_generation == 0
    assert len([key for key in current.reconciliation_attempts if key.startswith("ordinary-correction-v1:")]) == 1
