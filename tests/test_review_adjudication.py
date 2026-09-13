import hashlib
import json
from dataclasses import replace

import pytest

from auto_coder.review_adjudication import (
    AdjudicationLedger,
    AdjudicationStatus,
    Decision,
    IssueContract,
    Requirement,
    ReviewContext,
    SourceComment,
    contract_identity,
    parse_decision,
    render_decision,
)

CTX = "11111111-1111-4111-8111-111111111111"
A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
C = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def contracts(objective: str = "Keep scope exact.") -> tuple[IssueContract, ...]:
    return (
        IssueContract(20, 2, (Requirement("REQ-002", "β second"),), objective),
        IssueContract(10, 1, (Requirement("REQ-001", "first"),), "First objective"),
    )


def ledger() -> AdjudicationLedger:
    items = contracts()
    _, digest, objectives = contract_identity(items, "v1")
    return AdjudicationLedger(ReviewContext(CTX, 9, "o/r", 7, "T", 100, 42, "Bot", "f" * 64, "r1", "1" * 40, "2" * 40, "main", "v1", items, digest, objectives))


def decision(decision_id: str, supersedes: tuple[str, ...] = (), verdict: str = "UPHOLD", directive: str = "FIX") -> Decision:
    state = ledger().context
    return Decision(decision_id, CTX, state.head_sha, state.contract_digest, verdict, directive, supersedes, "Because it is required.", "dashboard")


def source(comment_id: int, value: Decision, actor: int = 8) -> SourceComment:
    return SourceComment(9, "o/r", 7, "T", 100, comment_id, actor, f"2026-01-01T00:00:{comment_id:02d}Z", "r1", render_decision(value))


def test_canonical_contract_fixture_and_objective_bytes() -> None:
    canonical, digest, fingerprints = contract_identity(contracts(), "v1")
    expected = '{"issues":[{"issue_id":10,"issue_number":1,"requirements":[{"id":"REQ-001","text":"first"}]},{"issue_id":20,"issue_number":2,"requirements":[{"id":"REQ-002","text":"β second"}]}],"parser_version":"v1"}'
    assert canonical == expected
    assert digest == hashlib.sha256(expected.encode()).hexdigest()
    assert fingerprints[1] != contract_identity(contracts("Keep scope exact. "), "v1")[2][1]
    with pytest.raises(ValueError, match="unique positive identities"):
        contract_identity((contracts()[0], contracts()[0]), "v1")


def test_literal_parser_renderer_and_rejections() -> None:
    value = decision(A)
    assert parse_decision(render_decision(value)) == value
    assert parse_decision(" \n\n" + render_decision(value)) == value
    assert parse_decision(render_decision(decision(A, verdict="OVERRULE", directive="NO_CHANGE"))).directive == "NO_CHANGE"
    assert parse_decision(render_decision(decision(A, verdict="UNDECIDED", directive="NONE"))).verdict == "UNDECIDED"
    with pytest.raises(ValueError, match="exact v1"):
        parse_decision("quoted " + render_decision(value))
    duplicate = render_decision(value).replace("{", '{"decision_id":"' + B + '",', 1)
    with pytest.raises(ValueError, match="duplicate JSON key"):
        parse_decision(duplicate)
    with pytest.raises(ValueError, match="unsupported verdict"):
        parse_decision(render_decision(value).replace('"FIX"', '"NONE"'))


@pytest.mark.parametrize("field", ["verdict", "source"])
def test_ingest_returns_invalid_for_non_scalar_enum_fields(field: str) -> None:
    state = ledger()
    raw = render_decision(decision(A)).replace(f'"{field}":"', f'"{field}":["', 1)
    raw = raw.replace('","supersedes"' if field == "source" else '","head_sha"', '"],"supersedes"' if field == "source" else '"],"head_sha"', 1)
    malformed = replace(source(1, decision(A)), raw_body=raw)
    assert state.ingest(malformed, [8], [42]).status is AdjudicationStatus.INVALID
    assert state.context.decisions == {}


