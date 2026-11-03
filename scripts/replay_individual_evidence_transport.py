"""Advisory semantic replay of production-rendered interned Issue evidence.

Rendering is the default. --execute makes one read-only semantic call per case;
observed verdicts are recorded, never used as a deterministic merge gate.
"""

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from auto_coder.backend_manager import run_llm_prompt
from auto_coder.cli_helpers import create_adversarial_validation_backend_manager
from auto_coder.individual_evidence_envelope import decode_individual_evidence
from auto_coder.llm_backend_config import active_repo_context
from auto_coder.objective_evidence import ObjectiveAnchor, extract_objective
from auto_coder.requirement_contract import build_normative_issue_manifest
from auto_coder.specification_analyzer import IncrementalReviewContext, IndividualRelationshipContext, IndividualReviewEvidence, analyze_issue_specification


def replay(output: Path, repository: str, execute: bool) -> list[dict[str, object]]:
    output.mkdir(parents=True, exist_ok=False)
    results = []
    with active_repo_context(repository):
        manager = create_adversarial_validation_backend_manager(validation_kind="issue") if execute else None
        if execute and manager is None:
            raise RuntimeError("No configured strong Issue validation backend")
        for name in ("missing-invariant", "adjacent-demand"):
            objective_text = "Render the selected draft as a JSON preview without modifying persisted live state."
            requirement = "Return a JSON object containing exactly the supplied draft's id and content, with their supplied values unchanged."
            requirements = "REQ-001: " + requirement
            if name == "adjacent-demand":
                requirements += "\nREQ-002: Do not modify any persisted live state while producing the preview."
            body = "## Objective\n\n" + objective_text + "\n\n## Requirements\n\n" + requirements + "\n\n## Context\n\nAn optional adjacent suggestion is to add an avatar editor; it is outside the preview contract."
            manifest = build_normative_issue_manifest(7, name, body)
            objective = ObjectiveAnchor(7, "ANCHORED", objective_text, "advisory-original", extract_objective(body))
            related = json.dumps([{"issue_number": 99, "state": "closed", "body_non_normative_evidence": "An unrelated avatar editor may be considered separately."}], separators=(",", ":"))
            for mode in ("FULL", "INCREMENTAL"):
                finding = {
                    "finding_id": "prior-finding",
                    "category": "objective_conflict" if name == "missing-invariant" else "hidden_requirement",
                    "requirement_ids": [],
                    "explanation": "No Requirement forbids persisted live-state writes." if name == "missing-invariant" else "Add an avatar editor, an adjacent suggestion outside preview behavior.",
                    "clarification": "State the missing no-write invariant." if name == "missing-invariant" else "Implement avatar editing.",
                    "counterexample": "",
                    "missing_normative_boundary": "",
                    "last_disposition": "STILL_VALID",
                    "last_reasoning": "Historical evidence, not authority.",
                }
                predecessor = (
                    json.dumps(
                        {"decision_key": "advisory-predecessor", "accepted_sequence": 1, "review_inputs": {"body": body, "related_contracts": related}, "verdict": "BLOCKED", "findings": [finding], "finding_dispositions": [], "coverage": [], "execution_provenance": "advisory-history"},
                        separators=(",", ":"),
                    )
                    if mode == "INCREMENTAL"
                    else ""
                )
                context = IncrementalReviewContext(mode, "advisory-predecessor" if predecessor else None, predecessor, '{"changed_fields":[]}' if predecessor else "")
                evidence = IndividualReviewEvidence(body, (), objective, context)
                case = name + "-" + mode.lower()

                def receiver(prompt: str) -> str:
                    payload = prompt.rsplit("```json\n", 1)[1].split("\n```", 1)[0]
                    complete = decode_individual_evidence(payload)
                    assert complete["current"]["body"] == body
                    assert complete["current"]["relationship"]["related_contracts"] == related
                    assert complete["incremental"] == asdict(context)
                    (output / (case + ".prompt.txt")).write_text(prompt, encoding="utf-8")
                    (output / (case + ".logical-inputs.json")).write_text(json.dumps(complete, ensure_ascii=False, indent=2), encoding="utf-8")
                    if execute:
                        response = run_llm_prompt(prompt, backend_manager=manager, is_noedit=True)
                        (output / (case + ".response.txt")).write_text(response, encoding="utf-8")
                        return response
                    return '{"verdict":"ERROR","remediation":"NONE","findings":[],"finding_dispositions":[],"coverage":[]}'

                result = analyze_issue_specification(manifest, body, review_evidence=evidence, relationship_context=IndividualRelationshipContext("standalone", related), prompt_runner=receiver)
                prompt = (output / (case + ".prompt.txt")).read_text(encoding="utf-8")
                record = {
                    "case": case,
                    "semantic_execution": execute,
                    "expected_advisory_boundary": "A genuine missing no-write invariant" if name == "missing-invariant" else "Avatar editing is adjacent and non-blocking",
                    "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                    "characters": len(prompt),
                    "utf8_bytes": len(prompt.encode()),
                    "complete_reference_resolution": True,
                    "observed_result": asdict(result) if execute else None,
                }
                results.append(record)
                (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    print(json.dumps(replay(args.output, args.repository, args.execute), ensure_ascii=False, indent=2))
