import pytest

from src.auto_coder.parent_issue_reconciliation import ParentDeclarationStatus
from src.auto_coder.sibling_dependencies import (
    UNAVAILABLE,
    BlockedByDeclarationStatus,
    DependencySatisfaction,
    GraphValidity,
    IssueEvidence,
    IssueState,
    IssueType,
    evaluate_family_graph,
    parse_blocked_by_declaration,
)


def create_evidence(
    number: int,
    repository="owner/repo",
    type_=IssueType.ISSUE,
    state=IssueState.OPEN,
    parent=100,
    body="Parent-Issue: #100",
    native_deps=frozenset(),
    nonexistent=False,
):
    return IssueEvidence(
        number=number,
        repository=repository,
        type=type_,
        state=state,
        authoritative_parent=parent,
        body=body,
        observed_native_dependencies=native_deps,
        is_authoritatively_nonexistent=nonexistent,
    )


def test_as001_reversed_issue_numbers():
    # 101 -> 205, 310
    evidence = {
        101: create_evidence(101, body="Parent-Issue: #100\nBlocked-By: #310, #205", state=IssueState.OPEN),
        205: create_evidence(205, body="Parent-Issue: #100", state=IssueState.CLOSED),
        310: create_evidence(310, body="Parent-Issue: #100", state=IssueState.OPEN),
    }
    graph = evaluate_family_graph(evidence, 100, "owner/repo")
    assert graph.results[101].satisfaction == DependencySatisfaction.WAITING

    # Permuted response order doesn't change semantics
    evidence_2 = {
        310: evidence[310],
        205: evidence[205],
        101: evidence[101],
    }
    graph_2 = evaluate_family_graph(evidence_2, 100, "owner/repo")
    assert graph_2.results[101].satisfaction == DependencySatisfaction.WAITING

    # When both close
    evidence[310] = create_evidence(310, body="Parent-Issue: #100", state=IssueState.CLOSED)
    graph_3 = evaluate_family_graph(evidence, 100, "owner/repo")
    assert graph_3.results[101].satisfaction == DependencySatisfaction.SATISFIED


def test_as002_empty_and_absent():
    # Empty declaration clears native dependencies
    ev1 = create_evidence(101, body="Parent-Issue: #100\nBlocked-By:", native_deps=frozenset([205]))
    res1 = evaluate_family_graph({101: ev1}, 100, "owner/repo").results[101]
    assert res1.desired_dependencies == frozenset()
    assert res1.is_synchronized is False

    # Absent leaves native dependencies
    ev2 = create_evidence(102, body="Parent-Issue: #100", native_deps=frozenset([205]))
    res2 = evaluate_family_graph({102: ev2, 205: create_evidence(205)}, 100, "owner/repo").results[102]
    assert res2.desired_dependencies == frozenset([205])
    assert res2.is_synchronized is True

    # Blocked-by without Parent-Issue is invalid
    ev3 = create_evidence(103, body="Blocked-By:")
    res3 = evaluate_family_graph({103: ev3}, 100, "owner/repo").results[103]
    assert res3.is_valid_graph == GraphValidity.INVALID


