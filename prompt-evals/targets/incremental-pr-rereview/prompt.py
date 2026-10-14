"""Render the production incremental PR-review prompt for semantic evaluation."""

import json
from pathlib import Path
from string import Template

import yaml

POLICY_KEYS = (
    "objective_requirements_boundary",
    "parent_child_contract_boundary",
    "pr_validation_contract_boundary",
    "bounded_correction_contract",
    "task_scoped_test_execution",
)


def _production_template(prompts, template):
    """Mirror prompt_loader's policy composition for PR validation prompts."""
    fragments = [prompts["policies"][key].rstrip() for key in POLICY_KEYS]
    return "\n\n".join([*fragments, template])


def render(context):
    """Assemble the same self-contained incremental prompt production sends."""
    variables = context["vars"]
    prompts = yaml.safe_load((Path(__file__).resolve().parents[3] / "src/auto_coder/prompts.yaml").read_text(encoding="utf-8"))
    rereview = Template(_production_template(prompts, prompts["pr"]["adversarial_validation_rereview"])).substitute(
        previous_head_sha="accepted-head",
        current_head_sha="current-head",
    )
    gap = {
        "gap_id": "TOG-original-oracle",
        "requirement_id": "REQ-001",
        "requirement_text": variables["requirement"],
        "authoritative_boundary": "ReviewController.validate",
        "invariant": "The exact current Requirement is protected at the production review boundary.",
        "plausible_incorrect_implementation": "The correction bypasses the required guard.",
        "why_tests_still_pass": "No production-boundary regression observes the guard.",
        "material_consequence": "The explicit Requirement can regress undetected.",
        "focused_regression_scenario": "Exercise ReviewController.validate and assert the guard.",
        "status": variables["prior_gap_status"],
        "resolution_evidence": variables.get("prior_resolution_evidence", ""),
        "resolution_head_sha": "accepted-head" if variables["prior_gap_status"] == "RESOLVED" else "",
    }
    values = {
        "review_policy": rereview,
        "repo_name": "owner/repo",
        "pr_number": "2495",
        "pr_title": variables["title"],
        "pr_body": "Closes #2493",
        "linked_issues_context": variables["scope_context"],
        "requirement_manifest": f"Manifest mode: explicit\n- REQ-001: {variables['requirement']}",
        "changed_files": "- src/review_controller.py\n- tests/test_review_controller.py",
        "coverage_status": "COMPLETE: every changed file has complete patch evidence.",
        "changed_tests": "- tests/test_review_controller.py",
        "pr_diff": variables["current_evidence"],
        "claimed_review_threads": variables["review_demand"],
        "prior_test_oracle_gaps": json.dumps([gap], indent=2, sort_keys=True),
        "accepted_strong_findings": "(No accepted Strong audit findings are recorded for this PR.)",
        "ordinary_requirement_ids": '["REQ-001"]',
        "ci_execution_evidence": "Current-head focused tests passed; semantic judgment remains independent.",
        "adjacent_exploration_budget": "0",
        "evidence_recovery_budget": "0",
        "closure_extension": "",
    }
    main = _production_template(prompts, prompts["pr"]["adversarial_validation"])
    return Template(main).substitute(values)