def test_authority_matching_and_immutable_reobservation() -> None:
    state = ledger()
    physical = source(1, decision(A))
    assert state.ingest(physical, [8], [42]).status is AdjudicationStatus.APPLICABLE
    assert state.ingest(physical, [8], [42]).reason == "same immutable activity"
    wrong = replace(source(2, decision(B)), thread_id="other")
    assert state.ingest(wrong, [8], [42]).status is AdjudicationStatus.UNAUTHORIZED
    assert state.ingest(source(3, decision(B), actor=9), [8], [42]).status is AdjudicationStatus.UNAUTHORIZED
    stale = replace(decision(B), head_sha="9" * 40)
    assert state.ingest(source(4, stale), [8], [42]).status is AdjudicationStatus.STALE


def test_graph_is_source_ordered_and_delivery_order_independent() -> None:
    def run(order: tuple[str, ...]) -> AdjudicationLedger:
        state = ledger()
        values = {A: source(1, decision(A)), B: source(2, decision(B, (A,))), C: source(3, decision(C, (A,)))}
        state.ingest(values[A], [8], [42])
        for item in order:
            state.ingest(values[item], [8], [42])
        return state

    first, second = run((B, C)), run((C, B))
    assert first.tips() == second.tips() == (B, C)
    assert first.current(None, None, "check").status is AdjudicationStatus.CONFLICT
    d = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    assert first.ingest(source(4, decision(d, (B, C))), [8], [42]).tips == (d,)
    assert AdjudicationLedger.loads(first.dumps()).tips() == (d,)


def test_predecessor_successor_delivery_order_and_restart_are_equivalent() -> None:
    expected = ledger()
    expected.ingest(source(1, decision(A)), [8], [42])
    expected.ingest(source(2, decision(B, (A,))), [8], [42])
    reversed_state = ledger()
    assert reversed_state.ingest(source(2, decision(B, (A,))), [8], [42]).status is AdjudicationStatus.SOURCE_UNAVAILABLE
    reversed_state.ingest(source(1, decision(A)), [8], [42])
    assert reversed_state.tips() == expected.tips() == (B,)
    assert AdjudicationLedger.loads(reversed_state.dumps()).tips() == (B,)


def test_pending_duplicate_identity_retires_independent_of_delivery_order() -> None:
    first = ledger()
    first.ingest(source(1, decision(A)), [8], [42])
    first.ingest(source(2, decision(B, (A,))), [8], [42])
    assert first.ingest(source(3, decision(B, (A,))), [8], [42]).status is AdjudicationStatus.INVALID

    delayed = ledger()
    delayed.ingest(source(2, decision(B, (A,))), [8], [42])
    assert delayed.ingest(source(3, decision(B, (A,))), [8], [42]).status is AdjudicationStatus.INVALID
    restored = AdjudicationLedger.loads(delayed.dumps())
    assert restored.ingest(source(1, decision(A)), [8], [42]).status is AdjudicationStatus.INVALID
    assert first.current(None, None, "first order").status is restored.current(None, None, "last order").status is AdjudicationStatus.INVALID


@pytest.mark.parametrize("supersedes", [(B, A), (A, B)])
def test_known_forward_reference_is_rejected_before_missing_predecessor(supersedes: tuple[str, ...]) -> None:
    state = ledger()
    later_a = replace(source(5, decision(A)), created_at="2026-01-01T00:00:05Z")
    state.ingest(later_a, [8], [42])
    earlier_c = replace(source(3, decision(C, supersedes)), created_at="2026-01-01T00:00:03Z")
    result = state.ingest(earlier_c, [8], [42])
    assert result.status is AdjudicationStatus.INVALID
    assert state.context.pending_decisions == {}
    assert state.current(None, None, "A remains eligible").status is AdjudicationStatus.APPLICABLE


def test_pending_forward_reference_is_discarded_when_later_predecessor_arrives() -> None:
    state = ledger()
    earlier_c = replace(source(3, decision(C, (B, A))), created_at="2026-01-01T00:00:03Z")
    state.ingest(earlier_c, [8], [42])
    state = AdjudicationLedger.loads(state.dumps())
    later_a = replace(source(5, decision(A)), created_at="2026-01-01T00:00:05Z")
    result = state.ingest(later_a, [8], [42])
    assert result.status is AdjudicationStatus.APPLICABLE
    assert result.decision_id == A
    assert state.context.pending_decisions == {}


