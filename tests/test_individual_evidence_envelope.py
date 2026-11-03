"""Deterministic integrity/orchestration checks, not semantic model verdicts."""

import hashlib
import json
import runpy
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

from auto_coder.individual_evidence_envelope import decode_individual_evidence, encode_individual_evidence
from auto_coder.objective_evidence import ObjectiveAnchor, ObjectiveExtraction, objective_evidence_json
from auto_coder.prompt_loader import load_prompts, render_prompt
from auto_coder.requirement_contract import NormativeIssueManifest, NormativeRequirement
from auto_coder.specification_analyzer import IncrementalReviewContext, IndividualRelationshipContext, IndividualReviewEvidence, SpecificationAnalysisResult, analyze_issue_specification
from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle, validation_policy_identity
from tests.support.individual_review_evidence import prompt_evidence
from tests.test_issue_review_service import FakeGitHub, engine_with_lane


def test_contained_normative_atoms_are_interned_once_in_actual_request(tmp_path, monkeypatch):
    body = "## Requirements\nREQ-001: Keep state and log.\nREQ-002: Keep state\n\n## Context\nKeep state and log. Keep state"
    from auto_coder.requirement_contract import build_normative_issue_manifest

    manifest = build_normative_issue_manifest(7, "Contained atoms", body)
    captured = []

    def transport(prompt):
        captured.append(prompt)
        return json.dumps({"verdict": "READY", "remediation": "NONE", "findings": [], "finding_dispositions": [], "coverage": [{"boundary": boundary, "status": "FRESH", "no_impact_reason": ""} for boundary in ("REQ-001", "REQ-002", "contract-wide")]})

    monkeypatch.setattr("auto_coder.specification_validation_lifecycle.analyze_issue_specification", lambda current, text: analyze_issue_specification(current, text, prompt_runner=transport))
    gate = SpecificationValidationLifecycle("owner/repo", "configured-route", tmp_path / "decisions.json")
    decision = gate.decide(manifest, "Contained atoms", body)

    assert decision.verdict == "READY"
    wire = captured[0].rsplit("```json\n", 1)[1].split("\n```", 1)[0]
    assert wire.count("Keep state") == 1
    inputs = prompt_evidence(captured[0])
    assert inputs["current"]["body"] == body
    assert inputs["current"]["manifest"]["requirements"] == ({"requirement_id": "REQ-001", "text": "Keep state and log."}, {"requirement_id": "REQ-002", "text": "Keep state"})


