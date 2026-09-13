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
    assert parse_decision(render_decision(decision(A, verdict="OVERRULE", directive="NO_CHANGE"))).directive == "NO_CHANGE"
    assert parse_decision(render_decision(decision(A, verdict="UNDECIDED", directive="NONE"))).verdict == "UNDECIDED"
    with pytest.raises(ValueError, match="exact v1"):
        parse_decision("quoted " + render_decision(value))
    duplicate = render_decision(value).replace("{", '{"decision_id":"' + B + '",', 1)
    with pytest.raises(ValueError, match="duplicate JSON key"):
        parse_decision(duplicate)
    with pytest.raises(ValueError, match="unsupported verdict"):
        parse_decision(render_decision(value).replace('"FIX"', '"NONE"'))


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


def test_reconcile_unavailable_revision_and_tip_author_revocation() -> None:
    state = ledger()
    state.ingest(source(1, decision(A)), [8], [42])
    args = dict(
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
    assert state.reconcile(available=False, **args).status is AdjudicationStatus.SOURCE_UNAVAILABLE
    assert state.reconcile(available=True, **args).status is AdjudicationStatus.APPLICABLE
    assert state.reconcile(available=True, **{**args, "adjudicator_ids": []}).status is AdjudicationStatus.REVOKED
    assert state.reconcile(available=True, **args).status is AdjudicationStatus.INVALID


def test_checked_in_schema_has_exact_production_fields() -> None:
    from pathlib import Path

    schema = json.loads((Path(__file__).parents[1] / "src/auto_coder/review_adjudication.schema.json").read_text())
    assert set(schema["required"]) == set(schema["properties"])
