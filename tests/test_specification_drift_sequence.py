"""Production-reachable regressions for the historical Issue #1790 drift sequence."""

import json
from pathlib import Path

from auto_coder.requirement_contract import build_normative_issue_manifest
from auto_coder.specification_analyzer import analyze_issue_specification
from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "specification_regressions" / "auto-coder-1790"
TITLE = "Synthetic #1790 specification-drift reconstruction"


class CurrentIssue:
    """Minimal GitHub boundary used by specification remediation application."""

    def __init__(self, number: int, title: str, body: str) -> None:
        self.number = number
        self.title = title
        self.body = body
        self.comments: list[dict[str, str]] = []

    def get_issue_dispatch_snapshot_strict(self, _repository: str, _number: int) -> dict[str, object]:
        return {
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "labels": [{"name": "implementation-ready"}],
        }

    def get_issue_comments_strict(self, _repository: str, _number: int) -> list[dict[str, str]]:
        return list(self.comments)

    def add_comment_to_issue(self, _repository: str, _number: int, body: str) -> None:
        self.comments.append({"body": body})

    def remove_labels(self, _repository: str, _number: int, _labels: list[str], item_type: str) -> None:
        assert item_type == "issue"


def fixture() -> tuple[dict[str, object], dict[str, str]]:
    metadata = json.loads((FIXTURE_ROOT / "metadata.json").read_text(encoding="utf-8"))
    generations = {generation["id"]: (FIXTURE_ROOT / generation["file"]).read_text(encoding="utf-8") for generation in metadata["generations"]}
    return metadata, generations


def response(verdict: str, remediation: str = "NONE", explanation: str = "") -> str:
    findings = []
    if verdict == "BLOCKED":
        findings = [
            {
                "category": "unstated_dependency",
                "requirement_ids": ["REQ-001"],
                "explanation": explanation or "A current material authority is undefined.",
                "clarification": "Define the missing authority at the correct responsibility boundary.",
                "counterexample": "",
                "missing_normative_boundary": "",
            }
        ]
    return json.dumps({"verdict": verdict, "remediation": remediation, "findings": findings})


def submit(gate: SpecificationValidationLifecycle, number: int, body: str):
    manifest = build_normative_issue_manifest(number, TITLE, body)
    assert manifest.explicit_contract_valid
    return gate.decide(manifest, TITLE, body)


def install_model(monkeypatch, runner) -> None:
    """Restore the real analyzer behind the suite-wide no-network test guard."""
    monkeypatch.setattr(
        "auto_coder.specification_validation_lifecycle.analyze_issue_specification",
        lambda manifest, body: analyze_issue_specification(manifest, body, prompt_runner=runner),
    )


def test_1790_fixture_provenance_is_explicitly_synthetic_and_non_normative() -> None:
    """REQ-001/002/003/009: never misrepresent reconstructed prose as GitHub history."""
    metadata, generations = fixture()
    source = metadata["source"]
    assert source == {
        "repository": "kitamura-tetsuo/auto-coder",
        "issue_number": 1790,
        "url": "https://github.com/kitamura-tetsuo/auto-coder/issues/1790",
    }
    assert metadata["normative_status"] == "historical_negative_example_only"
    assert "not an exact GitHub snapshot" in metadata["provenance_note"]
    entries = metadata["generations"]
    assert {comment for entry in entries for comment in entry["source_comment_ids"]} == {
        5558139139,
        5558158773,
        5558178680,
        5558200482,
        5558239835,
        5558260865,
        5558400048,
    }
    assert all(entry["content_kind"] == "synthetic_reconstruction" for entry in entries)
    assert all("Synthetic reconstruction" in generations[entry["id"]] for entry in entries)
    assert entries[0]["responsibilities"] == ["coordination-retirement", "manual-ci-duplicate-dispatch"]
    assert entries[-1]["responsibilities"] == ["supersession-decision"]