@pytest.mark.parametrize("role", ["standalone", "child"])
def test_review_lane_reconstructs_real_persisted_resolved_and_closed_family_evidence(tmp_path, monkeypatch, role):
    """Transport oracle starts at the real Review lane, not a supplied predecessor."""
    target = 2
    parents = {target: 1, **{number: 1 for number in range(100, 125)}} if role == "child" else {}
    github = FakeGitHub([], parents=parents)
    body = "## Objective\nPreserve the original contract.\n\n## Requirements\nREQ-001: Keep the caller's exact authority.\n\n## Context\n" + "Retained current evidence 日本語.\r\n" * 100
    github.issues[target] = github.snapshot(target, body=body)
    if role == "child":
        github.issues[1] = github.snapshot(1, title="Parent", body="## Objective\nCoordinate the bounded family.")
        for number in range(100, 125):
            github.issues[number] = github.snapshot(number, title=f"Closed sibling {number}", state="closed", body=f"## Requirements\nREQ-001: Preserve member {number}.\n\n## Context\n" + f"Complete member {number} evidence 日本語.\r\n" * 100)
    captured = []

    def model_transport(prompt):
        inputs = prompt_evidence(prompt)
        captured.append((prompt, inputs))
        predecessor = json.loads(inputs["incremental"]["predecessor_evidence"]) if inputs["incremental"]["predecessor_evidence"] else None
        first = len(captured) == 1
        finding = {"category": "material_ambiguity", "requirement_ids": ["REQ-001"], "explanation": "A source is undefined.", "clarification": "Define the source.", "counterexample": "", "missing_normative_boundary": ""}
        return json.dumps(
            {
                "verdict": "BLOCKED" if first else "READY",
                "remediation": "EDIT_IN_PLACE" if first else "NONE",
                "findings": [finding] if first else [],
                "finding_dispositions": [{"finding_id": entry["finding_id"], "disposition": "RESOLVED", "reasoning": "The exact original counterexample is resolved."} for entry in predecessor["findings"]] if predecessor else [],
                "coverage": [{"boundary": boundary, "status": "FRESH", "no_impact_reason": ""} for boundary in ("REQ-001", "contract-wide")],
            }
        )

    # Restore the real assembly/parser behind the suite's semantic-call guard;
    # only the model transport is controlled, with real lifecycle contextvars.
    monkeypatch.setattr("auto_coder.specification_validation_lifecycle.analyze_issue_specification", lambda manifest, current_body: analyze_issue_specification(manifest, current_body, prompt_runner=model_transport))
    engine = engine_with_lane(tmp_path, github, None, monkeypatch=monkeypatch)
    service = engine._get_review_service("owner/repo")
    gate = engine._get_specification_validator("owner/repo")
    decisions = []
    try:
        for phase in range(3):
            if phase == 1:
                github.issues[target]["body"] += "\nThe original source is now explicit."
            elif phase == 2:
                github.issues[100 if role == "child" else target]["body"] += "\nChanged closed-sibling evidence." if role == "child" else "\nA later non-normative edit."
            github.issues[target]["labels"] = [{"name": "implementation-ready"}]
            outcome = service.pump_target(target, "lossless-production-boundary")
            assert outcome is not None and outcome.status == "completed"
            selected = [decision for decision in outcome.decisions.values() if hasattr(decision.identity, "issue_number") and decision.identity.issue_number == target]
            assert len(selected) == 1
            decisions.append(selected[0])
            assert len(captured) == phase + 1
            _, inputs = captured[-1]
            current = inputs["current"]
            stored = json.loads(gate.store.get(selected[0].identity).review_inputs)
            assert current["body"] == github.issues[target]["body"] == stored["body"]
            assert current["manifest"]["requirements"] == ({"requirement_id": "REQ-001", "text": "Keep the caller's exact authority."},)
            assert current["title"] == stored["title"] == "Title"
            assert current["relationship"]["role"] == role
            assert current["baseline"] == stored["baseline"]
            assert current["objective"] == stored["objective"]
            assert current["prior_applied_outcomes"] == tuple(stored["prior_applied_outcomes"])
            assert inputs["incremental"]["mode"] == ("FULL" if phase == 0 else "INCREMENTAL")
            if role == "child":
                related = json.loads(current["relationship"]["related_contracts"])
                assert {entry["issue_number"] for entry in related} == {1, *range(100, 125)}
                for entry in related:
                    observed = github.issues[entry["issue_number"]]
                    assert entry["body_non_normative_evidence"] == observed["body"]
                    assert entry["title"] == observed["title"]
                    wire = captured[-1][0].rsplit("```json\n", 1)[1].split("\n```", 1)[0]
                    for requirement in entry["normative_manifest"]:
                        assert wire.count(requirement["text"]) == 1
            if phase:
                previous = gate.store.get(decisions[phase - 1].identity)
                predecessor = json.loads(inputs["incremental"]["predecessor_evidence"])
                assert inputs["incremental"]["predecessor_decision_key"] == predecessor["decision_key"] == previous.identity.key
                assert predecessor["accepted_sequence"] == previous.accepted_sequence
                assert predecessor["review_inputs"] == json.loads(previous.review_inputs)
                assert predecessor["findings"][0]["finding_id"] == decisions[0].finding_ids[0]
                if phase == 2:
                    assert predecessor["findings"][0]["last_disposition"] == "RESOLVED"
                    delta = json.loads(inputs["incremental"]["assessed_delta"])
                    field = "related_contracts" if role == "child" else "body"
                    assert delta[field]["before"] == json.loads(previous.review_inputs)[field]
                    assert delta[field]["after"] == stored[field]
        assert decisions[-1].verdict == "READY"
    finally:
        engine.review_scheduler.shutdown()


