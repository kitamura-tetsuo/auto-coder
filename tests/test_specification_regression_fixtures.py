import hashlib
import json
from pathlib import Path

import pytest

from src.auto_coder.objective_evidence import ObjectiveAnchor, extract_objective
from src.auto_coder.requirement_contract import build_normative_issue_manifest
from src.auto_coder.specification_analyzer import IndividualRelationshipContext, IndividualReviewEvidence
from src.auto_coder.specification_analyzer import analyze_issue_specification as _analyze_issue_specification

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "specification_regressions"


def analyze_issue_specification(manifest, body, **kwargs):
    current = extract_objective(body)
    state = "ANCHORED" if current.status == "PRESENT" else "UNANCHORED"
    kwargs["review_evidence"] = IndividualReviewEvidence("{}", objective=ObjectiveAnchor(manifest.issue_number, state, current.text, "fixture:v1", current))
    return _analyze_issue_specification(manifest, body, **kwargs)


def test_outliner_5290_fixture_preserves_provenance_and_review_oracle() -> None:
    metadata = json.loads((FIXTURE_DIR / "outliner-5290.json").read_text(encoding="utf-8"))
    body = (FIXTURE_DIR / "outliner-5290.md").read_text(encoding="utf-8")

    source = metadata["source"]
    assert source["repository"] == "kitamura-tetsuo/outliner"
    assert source["issue_number"] == 5290
    assert source["url"] == "https://github.com/kitamura-tetsuo/outliner/issues/5290"
    assert hashlib.sha256(body.encode("utf-8")).hexdigest() == source["body_sha256"]

    manifest = build_normative_issue_manifest(source["issue_number"], source["title"], body)
    assert manifest.explicit_contract_present is True
    assert manifest.explicit_contract_valid is True
    assert [requirement.requirement_id for requirement in manifest.requirements] == [
        "REQ-001",
        "REQ-002",
        "REQ-003",
        "REQ-004",
        "REQ-005",
        "REQ-006",
        "REQ-007",
        "REQ-008",
        "REQ-009",
        "REQ-010",
    ]

    oracle = metadata["expected_specification_review"]
    assert oracle["verdict"] == "BLOCKED"
    assert oracle["required_requirement_ids"] == ["REQ-002", "REQ-010"]
    assert set(oracle["acceptable_categories"]) == {
        "unstated_dependency",
        "unverifiable_requirement",
        "material_ambiguity",
    }
    assert oracle["gap_kind"] == "undefined_authoritative_permission_model"

    requirements = {requirement.requirement_id: requirement.text for requirement in manifest.requirements}
    assert "existing Schedule write permission" in requirements["REQ-002"]
    assert "existing authorization rules" in requirements["REQ-010"]
    assert "Given a viewer without Schedule write permission" in body


def test_outliner_5290_complete_fixture_reaches_strengthened_review_policy() -> None:
    metadata = json.loads((FIXTURE_DIR / "outliner-5290.json").read_text(encoding="utf-8"))
    body = (FIXTURE_DIR / "outliner-5290.md").read_text(encoding="utf-8")
    source = metadata["source"]
    oracle = metadata["expected_specification_review"]
    manifest = build_normative_issue_manifest(source["issue_number"], source["title"], body)

    def review_at_provider_boundary(prompt: str) -> str:
        # The supported analyzer boundary must preserve the complete historical
        # evidence and explicitly instruct every provider about this defect class.
        assert body in prompt
        assert "existing authorization rules" in prompt
        assert "readable-but-non-writable principal" in prompt
        assert "none can convert the contract to READY" in prompt
        return json.dumps(
            {
                "verdict": oracle["verdict"],
                "remediation": "EDIT_IN_PLACE",
                "findings": [
                    {
                        "category": "unstated_dependency",
                        "requirement_ids": oracle["required_requirement_ids"],
                        "explanation": (f"{oracle['missing_normative_boundary']} " f"{oracle['counterexample_anchor']}"),
                        "clarification": ("Define the authoritative Schedule read/write permission semantics " "and establish how the AS-009 read-only principal is reachable."),
                        "counterexample": "",
                        "missing_normative_boundary": "",
                    }
                ],
            }
        )

    result = analyze_issue_specification(manifest, body, prompt_runner=review_at_provider_boundary)

    assert result.verdict == "BLOCKED"
    assert len(result.findings) == 1
    assert result.findings[0].requirement_ids == ("REQ-002", "REQ-010")
    assert oracle["missing_normative_boundary"] in result.findings[0].explanation
    assert "AS-009" in result.findings[0].explanation


def _issue_1774_fixture():
    metadata = json.loads((FIXTURE_DIR / "auto-coder-1774-pre-second-fix.json").read_text(encoding="utf-8"))
    body = (FIXTURE_DIR / "auto-coder-1774-pre-second-fix.md").read_text(encoding="utf-8")
    return metadata, body