def test_cumulative_drift_traverses_history_prompt_parser_and_reissue_application(tmp_path, monkeypatch) -> None:
    """AS-001/004/006: exercise persisted origin through analysis and durable effects."""
    metadata, bodies = fixture()
    prompts: list[str] = []

    def model(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        if "semantic pull-request labels fuzzily" in prompt:
            assert "manual-CI guard" in prompt
            assert "Durable first-review baseline" in prompt
            assert "urgent emergency capacity" in prompt
            assert prompt.count('"verdict": "BLOCKED"') == 2
            return response(
                "BLOCKED",
                "REISSUE_REQUIRED",
                "Label resolution and prompt selection are independently testable semantic layers outside the baseline coordination-retirement responsibility.",
            )
        if "urgent Issue obtains emergency" in prompt or "ownership across label removal" in prompt:
            return response("BLOCKED", "EDIT_IN_PLACE")
        return response("READY")

    install_model(monkeypatch, model)
    path = tmp_path / "validations.json"
    gate = SpecificationValidationLifecycle("kitamura-tetsuo/auto-coder", "eval/model", path)
    assert submit(gate, 1790, bodies["baseline"]).verdict == "READY"

    for generation in ("ownership", "urgent-capacity"):
        decision = submit(gate, 1790, bodies[generation])
        assert decision.remediation == "EDIT_IN_PLACE"
        assert gate.apply_blocked(CurrentIssue(1790, TITLE, bodies[generation]), decision) is None

    # Historical expansion cannot manufacture a blocker in a currently READY contract.
    ready = submit(gate, 1790, bodies["retained-lifecycle"])
    assert (ready.verdict, ready.remediation) == ("READY", "NONE")

    drifted = submit(gate, 1790, bodies["semantic-resolution"])
    assert (drifted.verdict, drifted.remediation) == (
        metadata["oracle"]["verdict"],
        metadata["oracle"]["remediation"],
    )
    github = CurrentIssue(1790, TITLE, bodies["semantic-resolution"])
    assert gate.apply_blocked(github, drifted) is None
    assert gate.is_reissue_required(1790)
    assert "responsibility" in github.comments[0]["body"]


def test_delta_only_reviewer_is_rejected_by_sequence_oracle() -> None:
    """AS-002: a newest-delta-only disposition cannot satisfy the fixture oracle."""
    metadata, bodies = fixture()

    def delta_only_reviewer(_newest_body: str) -> tuple[str, str]:
        return "BLOCKED", "EDIT_IN_PLACE"

    dispositions = [delta_only_reviewer(bodies[item["id"]]) for item in metadata["generations"][1:6]]
    assert all(disposition == ("BLOCKED", "EDIT_IN_PLACE") for disposition in dispositions)
    assert dispositions[3] != (metadata["oracle"]["verdict"], metadata["oracle"]["remediation"])


def test_fourth_blocked_generation_trips_circuit_breaker_but_ready_can_converge(tmp_path, monkeypatch) -> None:
    """AS-003/004: four parsed analyzer results traverse the production lifecycle."""
    _, bodies = fixture()
    install_model(monkeypatch, lambda *_args, **_kwargs: response("BLOCKED", "EDIT_IN_PLACE"))
    path = tmp_path / "validations.json"
    for index, generation in enumerate(("baseline", "ownership", "urgent-capacity")):
        gate = SpecificationValidationLifecycle("kitamura-tetsuo/auto-coder", f"model-{index}", path)
        decision = submit(gate, 1790, bodies[generation])
        gate.apply_blocked(CurrentIssue(1790, TITLE, bodies[generation]), decision)
        assert gate.store.get(decision.identity).remediation == "EDIT_IN_PLACE"

    # The next generation remains eligible to become READY even after three repairs.
    install_model(monkeypatch, lambda *_args, **_kwargs: response("READY"))
    ready_gate = SpecificationValidationLifecycle("kitamura-tetsuo/auto-coder", "ready-model", path)
    assert submit(ready_gate, 1790, bodies["retained-lifecycle"]).remediation == "NONE"

    install_model(monkeypatch, lambda *_args, **_kwargs: response("BLOCKED", "EDIT_IN_PLACE"))
    blocked_gate = SpecificationValidationLifecycle("kitamura-tetsuo/auto-coder", "fourth-model", path)
    fourth = submit(blocked_gate, 1790, bodies["semantic-resolution"])
    blocked_gate.apply_blocked(CurrentIssue(1790, TITLE, bodies["semantic-resolution"]), fourth)
    applied = blocked_gate.store.get(fourth.identity)
    assert applied is not None
    assert applied.remediation == "REISSUE_REQUIRED"
    assert applied.remediation_reason == "repair_round_limit_exhausted(limit=3,previously_applied_edit_in_place_rounds=3)"


def test_coherent_lifecycle_clarifications_remain_editable_below_boundary(tmp_path, monkeypatch) -> None:
    """AS-005: revision count alone is not the scope-drift oracle."""
    install_model(monkeypatch, lambda *_args, **_kwargs: response("BLOCKED", "EDIT_IN_PLACE"))
    gate = SpecificationValidationLifecycle("owner/control", "model", tmp_path / "validations.json")
    for revision in range(3):
        body = "## Requirements\n\n" "REQ-001: Preserve one job lifecycle from accepted through running to terminal state" f", with clarification revision {revision}.\n"
        decision = submit(gate, 42, body)
        gate.apply_blocked(CurrentIssue(42, TITLE, body), decision)
        applied = gate.store.get(decision.identity)
        assert applied is not None and applied.remediation == "EDIT_IN_PLACE"
        assert not gate.is_reissue_required(42)
