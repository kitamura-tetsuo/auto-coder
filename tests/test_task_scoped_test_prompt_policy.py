"""Deterministic delivery regressions for task-scoped LLM test execution."""

from pathlib import Path

import pytest

from auto_coder.pr_repair import ExistingPrRepairTarget, build_existing_pr_repair_prompt
from auto_coder.prompt_loader import DEFAULT_PROMPTS_PATH, render_prompt

POLICY = "TASK-SCOPED TEST EXECUTION POLICY:"


def _issue_values() -> dict[str, object]:
    return {
        "repo_name": "owner/repo",
        "issue_number": 2478,
        "issue_title": "Bound test execution",
        "issue_body": "## Requirements\nREQ-001: Keep execution focused.",
        "issue_labels": "urgent",
        "issue_state": "open",
        "issue_author": "operator",
        "commit_log": "(No commit history)",
    }


@pytest.mark.parametrize(
    "key",
    [
        "issue.action",
        "issue.local_implementation",
        "jules.issue.action",
        "pr.action",
        "pr.github_actions_fix",
        "pr.local_test_fix",
        "pr.merge_conflict_resolution",
        "pr.local_review_correction",
        "pr.local_repair_verification",
        "pr.adversarial_validation_initial_review",
        "pr.adversarial_validation_rereview",
        "pr.adversarial_validation_fix",
        "codex_cloud.initial_issue_implementation",
        "codex_cloud.continuation",
    ],
)
def test_every_operation_prompt_carries_scoped_test_policy(key: str) -> None:
    prompt = render_prompt(key)

    assert prompt.count(POLICY) == 1
    assert "The complete repository test suite is run by CI, not by the agent." in prompt
    assert "runner or wrapper honors that selection" in prompt
    assert "iterating every file, shard, package, or delegated agent" in prompt
    assert "passed, failed, interrupted, or not-run" in prompt


def test_label_selected_prompt_and_backend_wrappers_preserve_policy() -> None:
    values = _issue_values()
    selected = render_prompt(
        "issue.action",
        labels=["urgent"],
        label_prompt_mappings={"urgent": "issue.urgent"},
        label_priorities=["urgent"],
        **values,
    )
    muse_payload = render_prompt("muse.execution", mode="edit", result_root="/tmp/result", task_prompt=selected)
    opencode_payload = render_prompt("opencode.execution", result_root="/tmp/result", task_prompt=selected)

    assert "URGENT ISSUE" in selected
    assert selected.count(POLICY) == 1
    assert muse_payload.count(POLICY) == 1
    assert opencode_payload.count(POLICY) == 1


def test_custom_label_template_receives_policy_when_it_maps_to_governed_key(tmp_path: Path) -> None:
    custom = tmp_path / "prompts.yaml"
    custom.write_text(Path(DEFAULT_PROMPTS_PATH).read_text(encoding="utf-8").replace("URGENT ISSUE", "CUSTOM URGENT ISSUE"), encoding="utf-8")

    prompt = render_prompt(
        "issue.action",
        path=str(custom),
        labels=["custom"],
        label_prompt_mappings={"custom": "issue.urgent"},
        label_priorities=["custom"],
        **_issue_values(),
    )

    assert "CUSTOM URGENT ISSUE" in prompt
    assert prompt.count(POLICY) == 1


def test_existing_cloud_session_followup_receives_new_policy_directly() -> None:
    target = ExistingPrRepairTarget("owner/repo", 52, "issue-52", "main", "abc123")

    prompt = build_existing_pr_repair_prompt(target, "Repair the bounded finding and run its focused regression.")

    assert prompt.count(POLICY) == 1
    assert "Repair the bounded finding" in prompt
    assert "Do not create a new pull request." in prompt


def test_stricter_no_test_and_no_edit_controls_are_not_reversed() -> None:
    issue_prompt = render_prompt("issue.action", **_issue_values())
    deferred = render_prompt("operator.deferred_local_tests", include_header=False)
    noedit = render_prompt("opencode.noedit_execution", task_prompt=issue_prompt)

    assert POLICY in issue_prompt
    assert "do not execute local tests during this invocation" in deferred
    assert "Explicit no-test, no-shell, and no-edit constraints remain stricter" in issue_prompt
    assert "you cannot edit files, run shell commands" in noedit


def test_advisory_semantic_examples_are_decided_by_delivered_policy() -> None:
    """Advisory examples document intended semantics, not observed model behavior."""
    policy = render_prompt("issue.action", **_issue_values())

    assert "focused integration or end-to-end test outside edited directories" in policy
    assert "do not recreate a full-suite run by iterating every file, shard, package" in policy
    assert "If the command cannot be scoped, its scope is unknown" in policy
    assert "semantic prompt evaluation remains advisory rather than merge-blocking" in policy
