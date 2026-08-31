"""Tests for the shared existing-PR repair semantic layer (Issue #1609)."""

from src.auto_coder.pr_repair import (
    ExistingPrRepairTarget,
    build_existing_pr_repair_prompt,
    resolve_existing_pr_repair_target,
)


def test_resolve_existing_pr_repair_target_extracts_complete_metadata():
    pr_data = {
        "number": 200,
        "head": {"ref": "issue-100-fix", "sha": "H1"},
        "base": {"ref": "main"},
    }

    target = resolve_existing_pr_repair_target("owner/repo", pr_data)

    assert target == ExistingPrRepairTarget(
        repo_name="owner/repo",
        pr_number=200,
        head_branch="issue-100-fix",
        base_branch="main",
        head_sha="H1",
    )


def test_resolve_existing_pr_repair_target_returns_none_when_incomplete():
    assert resolve_existing_pr_repair_target("owner/repo", {"number": 200}) is None
    assert resolve_existing_pr_repair_target("owner/repo", {"head": {"ref": "x", "sha": "H1"}}) is None


def test_build_existing_pr_repair_prompt_carries_ac_001_invariants():
    """Reproduces the PR #1608 failure mode's regression oracle (Issue #1609 AC-001)."""
    target = ExistingPrRepairTarget(
        repo_name="owner/repo",
        pr_number=200,
        head_branch="issue-100-fix",
        base_branch="main",
        head_sha="H1",
    )

    prompt = build_existing_pr_repair_prompt(target, "Fix the adversarial validation findings.")

    assert "pull request #200" in prompt
    assert "issue-100-fix" in prompt
    assert "H1" in prompt
    assert "main" in prompt
    assert "Do not create a new branch." in prompt
    assert "Do not create a new pull request." in prompt
    assert "Do not replace or close the existing pull request." in prompt
    assert "Creating another PR does not satisfy this task." in prompt
    assert "Fix the adversarial validation findings." in prompt
