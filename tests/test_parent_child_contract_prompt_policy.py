from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from auto_coder.prompt_loader import render_prompt

ISSUE_RENDER_DATA = {
    "repo_name": "owner/repo",
    "issue_number": 1954,
    "issue_title": "Align authoring, repair, and downstream prompts",
    "issue_body": "## Objective\nPrevent authoring and repair prompts from creating parent implementation contracts.\n\n## Requirements\nREQ-001: Reject a parent Requirements section.",
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
def test_production_label_dispatch_composes_parent_child_boundary_policy(label: str, template_key: str, marker: str) -> None:
    prompt = render_prompt(
        "issue.action",
        **ISSUE_RENDER_DATA,
        labels=[label],
        label_prompt_mappings={label: template_key},
        label_priorities=[label],
    )

    assert marker in prompt
    assert "PARENT/CHILD ISSUE CONTRACT BOUNDARY POLICY" in prompt
    assert "must never carry a `## Requirements` section" in prompt
    assert "explicit single-line `REQ-NNN:` Requirements" in prompt


def test_default_and_jules_issue_production_routes_receive_parent_child_policy() -> None:
    default = render_prompt("issue.action", **ISSUE_RENDER_DATA)
    jules = render_prompt("issue.action", **ISSUE_RENDER_DATA, is_jules=True)
    cloud = render_prompt(
        "codex_cloud.initial_issue_implementation",
        repo_name="owner/repo",
        base_branch="main",
        issue_number=1954,
        issue_url="https://github.com/owner/repo/issues/1954",
        issue_title="Align authoring, repair, and downstream prompts",
        issue_body=ISSUE_RENDER_DATA["issue_body"],
        issue_labels="implementation-ready",
        issue_state="open",
        issue_author="author",
        issue_attempt=1,
        backend_name="codex",
        linked_issues_context="(none)",
        commit_log="(none)",
    )

    for prompt in (default, jules, cloud):
        assert "PARENT/CHILD ISSUE CONTRACT BOUNDARY POLICY" in prompt
        assert "never grandfathered or silently stripped" in prompt
        assert "distinct from hard runtime structural enforcement" in prompt


def test_issue_action_decomposition_instructions_forbid_parent_requirements_and_require_child_ownership() -> None:
    prompt = render_prompt("issue.action", **ISSUE_RENDER_DATA)

    assert "This issue itself becomes a contract-free tracking parent" in prompt
    assert "do not add a\n  `## Requirements` section or any REQ-NNN declaration to it." in prompt
    assert "Create every\n  prerequisite sibling first so its real Issue number is already known" in prompt
    assert "never publish a dependent first and repair `Blocked-By:` afterward" in prompt


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
def test_initial_followup_and_corrective_pr_variants_receive_parent_child_boundary(key: str) -> None:
    prompt = render_prompt(key)

    assert "PARENT/CHILD ISSUE CONTRACT BOUNDARY POLICY" in prompt
    assert "treat the parent as coordination/scope evidence with no implementation Requirements" in prompt
    assert "fabricated parent REQ ID" in prompt
    assert "never authorizes implementing the parent itself" in prompt


def test_registered_semantic_target_depends_on_shared_policy_and_has_five_cases() -> None:
    root = Path(__file__).resolve().parents[1]
    registry = json.loads((root / "prompt-evals/registry.json").read_text(encoding="utf-8"))
    target = next(item for item in registry["targets"] if item["id"] == "parent-child-contract-boundary")
    dependency = target["prompt_dependencies"][0]
    cases = yaml.safe_load((root / "prompt-evals/targets/parent-child-contract-boundary/cases/policy.yaml").read_text(encoding="utf-8"))

    assert dependency == {
        "path": "src/auto_coder/prompts.yaml",
        "keys": ["policies.parent_child_contract_boundary"],
    }
    assert {case["vars"]["id"] for case in cases} == {
        "new-parent-child-authoring",
        "reject-parent-requirement-repair",
        "legacy-removal-preserves-behavior",
        "dependency-roots-and-join",
        "parent-context-hidden-pr-requirement",
    }