def test_collision_edit_deletion_and_corruption_fail_closed() -> None:
    state = ledger()
    original = source(1, decision(A))
    state.ingest(original, [8], [42])
    collision = source(2, decision(A))
    assert state.ingest(collision, [8], [42]).status is AdjudicationStatus.INVALID
    assert state.context.retired_reason is not None

    edited = ledger()
    edited.ingest(original, [8], [42])
    assert edited.ingest(replace(original, update_revision="r2"), [8], [42]).status is AdjudicationStatus.INVALID
    deleted = ledger()
    deleted.ingest(original, [8], [42])
    deleted.observe_deletion(1)
    assert deleted.context.retired_reason == "accepted source 1 was confirmed deleted"
    corrupt = deleted.dumps().replace(deleted.context.contract_digest, "0" * 64)
    with pytest.raises(ValueError, match="corrupt"):
        AdjudicationLedger.loads(corrupt)


def test_serialized_history_rejects_missing_or_forged_decisions() -> None:
    state = ledger()
    state.ingest(source(1, decision(A)), [8], [42])
    state.ingest(source(2, decision(B, (A,))), [8], [42])
    missing = json.loads(state.dumps())
    del missing["context"]["decisions"][A]
    with pytest.raises(ValueError, match="corrupt"):
        AdjudicationLedger.loads(json.dumps(missing))
    forged = json.loads(state.dumps())
    forged["context"]["decisions"][B]["decision"]["verdict"] = "OVERRULE"
    forged["context"]["decisions"][B]["decision"]["directive"] = "NO_CHANGE"
    with pytest.raises(ValueError, match="corrupt"):
        AdjudicationLedger.loads(json.dumps(forged))


@pytest.mark.parametrize("retirement", ["collision", "edit", "deletion", "head", "revocation"])
def test_every_retirement_permanently_blocks_selection_reread_and_restart(retirement: str) -> None:
    state = ledger()
    original = source(1, decision(A))
    state.ingest(original, [8], [42])
    args = reconciliation_args(state)
    if retirement == "collision":
        state.ingest(source(2, decision(A)), [8], [42])
    elif retirement == "edit":
        state.ingest(replace(original, update_revision="r2"), [8], [42])
    elif retirement == "deletion":
        state.observe_deletion(1)
    elif retirement == "head":
        state.reconcile(available=True, **{**args, "head_sha": "3" * 40})
    else:
        state.reconcile(available=True, **{**args, "adjudicator_ids": []})
    assert state.current(None, None, "selection").status is AdjudicationStatus.INVALID
    assert state.ingest(original, [8], [42]).status is AdjudicationStatus.INVALID
    assert AdjudicationLedger.loads(state.dumps()).current(None, None, "restart").status is AdjudicationStatus.INVALID


def reconciliation_args(state: AdjudicationLedger) -> dict[str, object]:
    return dict(
        root_body_hash="f" * 64,
        root_update_revision="r1",
        root_author_id=42,
        root_actor_type="Bot",
        head_sha="1" * 40,
        base_sha="2" * 40,
        base_ref="main",
        contract_digest=state.context.contract_digest,
        objective_fingerprints=state.context.objective_fingerprints,
        root_reviewer_ids=[42],
        adjudicator_ids=[8],
    )


def test_reconcile_unavailable_revision_and_tip_author_revocation() -> None:
    state = ledger()
    state.ingest(source(1, decision(A)), [8], [42])
    args = reconciliation_args(state)
    assert state.reconcile(available=False, **args).status is AdjudicationStatus.SOURCE_UNAVAILABLE
    assert state.current(None, None, "during outage").status is AdjudicationStatus.SOURCE_UNAVAILABLE
    assert state.ingest(source(1, decision(A)), [8], [42]).status is AdjudicationStatus.SOURCE_UNAVAILABLE
    assert AdjudicationLedger.loads(state.dumps()).current(None, None, "restart").status is AdjudicationStatus.SOURCE_UNAVAILABLE
    assert state.reconcile(available=True, **args).status is AdjudicationStatus.APPLICABLE
    assert state.reconcile(available=True, **{**args, "adjudicator_ids": []}).status is AdjudicationStatus.REVOKED
    assert state.reconcile(available=True, **args).status is AdjudicationStatus.INVALID


