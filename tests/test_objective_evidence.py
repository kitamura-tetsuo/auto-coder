"""Objective anchor extraction and production lifecycle regressions."""

import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from auto_coder.decomposition_analyzer import DecompositionAnalysisResult, DecompositionIssue
from auto_coder.decomposition_validation_lifecycle import DecompositionValidationLifecycle
from auto_coder.objective_evidence import ObjectiveAnchorStore, extract_objective
from auto_coder.requirement_contract import build_normative_issue_manifest
from auto_coder.specification_analyzer import SpecificationAnalysisResult, analyze_issue_specification
from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle


def body(objective: str) -> str:
    return f"## Objective\n{objective}\n\n## Requirements\n- REQ-001: Preserve the value."


def issue(number: int, objective: str) -> DecompositionIssue:
    value = body(objective)
    return DecompositionIssue(build_normative_issue_manifest(number, f"Issue {number}", value), value)


def parent_body(objective: str) -> str:
    """A structurally valid tracking-parent body: Objective only, no Requirements."""
    return f"## Objective\n{objective}"


def parent_issue(number: int, objective: str) -> DecompositionIssue:
    value = parent_body(objective)
    return DecompositionIssue(build_normative_issue_manifest(number, f"Issue {number}", value), value)


def test_structural_extraction_preserves_prose_and_rejects_examples_and_duplicates():
    prose = "Use v1.2 for e.g. identifiers。\n  Keep interior spacing."
    assert extract_objective(f"```\n## Objective\nignored\n```\n> ## Objective\nquoted\n- ## Objective\nlisted\n## Objective  \t\n {prose} \r\n## Requirements\nx").text == prose
    assert extract_objective("## Objective\n\n## Requirements\nx").status == "INVALID"
    assert extract_objective("## Objective\none\n# Next\n## Objective\ntwo").status == "INVALID"
    assert extract_objective("~~~md\n## Objective\nexample\n~~~").status == "ABSENT"


def test_individual_production_lifecycle_keeps_original_and_current_separate(tmp_path):
    prompts = []
    first = body("Original purpose")
    edited = body("Replacement purpose")
    manifest = build_normative_issue_manifest(1857, "Anchor", first)

    def analyze(manifest, issue_body):
        return analyze_issue_specification(
            manifest,
            issue_body,
            prompt_runner=lambda prompt: prompts.append(prompt) or '{"verdict":"READY","remediation":"NONE","findings":[]}',
        )

    with patch("auto_coder.specification_validation_lifecycle.analyze_issue_specification", side_effect=analyze):
        first_result = SpecificationValidationLifecycle("owner/repo", "model-a", tmp_path / "decisions.json").decide(manifest, "Anchor", first)
        changed_result = SpecificationValidationLifecycle("owner/repo", "model-b", tmp_path / "decisions.json").decide(build_normative_issue_manifest(1857, "Anchor", edited), "Anchor", edited)

    assert first_result.verdict == "READY"
    assert changed_result.verdict == "BLOCKED"
    assert changed_result.findings[0].category == "objective_conflict"
    assert len(prompts) == 1
    assert "Original purpose" not in json.dumps([{"requirement_id": item.requirement_id, "text": item.text} for item in build_normative_issue_manifest(1857, "Anchor", edited).requirements])


def test_complete_set_captures_each_identity_and_restart_individual_reuses_child_anchor(tmp_path):
    parent, child = parent_issue(100, "Parent purpose"), issue(101, "Child purpose")
    parent_snapshot = {"id": 1000, "number": 100, "title": "Issue 100", "body": parent.body}
    child_snapshot = {"id": 1010, "number": 101, "title": "Issue 101", "body": child.body}
    set_gate = DecompositionValidationLifecycle("owner/repo", "model", tmp_path / "sets.json", lambda _parent, _children: DecompositionAnalysisResult("READY"))
    assert set_gate.decide(set_gate.identity(parent_snapshot, [child_snapshot]), parent, [child]).verdict == "READY"

    edited = body("New child purpose")
    captured = set_gate.objective_store.capture(101, edited, "individual-current-snapshot:v1")
    assert captured.original_text == "Child purpose"
    assert captured.current.text == "New child purpose"
    assert set_gate.objective_store.capture(100, parent.body, "individual-current-snapshot:v1").original_text == "Parent purpose"


