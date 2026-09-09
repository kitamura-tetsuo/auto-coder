"""Regression coverage for the role-aware structural assessment boundary."""

import pytest

from auto_coder.requirement_contract import build_normative_issue_manifest
from auto_coder.role_structural_assessment import (
    REASON_CHILD_REQUIREMENTS_INVALID,
    REASON_PARENT_OBJECTIVE_REQUIRED,
    REASON_PARENT_REQUIREMENTS_FORBIDDEN,
    ROLE_IMPLEMENTATION_CHILD,
    ROLE_TRACKING_PARENT,
    assess_role_structure,
)


def manifest(issue_number, title, body):
    return build_normative_issue_manifest(issue_number, title, body)


def assess(issue_number, title, body, role):
    return assess_role_structure(manifest(issue_number, title, body), body, role)


# AS-001 — a real contract-free parent is structurally valid.


@pytest.mark.parametrize(
    "objective",
    [
        "Coordinate the decomposition without owning any implementation contract.",
        "パーサーの役割を分割し、実装契約を持たないことを保証する。",
        "Coordinate a multi-stage decomposition across several dependent implementation children, " "preserving each child's independent Requirements contract while this parent itself intentionally " "carries no Requirements of its own, only Context and a Child Issues checklist.",
    ],
)
def test_contract_free_parent_with_objective_context_and_checklist_is_valid(objective):
    body = f"## Objective\n{objective}\n\n## Context\nBackground only.\n\n## Child Issues\n- [ ] #101\n- [ ] #102\n"

    result = assess(1951, "Tracking parent", body, ROLE_TRACKING_PARENT)

    assert result.status == "VALID"
    assert result.issue_number == 1951
    assert result.role == ROLE_TRACKING_PARENT
    assert result.defects == ()
    assert result.error is None


# AS-002 — optionality and misplaced declarations cannot bypass the ban.


@pytest.mark.parametrize(
    "body",
    [
        "## Objective\nCoordinate work.\n\n## Requirements\n",
        "## Objective\nCoordinate work.\n\n## Requirements\nREQ-001: A real declaration.\n",
        "## Objective\nCoordinate work.\n\n### requirements ###\n",
        "## Objective\nCoordinate work.\n\n## Context\nREQ-001: Misplaced under Context.\n",
        "## Objective\nCoordinate work.\n\n## Context\n- REQ-001: Bulleted.\n",
        "## Objective\nCoordinate work.\n\n## Context\n1. REQ-001: Numbered.\n",
        "## Objective\nCoordinate work.\n\n## Context\n- [ ] REQ-001: Checkbox.\n",
        "## Objective\nCoordinate work.\n\n## Context\n`REQ-001:` Backticked.\n",
        "## Objective\nCoordinate work.\n\n## Context\nREQ-001:\n",
    ],
)
def test_forbidden_requirements_heading_and_declaration_forms_are_blocked(body):
    result = assess(1951, "Tracking parent", body, ROLE_TRACKING_PARENT)

    assert result.status == "BLOCKED"
    assert any(defect.reason == REASON_PARENT_REQUIREMENTS_FORBIDDEN for defect in result.defects)


def test_renaming_the_section_alone_does_not_rescue_a_remaining_declaration():
    body = "## Objective\nCoordinate work.\n\n### requirements ###\nREQ-001: Still a declaration.\n"

    result = assess(1951, "Tracking parent", body, ROLE_TRACKING_PARENT)

    defect = next(d for d in result.defects if d.reason == REASON_PARENT_REQUIREMENTS_FORBIDDEN)
    assert len(defect.locations) == 2


# AS-003 — documentation references and examples are not implementation declarations.


def test_references_and_excluded_examples_do_not_trigger_the_ban_but_content_after_them_is_inspected():
    clean_body = (
        "## Objective\nCoordinate work.\n\n"
        "## Child Issues\n- [ ] #123/REQ-001 tracked here\n"
        "Discussion mentions Requirements informally without a heading.\n"
        "```\n## Requirements\nREQ-001: fenced example, ignored.\n```\n"
        "> ## Requirements\n> REQ-001: blockquoted example, ignored.\n"
        "    ## Requirements\n    REQ-001: indented example, ignored.\n"
        "<!-- ## Requirements\nREQ-001: commented example, ignored. -->\n"
    )
    clean_result = assess(1951, "Tracking parent", clean_body, ROLE_TRACKING_PARENT)
    assert clean_result.status == "VALID"

    for trailer in ("## Requirements\nREQ-001: Real heading after examples.\n", "REQ-002: Real declaration after examples.\n"):
        result = assess(1951, "Tracking parent", clean_body + trailer, ROLE_TRACKING_PARENT)
        assert result.status == "BLOCKED"
        assert any(defect.reason == REASON_PARENT_REQUIREMENTS_FORBIDDEN for defect in result.defects)


# AS-004 — role is not guessed from convenient text.


def _valid_child_body():
    return "## Objective\nImplement the behavior.\n\n## Requirements\nREQ-001: Preserve the value.\n"