def test_historical_author_revocation_does_not_retire_but_tip_revocation_does() -> None:
    state = ledger()
    predecessor = source(1, decision(A), actor=8)
    state.ingest(predecessor, [8, 9], [42])
    state.ingest(source(2, decision(B, (A,)), actor=9), [8, 9], [42])
    reread = state.ingest(predecessor, [9], [42])
    assert (reread.status, reread.decision_id) == (AdjudicationStatus.APPLICABLE, B)
    assert state.context.retired_reason is None

    conflict = ledger()
    conflict.ingest(source(1, decision(A), actor=8), [8, 9], [42])
    conflict.ingest(source(2, decision(B), actor=9), [8, 9], [42])
    successor = source(3, decision(C, (A, B)), actor=8)
    assert conflict.ingest(successor, [8], [42]).status is AdjudicationStatus.REVOKED
    assert conflict.current(None, None, "after attempted successor").status is AdjudicationStatus.INVALID


def test_revoked_pending_author_cannot_gain_authority_on_promotion() -> None:
    state = ledger()
    state.ingest(source(2, decision(B, (A,)), actor=9), [8, 9], [42])
    assert state.reconcile(available=True, **{**reconciliation_args(state), "adjudicator_ids": [8]}).status is AdjudicationStatus.SOURCE_UNAVAILABLE
    result = state.ingest(source(1, decision(A), actor=8), [8], [42])
    assert (result.status, result.decision_id, result.actual_actor_id) == (AdjudicationStatus.APPLICABLE, A, 8)
    assert B not in state.context.decisions


def test_pending_branch_suspends_existing_authority_through_restart() -> None:
    state = ledger()
    state.ingest(source(1, decision(A)), [8], [42])
    pending = source(3, decision(C, (B,)))
    assert state.ingest(pending, [8], [42]).status is AdjudicationStatus.SOURCE_UNAVAILABLE
    assert state.current(None, None, "incomplete branch").status is AdjudicationStatus.SOURCE_UNAVAILABLE
    restored = AdjudicationLedger.loads(state.dumps())
    assert restored.current(None, None, "restart").status is AdjudicationStatus.SOURCE_UNAVAILABLE
    result = restored.ingest(source(2, decision(B)), [8], [42])
    assert result.status is AdjudicationStatus.CONFLICT
    assert result.tips == (A, C)


def test_pending_source_deletion_retires_before_restart_and_promotion() -> None:
    state = ledger()
    state.ingest(source(2, decision(B, (A,))), [8], [42])
    state.observe_deletion(2)
    restored = AdjudicationLedger.loads(state.dumps())
    assert restored.ingest(source(1, decision(A)), [8], [42]).status is AdjudicationStatus.INVALID
    assert restored.current(None, None, "deleted pending source").status is AdjudicationStatus.INVALID


def test_confirmed_edit_during_outage_permanently_retires() -> None:
    state = ledger()
    original = source(1, decision(A))
    state.ingest(original, [8], [42])
    state.reconcile(available=False, **reconciliation_args(state))
    assert state.ingest(replace(original, update_revision="r2"), [8], [42]).status is AdjudicationStatus.INVALID
    restored = AdjudicationLedger.loads(state.dumps())
    assert restored.reconcile(available=True, **reconciliation_args(restored)).status is AdjudicationStatus.INVALID


def test_missing_revision_reread_suspends_then_recovers_known_identity() -> None:
    state = ledger()
    original = source(1, decision(A))
    state.ingest(original, [8], [42])
    assert state.ingest(replace(original, update_revision=""), [8], [42]).status is AdjudicationStatus.SOURCE_UNAVAILABLE
    assert state.context.retired_reason is None
    restored = AdjudicationLedger.loads(state.dumps())
    assert restored.reconcile(available=True, **reconciliation_args(restored)).decision_id == A
    assert restored.context.retired_reason is None

    pending = ledger()
    incomplete_graph_source = source(2, decision(B, (A,)))
    pending.ingest(incomplete_graph_source, [8], [42])
    assert pending.ingest(replace(incomplete_graph_source, update_revision=""), [8], [42]).status is AdjudicationStatus.SOURCE_UNAVAILABLE
    assert pending.context.retired_reason is None