def test_independently_authored_wire_reconstructs_exact_typed_ordered_inputs():
    # The expected inputs and wire tree are authored independently of encoder
    # output. In particular tuples, CRLF/Unicode and reference-like data matter.
    expected = {"manifest": ("REQ-001", "保存\r\n"), "empty": "", "none": None, "list": [], "literal": "ie2:not-this-envelope:9", "flag": False}
    plain = ["m", [["manifest", ["t", [["r", "REQ-001"], ["r", "保存\r\n"]]]], ["empty", ["r", ""]], ["none", ["v", None]], ["list", ["l", []]], ["literal", ["r", "ie2:not-this-envelope:9"]], ["flag", ["v", False]]]]
    identity = hashlib.sha256(json.dumps(plain, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    strings = ["manifest", "REQ-001", "保存\r\n", "empty", "", "none", "list", "literal", "ie2:not-this-envelope:9", "flag"]
    refs = [f"ie1:{identity}:{i}" for i in range(len(strings))]
    wire = {"version": 1, "identity": identity, "definitions": [["s", text] for text in strings], "root": ["m", [[refs[0], ["t", [["r", refs[1]], ["r", refs[2]]]]], [refs[3], ["r", refs[4]]], [refs[5], ["v", None]], [refs[6], ["l", []]], [refs[7], ["r", refs[8]]], [refs[9], ["v", False]]]]}
    actual = decode_individual_evidence(json.dumps(wire))
    assert actual == expected
    assert list(actual) == ["manifest", "empty", "none", "list", "literal", "flag"]
    assert "absent" not in actual
    assert type(actual["manifest"]) is tuple
    assert type(actual["list"]) is list
    assert actual["manifest"][1].encode("utf-8") == b"\xe4\xbf\x9d\xe5\xad\x98\r\n"
    assert decode_individual_evidence(encode_individual_evidence(expected, ("保存\r\n",))) == expected


@pytest.mark.parametrize("corruption", ["version", "bool-version", "scope", "dangling", "cycle", "duplicate-content", "unknown-tag", "unused", "literal-change", "duplicate-key"])
def test_invalid_envelopes_never_return_partial_authoritative_input(corruption):
    payload = encode_individual_evidence({"manifest": "Keep state.", "body": "REQ-001: Keep state."}, ("Keep state.",))
    wire = json.loads(payload)
    if corruption == "version":
        wire["version"] = 2
    elif corruption == "bool-version":
        wire["version"] = True
    elif corruption == "scope":
        wire["root"][1][0][0] = "ie1:" + "0" * 64 + ":0"
    elif corruption == "dangling":
        wire["root"][1][0][0] = f'ie1:{wire["identity"]}:999999'
    elif corruption == "cycle":
        wire["definitions"][0] = ["c", [f'ie1:{wire["identity"]}:0']]
    elif corruption == "duplicate-content":
        wire["definitions"][1] = wire["definitions"][0]
    elif corruption == "unknown-tag":
        wire["root"][0] = "fetch-external"
    elif corruption == "unused":
        wire["definitions"].append(["s", "Unreferenced authority"])
    elif corruption == "literal-change":
        wire["definitions"][0][1] = "Different authority"
    encoded = json.dumps(wire)
    if corruption == "duplicate-key":
        encoded = encoded.replace('"version": 1', '"version": 1, "version": 1', 1)
    with pytest.raises(ValueError):
        decode_individual_evidence(encoded)


def test_original_semantic_template_is_preserved():
    # Captured from main90 before adding this transport representation.
    prompt = load_prompts()["issue"]["adversarial_specification_analysis"]
    assert hashlib.sha256(prompt.encode()).hexdigest() == "60e7f4a89994f93b47597fa070001758fae757e21728139f5dd87d453d5e99cf"


@pytest.mark.parametrize("template", ["individual_evidence_field_reference", "individual_evidence_envelope", "header"])
def test_receiving_instruction_change_invalidates_exact_reuse_and_predecessor(tmp_path, monkeypatch, template):
    manifest = NormativeIssueManifest(7, "Current", True, True, (NormativeRequirement("REQ-001", "Preserve state."),))
    modes = []

    def analyzer(_manifest, _body):
        modes.append("called")
        return SpecificationAnalysisResult("READY")

    gate = SpecificationValidationLifecycle("owner/repo", "unchanged-route", tmp_path / "decisions.json", analyzer)
    original = gate.decide(manifest, "Current", "REQ-001: Preserve state.")
    gate.decide(manifest, "Current", "REQ-001: Preserve state.")
    assert modes == ["called"]
    assert original.review_mode == "FULL"
    changed = deepcopy(load_prompts())
    if template == "header":
        changed["header"] += "\nA decision-affecting instruction changed."
    else:
        changed["issue"][template] += "\nA decision-affecting instruction changed."
    monkeypatch.setattr("auto_coder.specification_validation_lifecycle.load_prompts", lambda: changed)
    assert validation_policy_identity() != original.identity.policy_identity
    gate = SpecificationValidationLifecycle("owner/repo", "unchanged-route", tmp_path / "decisions.json", analyzer)
    current = gate.decide(manifest, "Current", "REQ-001: Preserve state.")
    assert current.identity.policy_identity != original.identity.policy_identity
    assert modes == ["called", "called"]
    assert current.review_mode == "FULL"
    edited = gate.decide(manifest, "Current", "REQ-001: Preserve state.\nUpdated context.")
    assert edited.review_mode == "INCREMENTAL"  # Only the new compatible policy is usable.


@pytest.mark.parametrize("corruption", ["unsupported", "missing", "conflicting"])
def test_corrupt_required_current_transport_errors_before_semantic_execution(monkeypatch, corruption):
    manifest = NormativeIssueManifest(7, "Current", True, True, (NormativeRequirement("REQ-001", "Preserve state."),))
    evidence = IndividualReviewEvidence("first baseline", objective=ObjectiveAnchor(7, "UNANCHORED", None, "original", ObjectiveExtraction("ABSENT")))
    original = encode_individual_evidence

    def corrupt(inputs, texts):
        wire = json.loads(original(inputs, texts))
        if corruption == "unsupported":
            wire["version"] = 999
        elif corruption == "missing":
            wire["definitions"].pop()
        else:
            wire["definitions"][0] = ["s", "A substituted current input"]
        return json.dumps(wire)

    monkeypatch.setattr("auto_coder.specification_analyzer.encode_individual_evidence", corrupt)
    invoked = []
    result = analyze_issue_specification(manifest, "caller body", review_evidence=evidence, prompt_runner=lambda prompt: invoked.append(prompt))
    assert result.verdict == "ERROR"
    assert "could not be reconstructed" in result.error
    assert invoked == []


@pytest.mark.usefixtures("_use_real_commands")
def test_advisory_cases_render_complete_inputs_without_claiming_semantic_results(tmp_path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "advisory"
    result = subprocess.run([sys.executable, str(root / "scripts/replay_individual_evidence_transport.py"), "--repository", "owner/repo", "--output", str(output)], cwd=root, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    records = json.loads((output / "results.json").read_text())
    assert {record["case"] for record in records} == {"missing-invariant-full", "missing-invariant-incremental", "adjacent-demand-full", "adjacent-demand-incremental"}
    for record in records:
        assert record["semantic_execution"] is False
        assert record["observed_result"] is None
        assert record["case_version"] == "individual-evidence-advisory-v1"
        assert record["policy_identity"] == validation_policy_identity()
        assert record["observed_backend"] is None and record["observed_model"] is None
        prompt = (output / (record["case"] + ".prompt.txt")).read_text()
        inputs = prompt_evidence(prompt)
        requirements = inputs["current"]["manifest"]["requirements"]
        assert len(requirements) == (2 if record["case"].startswith("adjacent") else 1)
        assert "without modifying persisted live state" in inputs["current"]["objective"]["original_text"]
        assert inputs["current"]["relationship"]["role"] == "standalone"
        assert "avatar" in inputs["current"]["body"]


def test_live_replay_records_actual_execution_model_and_versions(tmp_path, monkeypatch):
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/replay_individual_evidence_transport.py"))
    replay = module["replay"]
    manager = Mock()
    manager.get_last_backend_and_model.return_value = ("observed-fallback", "observed-model")
    calls = []

    def execute(prompt, *, backend_manager, is_noedit):
        assert backend_manager is manager and is_noedit is True
        calls.append(prompt)
        return '{"verdict":"ERROR","remediation":"NONE","findings":[],"finding_dispositions":[],"coverage":[]}'

    monkeypatch.setitem(replay.__globals__, "create_adversarial_validation_backend_manager", lambda **_: manager)
    monkeypatch.setitem(replay.__globals__, "run_llm_prompt", execute)
    records = replay(tmp_path / "controlled-metadata", "owner/repo", True)
    assert len(calls) == len(records) == 4
    for record in records:
        assert record["observed_backend"] == "observed-fallback"
        assert record["observed_model"] == "observed-model"
        assert record["case_version"] == "individual-evidence-advisory-v1"
        assert record["policy_identity"] == validation_policy_identity()


@pytest.mark.parametrize("role,parent_context", [("standalone", None), ("child", ""), ("child", "Closed parent evidence\r\n")])
@pytest.mark.parametrize("mode", ["FULL", "INCREMENTAL"])
def test_complete_production_prompt_reconstructs_independent_large_family_fixture(role, parent_context, mode, record_property):
    requirement = "Preserve each captured occurrence and its authority. 日本語。 " * 30
    title = "Lossless family evidence"
    body = f"## Requirements\r\nREQ-001: {requirement}\r\n## Context\r\nLiteral ie2:other:0 is data."
    related = json.dumps([{"issue_number": n, "state": "closed", "body_non_normative_evidence": f"Member {n}: " + ("Complete closed-member evidence 日本語. " * 250)} for n in range(100, 125)], ensure_ascii=False, separators=(",", ":"))
    objective = ObjectiveAnchor(7, "UNANCHORED", None, "fixture:v1", ObjectiveExtraction("ABSENT"))
    baseline = json.dumps({"issue_number": 7, "title": title, "body": body, "requirements": [{"requirement_id": "REQ-001", "text": requirement}]}, ensure_ascii=False, sort_keys=True, indent=2)
    predecessor = (
        json.dumps(
            {
                "decision_key": "accepted-predecessor",
                "accepted_sequence": 11,
                "review_inputs": {"related_contracts": related, "body": body, "baseline": baseline},
                "findings": [{"finding_id": "finding-a", "finding": {"explanation": "A fixed invariant was missing."}, "last_disposition": "RESOLVED"}],
                "finding_dispositions": [{"finding_id": "finding-a", "disposition": "RESOLVED", "reasoning": "Exact current boundary was added."}],
                "coverage": [{"boundary": "contract-wide", "status": "FRESH"}],
                "execution_provenance": {"backend": "retained-producer"},
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if mode == "INCREMENTAL"
        else ""
    )
    delta = json.dumps({"before": {"related_contracts": related, "title": title}, "after": {"related_contracts": related, "title": title}}, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if mode == "INCREMENTAL" else ""
    incremental = IncrementalReviewContext(mode, "accepted-predecessor" if predecessor else None, predecessor, delta)
    evidence = IndividualReviewEvidence(baseline, ("Previous material outcome\r\n", ""), objective, incremental)
    relationship = IndividualRelationshipContext(role, related, 99 if role == "child" else None)
    manifest = NormativeIssueManifest(7, title, True, True, (NormativeRequirement("REQ-001", requirement),))
    expected = {
        "current": {
            "manifest": {"issue_number": 7, "title": title, "explicit_contract_present": True, "explicit_contract_valid": True, "requirements": ({"requirement_id": "REQ-001", "text": requirement},), "error": None, "invalid_reason": None},
            "title": title,
            "body": body,
            "parent_context": parent_context,
            "relationship": {"role": role, "related_contracts": related, "parent_number": 99 if role == "child" else None},
            "baseline": baseline,
            "prior_applied_outcomes": ("Previous material outcome\r\n", ""),
            "objective": {"issue_number": 7, "state": "UNANCHORED", "original_text": None, "source_identity": "fixture:v1", "current": {"status": "ABSENT", "text": None}},
        },
        "incremental": {"mode": mode, "predecessor_decision_key": "accepted-predecessor" if predecessor else None, "predecessor_evidence": predecessor, "assessed_delta": delta},
    }
    captured = []

    def receiver(prompt):
        assert prompt.count(requirement) == 1, "Outgoing evidence repeats the same complete Requirement text"
        assert prompt_evidence(prompt) == expected
        captured.append(prompt)
        # This response checks orchestration/coverage only, not semantic skill.
        return json.dumps(
            {
                "verdict": "READY",
                "remediation": "NONE",
                "findings": [],
                "finding_dispositions": [{"finding_id": "finding-a", "disposition": "RESOLVED", "reasoning": "Fixture controller test"}] if predecessor else [],
                "coverage": [{"boundary": boundary, "status": "FRESH", "no_impact_reason": ""} for boundary in ("REQ-001", "contract-wide")],
            }
        )

    result = analyze_issue_specification(manifest, body, parent_context=parent_context, relationship_context=relationship, review_evidence=evidence, prompt_runner=receiver)
    assert result.verdict == "READY", result.error
    prompt = captured[0]
    raw = render_prompt(
        "issue.adversarial_specification_analysis",
        issue_number=7,
        issue_title=title,
        normative_manifest=json.dumps([{"requirement_id": "REQ-001", "text": requirement}], ensure_ascii=False, indent=2),
        issue_body=body,
        parent_context=parent_context or "(No parent Issue context supplied.)",
        relationship_role=role,
        related_contracts=related,
        durable_baseline=baseline,
        prior_applied_outcomes="\n\n".join(evidence.prior_applied_outcomes),
        objective_evidence=objective_evidence_json(objective),
        review_mode=mode,
        predecessor_decision_key=incremental.predecessor_decision_key or "(none)",
        predecessor_evidence=predecessor or "(none; perform a full review)",
        assessed_delta=delta or "(none; perform a full review)",
    )
    for name, measurement in (("characters", len), ("utf8_bytes", lambda text: len(text.encode("utf-8")))):
        before, after = measurement(raw), measurement(prompt)
        record_property(name + "_uninterned", before)
        record_property(name + "_interned_complete_prompt", after)
        if mode == "INCREMENTAL":
            assert after < before * 0.5
    assert len(prompt.encode("utf-8")) > len(prompt)
    # Exact Requirement bytes occur once despite manifest/body/baseline copies.
    assert prompt.count(requirement) == 1