def test_valid_child_body_is_permitted_as_child_but_forbidden_as_parent():
    body = _valid_child_body()

    child_result = assess(101, "Implementation child", body, ROLE_IMPLEMENTATION_CHILD)
    assert child_result.status == "VALID"

    parent_result = assess(101, "Implementation child", body, ROLE_TRACKING_PARENT)
    assert parent_result.status == "BLOCKED"
    assert any(defect.reason == REASON_PARENT_REQUIREMENTS_FORBIDDEN for defect in parent_result.defects)


def test_title_alone_never_exempts_a_child_from_its_requirements_contract():
    result = assess(101, "Tracking", "## Objective\nNo contract here.\n", ROLE_IMPLEMENTATION_CHILD)

    assert result.status == "BLOCKED"
    assert result.defects[0].reason == REASON_CHILD_REQUIREMENTS_INVALID


@pytest.mark.parametrize("role", [None, "", "parent", "child", "tracking-parent", 42])
def test_unknown_or_contradictory_role_is_an_assessment_error_not_a_chosen_role(role):
    body = _valid_child_body()

    result = assess_role_structure(manifest(101, "Ambiguous", body), body, role)

    assert result.status == "ERROR"
    assert result.error is not None
    assert result.defects == ()


# AS-005 — deterministic omissions are not retryable infrastructure failures.


@pytest.mark.parametrize(
    "body",
    [
        "## Context\nNo Objective heading at all.\n",
        "## Objective\nFirst.\n\n## Objective\nSecond.\n",
        "## Objective\n\n## Context\nEmpty Objective above.\n",
    ],
)
def test_parent_objective_omissions_are_blocked_with_stable_reason(body):
    result = assess(1951, "Tracking parent", body, ROLE_TRACKING_PARENT)

    assert result.status == "BLOCKED"
    assert result.defects[0].reason == REASON_PARENT_OBJECTIVE_REQUIRED


@pytest.mark.parametrize(
    "body",
    [
        "## Objective\nImplement it.\n",
        "## Objective\nImplement it.\n\n## Requirements\n",
        "## Objective\nImplement it.\n\n## Requirements\nREQ-001: One.\nREQ-001: Two.\n",
        "## Objective\nImplement it.\n\n## Requirements\nREQ-001: One.\nnot an entry\n",
    ],
)
def test_child_requirement_omissions_are_blocked_with_stable_reason(body):
    result = assess(101, "Implementation child", body, ROLE_IMPLEMENTATION_CHILD)

    assert result.status == "BLOCKED"
    assert result.defects[0].reason == REASON_CHILD_REQUIREMENTS_INVALID


def test_child_valid_contract_is_the_positive_control():
    result = assess(101, "Implementation child", _valid_child_body(), ROLE_IMPLEMENTATION_CHILD)

    assert result.status == "VALID"
    assert result.defects == ()


def test_parent_missing_objective_and_containing_forbidden_contract_exposes_both_defects():
    body = "## Context\nNo Objective.\n\n## Requirements\nREQ-001: Forbidden on a parent.\n"

    result = assess(1951, "Tracking parent", body, ROLE_TRACKING_PARENT)

    reasons = {defect.reason for defect in result.defects}
    assert reasons == {REASON_PARENT_REQUIREMENTS_FORBIDDEN, REASON_PARENT_OBJECTIVE_REQUIRED}


# AS-006 — no repair-by-mutation or hidden inheritance.


def test_legacy_parent_with_requirements_and_bare_child_are_not_mutated_or_cross_inherited():
    parent_body = "## Objective\nLegacy purpose.\n\n## Requirements\nREQ-001: Legacy parent contract.\n"
    child_body = "## Objective\nImplement the legacy behavior.\n\n" "## Acceptance Scenarios\n### AS-001\nDetailed acceptance examples but no Requirements section.\n"
    parent_manifest, child_manifest = manifest(1950, "Legacy parent", parent_body), manifest(101, "Bare child", child_body)
    parent_snapshot, child_snapshot = str(parent_body), str(child_body)

    parent_result = assess_role_structure(parent_manifest, parent_body, ROLE_TRACKING_PARENT)
    child_result = assess_role_structure(child_manifest, child_body, ROLE_IMPLEMENTATION_CHILD)

    assert parent_body == parent_snapshot
    assert child_body == child_snapshot
    assert parent_result.status == "BLOCKED"
    assert parent_result.defects[0].reason == REASON_PARENT_REQUIREMENTS_FORBIDDEN
    assert child_result.status == "BLOCKED"
    assert child_result.defects[0].reason == REASON_CHILD_REQUIREMENTS_INVALID
    assert child_manifest.requirements == ()


# REQ-001 — malformed required input.


def test_non_manifest_or_non_string_body_is_an_assessment_error():
    assert assess_role_structure(None, "body", ROLE_TRACKING_PARENT).status == "ERROR"
    assert assess_role_structure(manifest(1, "T", "body"), None, ROLE_TRACKING_PARENT).status == "ERROR"