@pytest.mark.parametrize("pending", [False, True])
def test_changed_body_with_missing_revision_permanently_retires(pending: bool) -> None:
    state = ledger()
    original = source(2, decision(B, (A,))) if pending else source(1, decision(A))
    state.ingest(original, [8], [42])
    changed = replace(original, raw_body=original.raw_body + "\n", update_revision="")
    assert state.ingest(changed, [8], [42]).status is AdjudicationStatus.INVALID
    assert state.context.tombstones == [f"accepted source {original.comment_id} was edited"]
    restored = AdjudicationLedger.loads(state.dumps())
    assert restored.reconcile(available=True, **reconciliation_args(restored)).status is AdjudicationStatus.INVALID
    if pending:
        assert restored.ingest(source(1, decision(A)), [8], [42]).status is AdjudicationStatus.INVALID


@pytest.mark.parametrize("pending", [False, True])
def test_changed_revision_with_missing_creation_permanently_retires(pending: bool) -> None:
    state = ledger()
    original = source(2, decision(B, (A,))) if pending else source(1, decision(A))
    state.ingest(original, [8], [42])
    changed = replace(original, update_revision="r2", created_at="")
    assert state.ingest(changed, [8], [42]).status is AdjudicationStatus.INVALID
    assert state.context.tombstones == [f"accepted source {original.comment_id} was edited"]
    restored = AdjudicationLedger.loads(state.dumps())
    assert restored.reconcile(available=True, **reconciliation_args(restored)).status is AdjudicationStatus.INVALID
    if pending:
        assert restored.ingest(source(1, decision(A)), [8], [42]).status is AdjudicationStatus.INVALID


@pytest.mark.parametrize("revoked", ["tip", "root"])
def test_authorization_revocation_during_outage_is_permanent(revoked: str) -> None:
    state = ledger()
    original = source(1, decision(A))
    state.ingest(original, [8], [42])
    state.reconcile(available=False, **reconciliation_args(state))
    adjudicators = [] if revoked == "tip" else [8]
    reviewers = [42] if revoked == "tip" else []
    assert state.ingest(original, adjudicators, reviewers).status is AdjudicationStatus.REVOKED
    restored = AdjudicationLedger.loads(state.dumps())
    assert restored.reconcile(available=True, **reconciliation_args(restored)).status is AdjudicationStatus.INVALID


def test_authorized_identity_collision_during_outage_is_permanent() -> None:
    state = ledger()
    state.ingest(source(1, decision(A)), [8], [42])
    state.reconcile(available=False, **reconciliation_args(state))
    assert state.ingest(source(2, decision(A)), [8], [42]).status is AdjudicationStatus.INVALID
    assert state.context.tombstones == [f"decision identity collision for {A}"]
    restored = AdjudicationLedger.loads(state.dumps())
    assert restored.reconcile(available=True, **reconciliation_args(restored)).status is AdjudicationStatus.INVALID


@pytest.mark.parametrize("missing_field", ["retired_reason", "source_unavailable"])
def test_serialized_lifecycle_fields_are_required(missing_field: str) -> None:
    state = ledger()
    state.ingest(source(1, decision(A)), [8], [42])
    if missing_field == "retired_reason":
        state.observe_deletion(1)
    else:
        state.reconcile(available=False, **reconciliation_args(state))
    snapshot = json.loads(state.dumps())
    del snapshot["context"][missing_field]
    with pytest.raises(ValueError, match="corrupt"):
        AdjudicationLedger.loads(json.dumps(snapshot))


def test_serialized_source_relation_must_match_registered_context() -> None:
    state = ledger()
    state.ingest(source(1, decision(A)), [8], [42])
    snapshot = json.loads(state.dumps())
    snapshot["context"]["decisions"][A]["source"]["thread_id"] = "OTHER"
    with pytest.raises(ValueError, match="corrupt"):
        AdjudicationLedger.loads(json.dumps(snapshot))


def test_serialized_conflict_cannot_lose_an_independent_tip() -> None:
    state = ledger()
    state.ingest(source(1, decision(A)), [8], [42])
    state.ingest(source(2, decision(B)), [8], [42])
    snapshot = json.loads(state.dumps())
    del snapshot["context"]["decisions"][B]
    with pytest.raises(ValueError, match="corrupt"):
        AdjudicationLedger.loads(json.dumps(snapshot))


def test_serialized_conflict_requires_nonblank_integrity_evidence() -> None:
    state = ledger()
    state.ingest(source(1, decision(A)), [8], [42])
    state.ingest(source(2, decision(B)), [8], [42])
    snapshot = json.loads(state.dumps())
    del snapshot["context"]["decisions"][B]
    snapshot["context"]["integrity_digest"] = ""
    with pytest.raises(ValueError, match="corrupt"):
        AdjudicationLedger.loads(json.dumps(snapshot))