def _assert_1774_semantic_oracle(result, oracle) -> None:
    """Require both defect boundaries without coupling the fixture to model prose."""
    assert result.verdict == oracle["verdict"]
    missing = []
    for expected in oracle["required_defects"]:
        covered = False
        for finding in result.findings:
            searchable = " ".join(
                (
                    finding.explanation,
                    finding.clarification,
                    finding.counterexample,
                    finding.missing_normative_boundary,
                )
            ).casefold()
            if finding.category in expected["acceptable_categories"] and set(finding.requirement_ids).intersection(expected["requirement_ids_any"]) and all(any(term.casefold() in searchable for term in alternatives) for alternatives in expected["semantic_term_groups"]):
                covered = True
                break
        if not covered:
            missing.append(expected["key"])
    assert missing == [], f"Missing required semantic defect coverage: {missing}"


def _issue_1774_finding(category, requirement_ids, explanation, clarification):
    return {
        "category": category,
        "requirement_ids": requirement_ids,
        "explanation": explanation,
        "clarification": clarification,
        "counterexample": ("A superseded individual result can still alter current readiness." if category == "false_success_gap" else ""),
        "missing_normative_boundary": ("No authority check covers a stale result after a title/body edit." if category == "false_success_gap" else ""),
    }


def test_auto_coder_1774_child_review_reports_both_defects_in_one_production_path_pass() -> None:
    metadata, body = _issue_1774_fixture()
    source = metadata["source"]
    oracle = metadata["expected_specification_review"]
    relationship = metadata["authoritative_relationship_context"]

    assert hashlib.sha256(body.encode("utf-8")).hexdigest() == source["body_sha256"]
    assert "REQ-015" not in body
    assert "every direct child's current individual specification generation" not in body
    assert "direct-child title/body change must create a new decomposition" not in body

    manifest = build_normative_issue_manifest(source["issue_number"], source["title"], body)
    assert manifest.explicit_contract_valid is True
    assert [item.requirement_id for item in manifest.requirements] == [f"REQ-{number:03d}" for number in range(1, 15)]

    invocations = []

    def deterministic_prompt_provider(prompt: str) -> str:
        invocations.append(prompt)
        assert body in prompt
        assert "Authoritative relationship role selected by the caller after reconciliation: child" in prompt
        assert relationship["related_contracts"] in prompt
        assert "perform an ambiguity-closure pass" in prompt
        assert "perform a next-review-prediction pass" in prompt
        assert "child specification mutation can materially change whether parent/set evidence remains valid" in prompt
        return json.dumps(
            {
                "verdict": "BLOCKED",
                "remediation": "EDIT_IN_PLACE",
                "findings": [
                    _issue_1774_finding(
                        "false_success_gap",
                        ["REQ-003", "REQ-011", "REQ-012"],
                        "A stale individual validation completion after a title/body edit can retain current readiness or authorize implementation.",
                        "Require completion authority to match the current individual text identity.",
                    ),
                    _issue_1774_finding(
                        "material_ambiguity",
                        ["REQ-003", "REQ-010", "REQ-011"],
                        "It is undefined whether a direct-child title/body content mutation supersedes the parent decomposition validation identity or permits evidence reuse.",
                        "Specify child-to-parent invalidation for decomposition evidence.",
                    ),
                ],
            }
        )

    result = analyze_issue_specification(
        manifest,
        body,
        relationship_context=IndividualRelationshipContext(
            role=relationship["role"],
            related_contracts=relationship["related_contracts"],
        ),
        prompt_runner=deterministic_prompt_provider,
    )

    assert len(invocations) == 1
    _assert_1774_semantic_oracle(result, oracle)


def test_auto_coder_1774_blocked_with_only_stale_completion_is_regression_failure() -> None:
    metadata, body = _issue_1774_fixture()
    source = metadata["source"]
    manifest = build_normative_issue_manifest(source["issue_number"], source["title"], body)
    response = json.dumps(
        {
            "verdict": "BLOCKED",
            "remediation": "EDIT_IN_PLACE",
            "findings": [
                _issue_1774_finding(
                    "false_success_gap",
                    ["REQ-003", "REQ-011"],
                    "A superseded individual result after a title/body edit can retain current state authority.",
                    "Reject each stale validation completion before it can authorize implementation.",
                )
            ],
        }
    )
    result = analyze_issue_specification(manifest, body, prompt_runner=lambda _prompt: response)

    with pytest.raises(AssertionError, match="child_edit_decomposition_identity_boundary"):
        _assert_1774_semantic_oracle(result, metadata["expected_specification_review"])


def test_auto_coder_1774_oracle_accepts_equivalent_wording_and_order() -> None:
    metadata, body = _issue_1774_fixture()
    source = metadata["source"]
    manifest = build_normative_issue_manifest(source["issue_number"], source["title"], body)
    response = json.dumps(
        {
            "verdict": "BLOCKED",
            "remediation": "EDIT_IN_PLACE",
            "findings": [
                _issue_1774_finding(
                    "unverifiable_requirement",
                    ["REQ-010", "REQ-012"],
                    "The contract is unclear about decomposition evidence reuse after a child specification edit.",
                    "Define whether that content mutation changes the set identity.",
                ),
                _issue_1774_finding(
                    "material_ambiguity",
                    ["REQ-003", "REQ-011"],
                    "A late result for individual text edit validation has unspecified current state authority.",
                    "Define the completion guard for the superseded generation.",
                ),
            ],
        }
    )
    result = analyze_issue_specification(manifest, body, prompt_runner=lambda _prompt: response)

    _assert_1774_semantic_oracle(result, metadata["expected_specification_review"])