def test_as003_sibling_scope_cannot_be_forged():
    # #205 has native parent #200 (not #100)
    evidence = {
        101: create_evidence(101, body="Parent-Issue: #100\nBlocked-By: #205"),
        205: create_evidence(205, body="Parent-Issue: #100", parent=200),
    }
    # 205 isn't part of the direct children of 100, BUT since it's present in the evidence map,
    # the target validation checks its authoritative_parent, sees 200, and rightfully rejects it
    # as an invalid target because it's not a sibling (GraphValidity.INVALID, not UNRESOLVED).
    graph = evaluate_family_graph(evidence, 100, "owner/repo")
    assert graph.results[101].is_valid_graph == GraphValidity.INVALID

    # Missing evidence means we don't know the parent yet
    evidence_missing = {
        101: create_evidence(101, body="Parent-Issue: #100\nBlocked-By: #205"),
    }
    graph_missing = evaluate_family_graph(evidence_missing, 100, "owner/repo")
    assert graph_missing.results[101].is_valid_graph == GraphValidity.UNRESOLVED

    # Different repository
    evidence_repo = {
        101: create_evidence(101, body="Parent-Issue: #100\nBlocked-By: #205"),
        205: create_evidence(205, repository="owner/other"),
    }
    graph_repo = evaluate_family_graph(evidence_repo, 100, "owner/repo")
    assert graph_repo.results[101].is_valid_graph == GraphValidity.INVALID

    # PR target
    evidence_pr = {
        101: create_evidence(101, body="Parent-Issue: #100\nBlocked-By: #205"),
        205: create_evidence(205, type_=IssueType.PULL_REQUEST),
    }
    graph_pr = evaluate_family_graph(evidence_pr, 100, "owner/repo")
    assert graph_pr.results[101].is_valid_graph == GraphValidity.INVALID

    # Nonexistent
    evidence_nonexistent = {
        101: create_evidence(101, body="Parent-Issue: #100\nBlocked-By: #205"),
        205: create_evidence(205, nonexistent=True),
    }
    graph_non = evaluate_family_graph(evidence_nonexistent, 100, "owner/repo")
    assert graph_non.results[101].is_valid_graph == GraphValidity.INVALID


def test_as004_invalid_token():
    res = parse_blocked_by_declaration("Blocked-By: #205, owner/other#310", ParentDeclarationStatus.SUPPORTED)
    assert res.status == BlockedByDeclarationStatus.INVALID

    res = parse_blocked_by_declaration("Blocked-By: #205,", ParentDeclarationStatus.SUPPORTED)
    assert res.status == BlockedByDeclarationStatus.INVALID

    res = parse_blocked_by_declaration("Blocked-By: #0", ParentDeclarationStatus.SUPPORTED)
    assert res.status == BlockedByDeclarationStatus.INVALID

    res = parse_blocked_by_declaration("Blocked-By: #205\nBlocked-By: #310", ParentDeclarationStatus.SUPPORTED)
    assert res.status == BlockedByDeclarationStatus.INVALID


def test_as005_cycles():
    evidence = {
        101: create_evidence(101, body="Parent-Issue: #100\nBlocked-By: #205"),
        205: create_evidence(205, body="Parent-Issue: #100\nBlocked-By: #101", state=IssueState.CLOSED),
    }
    graph = evaluate_family_graph(evidence, 100, "owner/repo")
    assert graph.results[101].is_valid_graph == GraphValidity.INVALID
    assert graph.results[205].is_valid_graph == GraphValidity.INVALID

    # Transitive reopen
    evidence2 = {
        310: create_evidence(310, body="Parent-Issue: #100\nBlocked-By: #205"),
        205: create_evidence(205, body="Parent-Issue: #100\nBlocked-By: #101", state=IssueState.CLOSED),
        101: create_evidence(101, body="Parent-Issue: #100", state=IssueState.OPEN),
    }
    graph2 = evaluate_family_graph(evidence2, 100, "owner/repo")
    assert graph2.results[310].satisfaction == DependencySatisfaction.WAITING


def test_as006_absence_of_evidence():
    evidence = {
        101: create_evidence(101, body="Parent-Issue: #100\nBlocked-By: #205"),
        205: create_evidence(205, state=IssueState.UNAVAILABLE),
    }
    graph = evaluate_family_graph(evidence, 100, "owner/repo")
    assert graph.results[101].satisfaction == DependencySatisfaction.UNAVAILABLE


def test_as007_semantic_layer_no_side_effects():
    # Model evaluation should just return results without modifications or calls
    evidence = {
        101: create_evidence(101, body="Parent-Issue: #100\nBlocked-By: #205"),
        205: create_evidence(205, body="Parent-Issue: #100", state=IssueState.CLOSED),
    }
    # Create a copy to ensure no modification
    import copy

    evidence_copy = copy.deepcopy(evidence)
    graph = evaluate_family_graph(evidence, 100, "owner/repo")

    # Assert nothing in evidence changed
    assert evidence == evidence_copy
    assert graph.results[101].satisfaction == DependencySatisfaction.SATISFIED