def test_serialized_context_revision_must_match_decision_envelopes() -> None:
    state = ledger()
    state.ingest(source(1, decision(A)), [8], [42])
    snapshot = json.loads(state.dumps())
    snapshot["context"]["head_sha"] = "3" * 40
    with pytest.raises(ValueError, match="corrupt"):
        AdjudicationLedger.loads(json.dumps(snapshot))


def test_serialized_physical_comment_identity_must_be_unique() -> None:
    state = ledger()
    state.ingest(source(1, decision(A)), [8], [42])
    state.ingest(source(2, decision(B, (A,))), [8], [42])
    snapshot = json.loads(state.dumps())
    snapshot["context"]["decisions"][B]["source"]["comment_id"] = 1
    with pytest.raises(ValueError, match="corrupt"):
        AdjudicationLedger.loads(json.dumps(snapshot))


def test_missing_source_revision_suspends_without_accepting_decision() -> None:
    state = ledger()
    incomplete = replace(source(1, decision(A)), update_revision="")
    assert state.ingest(incomplete, [8], [42]).status is AdjudicationStatus.SOURCE_UNAVAILABLE
    assert state.context.decisions == {}
    assert AdjudicationLedger.loads(state.dumps()).current(None, None, "restart").status is AdjudicationStatus.SOURCE_UNAVAILABLE


def test_missing_creation_instant_suspends_without_mutating_history() -> None:
    state = ledger()
    incomplete = replace(source(1, decision(A)), created_at="")
    assert state.ingest(incomplete, [8], [42]).status is AdjudicationStatus.SOURCE_UNAVAILABLE
    assert state.context.decisions == {}
    state.reconcile(available=True, **reconciliation_args(state))
    assert state.ingest(source(1, decision(A)), [8], [42]).status is AdjudicationStatus.APPLICABLE
    assert AdjudicationLedger.loads(state.dumps()).current(None, None, "restart").decision_id == A


def test_self_reference_and_pending_cycle_do_not_reserve_identity() -> None:
    state = ledger()
    assert state.ingest(source(1, decision(A, (A,))), [8], [42]).status is AdjudicationStatus.INVALID
    assert state.context.pending_decisions == {}
    assert state.ingest(source(2, decision(A)), [8], [42]).status is AdjudicationStatus.APPLICABLE

    cycle = ledger()
    assert cycle.ingest(source(2, decision(A, (B,))), [8], [42]).status is AdjudicationStatus.SOURCE_UNAVAILABLE
    assert cycle.ingest(source(3, decision(B, (A,))), [8], [42]).status is AdjudicationStatus.INVALID
    assert cycle.context.pending_decisions == {}


def test_effective_tip_result_uses_tip_physical_source() -> None:
    state = ledger()
    state.ingest(source(1, decision(A), actor=8), [8, 9], [42])
    state.ingest(source(2, decision(B, (A,)), actor=9), [8, 9], [42])
    reread = state.ingest(source(1, decision(A), actor=8), [8, 9], [42])
    assert (reread.decision_id, reread.actual_actor_id, reread.source_comment_id) == (B, 9, 2)
    reconciled = state.reconcile(available=True, **{**reconciliation_args(state), "adjudicator_ids": [8, 9]})
    assert (reconciled.decision_id, reconciled.actual_actor_id, reconciled.source_comment_id) == (B, 9, 2)


def test_context_registration_rejects_missing_contracts_and_objectives() -> None:
    with pytest.raises(ValueError, match="complete Objectives"):
        contract_identity(contracts(""), "v1")
    valid = ledger().context
    with pytest.raises(ValueError, match="complete contracts"):
        AdjudicationLedger(replace(valid, contracts=(), objective_fingerprints=()))
    with pytest.raises(ValueError, match="incomplete registered review context"):
        AdjudicationLedger(replace(valid, root_update_revision=""))


def test_checked_in_schema_has_exact_production_fields() -> None:
    from pathlib import Path

    schema = json.loads((Path(__file__).parents[1] / "src/auto_coder/review_adjudication.schema.json").read_text())
    assert set(schema["required"]) == set(schema["properties"])