def test_complete_set_rejects_parent_or_child_objective_tampering_before_cached_ready(tmp_path):
    parent, child = parent_issue(200, "Coordinate preview without applying it."), issue(201, "Project the draft without applying it.")
    snapshots = lambda p, c: (
        {"id": 2000, "number": 200, "title": "Parent", "body": p.body},
        [{"id": 2010, "number": 201, "title": "Child", "body": c.body}],
    )
    calls = []
    gate = DecompositionValidationLifecycle(
        "owner/repo",
        "model",
        tmp_path / "sets.json",
        lambda _parent, _children: calls.append("model") or DecompositionAnalysisResult("READY"),
    )
    original_snapshots = snapshots(parent, child)
    assert gate.decide(gate.identity(*original_snapshots), parent, [child]).verdict == "READY"

    for changed_parent, changed_child, affected in (
        (parent_issue(200, "Coordinate preview and apply it."), child, 200),
        (parent, issue(201, "Persist and project the draft."), 201),
    ):
        current = snapshots(changed_parent, changed_child)
        result = gate.decide(gate.identity(*current), changed_parent, [changed_child])
        assert result.verdict == "BLOCKED"
        assert result.remediation == "EDIT_IN_PLACE"
        assert result.findings[0].category == "objective_conflict"
        assert result.findings[0].affected_issues[0].issue_number == affected
        assert result.findings[0].affected_issues[0].requirement_ids == ()
    assert calls == ["model"]


def test_legacy_absence_concurrency_and_corrupt_baseline_fail_closed(tmp_path):
    path = tmp_path / "individual_review_history.json"
    legacy = json.dumps({"issue_number": 7, "title": "Legacy", "body": "## Requirements\n- REQ-001: Work.", "requirements": []})
    path.write_text(json.dumps({"7": {"baseline": legacy, "applied_outcomes": []}}))
    store = ObjectiveAnchorStore("owner/repo", path)
    assert store.capture(7, body("Added later"), "individual-current-snapshot:v1").state == "UNANCHORED"

    fresh = ObjectiveAnchorStore("owner/repo", tmp_path / "concurrent.json")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda value: fresh.capture(8, body(value), "current:v1").original_text, ("One", "Two")))
    assert len(set(results)) == 1
    assert ObjectiveAnchorStore("owner/repo", tmp_path / "concurrent.json").capture(8, body("Three"), "current:v1").original_text == results[0]

    path.write_text(json.dumps({"9": {"baseline": "not json", "applied_outcomes": []}}))
    called = []
    gate = SpecificationValidationLifecycle("owner/repo", "model", tmp_path / "decisions.json", lambda *_args: called.append(True) or SpecificationAnalysisResult("READY"))
    decision = gate.decide(build_normative_issue_manifest(9, "Broken", body("Latest")), "Broken", body("Latest"))
    assert decision.verdict == "ERROR"
    assert called == []


def test_production_lifecycle_blocks_anchor_tampering_before_model_or_cached_ready(tmp_path):
    path = tmp_path / "decisions.json"
    original = body("Render a preview without changing live state.")
    calls = []
    gate = SpecificationValidationLifecycle(
        "owner/repo",
        "model",
        path,
        lambda *_args: calls.append("model") or SpecificationAnalysisResult("READY"),
    )
    first_manifest = build_normative_issue_manifest(1858, "Preview", original)
    assert gate.decide(first_manifest, "Preview", original).verdict == "READY"

    variants = (
        body("Render and apply a preview."),
        "## Requirements\n- REQ-001: Preserve the value.",
        "## Objective\n\n## Requirements\n- REQ-001: Preserve the value.",
        original + "\n\n## Objective\nSecond purpose.",
    )
    for changed in variants:
        result = gate.decide(build_normative_issue_manifest(1858, "Preview", changed), "Preview", changed)
        assert result.verdict == "BLOCKED"
        assert result.remediation == "EDIT_IN_PLACE"
        assert len(result.findings) == 1
        finding = result.findings[0]
        assert finding.category == "objective_conflict"
        assert finding.requirement_ids == ()
        assert "Render a preview without changing live state." in finding.explanation
        assert "replacement Issue" in finding.clarification
    assert calls == ["model"]


def test_unanchored_production_issue_remains_reviewable_and_never_adopts_later_objective(tmp_path):
    path = tmp_path / "decisions.json"
    calls = []
    gate = SpecificationValidationLifecycle(
        "owner/repo",
        "model",
        path,
        lambda *_args: calls.append("model") or SpecificationAnalysisResult("READY"),
    )
    legacy = "## Requirements\n- REQ-001: Preserve the value."
    assert gate.decide(build_normative_issue_manifest(12, "Legacy", legacy), "Legacy", legacy).verdict == "READY"
    later = body("A later purpose")
    assert gate.decide(build_normative_issue_manifest(12, "Legacy", later), "Legacy", later).verdict == "READY"
    assert gate.objective_store.capture(12, later, "current:v1").state == "UNANCHORED"
    assert calls == ["model", "model"]
