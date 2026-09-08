from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from auto_coder.prompt_loader import render_prompt

ISSUE_RENDER_DATA = {
    "repo_name": "owner/repo",
    "issue_number": 1860,
    "issue_title": "Keep preview purpose short",
    "issue_body": "## Objective\nPreview a saved draft without applying it.\n\n## Requirements\nREQ-001: Return the preview.",
    "issue_labels": "implementation-ready",
    "issue_state": "open",
    "issue_author": "author",
    "commit_log": "(none)",
    "linked_issues_context": "Parent anchor: exact text",
}


@pytest.mark.parametrize(
    ("label", "template_key", "marker"),
    [
        ("breaking-change", "issue.breaking_change", "BREAKING CHANGE REQUIREMENTS"),
        ("urgent", "issue.urgent", "URGENT ISSUE REQUIREMENTS"),
        ("bug", "issue.bug", "BUG FIX REQUIREMENTS"),
        ("enhancement", "issue.enhancement", "ENHANCEMENT REQUIREMENTS"),
        ("documentation", "issue.documentation", "DOCUMENTATION REQUIREMENTS"),
    ],
)
def test_production_label_dispatch_composes_short_objective_policy(label: str, template_key: str, marker: str) -> None:
    prompt = render_prompt(
        "issue.action",
        **ISSUE_RENDER_DATA,
        labels=[label],
        label_prompt_mappings={label: template_key},
        label_priorities=[label],
    )

    assert marker in prompt
    assert "one or two short prose sentences" in prompt
    assert "preserve its Objective verbatim" in prompt
    assert "sole merge-blocking implementation contract" in prompt


def test_default_and_jules_issue_production_routes_receive_policy() -> None:
    default = render_prompt("issue.action", **ISSUE_RENDER_DATA)
    jules = render_prompt("issue.action", **ISSUE_RENDER_DATA, is_jules=True)

    for prompt in (default, jules):
        assert "ISSUE AUTHORING AND REVIEW-RESPONSE POLICY" in prompt
        assert "Legacy Issues without an Objective continue normally" in prompt
        assert "Preserve supplied original/current Objective anchor evidence verbatim" in prompt


@pytest.mark.parametrize(
    "key",
    [
        "pr.action",
        "pr.github_actions_fix",
        "pr.local_test_fix",
        "pr.adversarial_validation_initial_review",
        "pr.adversarial_validation_rereview",
        "pr.adversarial_validation_followup",
        "pr.adversarial_validation_fix",
    ],
)
def test_initial_followup_and_corrective_pr_variants_receive_contract_boundary(key: str) -> None:
    prompt = render_prompt(key)

    assert "Treat an Issue Objective only as specification-scope evidence" in prompt
    assert "sole merge-blocking implementation contract" in prompt
    assert "Do not fabricate Requirement IDs" in prompt
    assert "never bypasses independent specification/readiness gates" in prompt


def test_registered_semantic_target_depends_on_shared_policy_and_has_five_cases() -> None:
    root = Path(__file__).resolve().parents[1]
    registry = json.loads((root / "prompt-evals/registry.json").read_text(encoding="utf-8"))
    target = next(item for item in registry["targets"] if item["id"] == "short-objective-contract")
    dependency = target["prompt_dependencies"][0]
    cases = yaml.safe_load((root / "prompt-evals/targets/short-objective-contract/cases/policy.yaml").read_text(encoding="utf-8"))

    assert dependency == {
        "path": "src/auto_coder/prompts.yaml",
        "keys": ["policies.short_objective_authoring", "policies.objective_requirements_boundary"],
    }
    assert {case["vars"]["id"] for case in cases} == {
        "new-authoring",
        "purpose-preserving-correction",
        "reject-adjacent-demand",
        "user-owned-purpose-change",
        "objective-only-pr-gap",
    }
