import hashlib
import json
from pathlib import Path

from src.auto_coder.requirement_contract import build_normative_issue_manifest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "specification_regressions"


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
