"""Shared semantics for repairing an existing pull request via a cloud follow-up.

Auto-Coder can send follow-up instructions to an existing cloud coding
task/session when a pull request needs corrective work (adversarial-validation
fixes, merge-conflict repair, CI repair, or review-driven repair). Every such
follow-up shares the same invariant: the cloud task must update the exact PR
that triggered the repair request, on its current head branch, and must never
satisfy the request by creating a new branch, a new pull request, or a
replacement task/session. This module centralizes that invariant so callers
cannot recreate weaker, provider-specific wording that omits it.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .prompt_loader import render_prompt


@dataclass
class ExistingPrRepairTarget:
    """Identifies the exact existing pull request a repair follow-up must update."""

    repo_name: str
    pr_number: int
    head_branch: str
    base_branch: str
    head_sha: str


def resolve_existing_pr_repair_target(repo_name: str, pr_data: Dict[str, Any]) -> Optional[ExistingPrRepairTarget]:
    """Extract the repair target from PR metadata, or None if it is incomplete.

    A repair follow-up cannot enforce the same-PR invariant without knowing the
    exact head branch, head commit, and base branch, so callers must fall back
    to their existing (weaker) behavior when this returns None.
    """
    pr_number = pr_data.get("number")
    head = pr_data.get("head") or {}
    base = pr_data.get("base") or {}
    head_branch = pr_data.get("head_branch") or head.get("ref")
    base_branch = pr_data.get("base_branch") or base.get("ref")
    head_sha = head.get("sha") or pr_data.get("head_sha")

    if not pr_number or not head_branch or not base_branch or not head_sha:
        return None

    return ExistingPrRepairTarget(
        repo_name=repo_name,
        pr_number=pr_number,
        head_branch=head_branch,
        base_branch=base_branch,
        head_sha=head_sha,
    )


def build_existing_pr_repair_prompt(target: ExistingPrRepairTarget, details: str) -> str:
    """Render a PR-repair follow-up prompt that enforces the same-PR invariant.

    ``details`` carries the workflow-specific corrective instructions
    (adversarial-validation findings, merge-conflict resolution steps, CI
    failure context, or review feedback). The invariant preamble/suffix is
    identical for every workflow so it cannot be independently weakened.
    """
    return render_prompt(
        "pr.existing_pr_repair",
        repo_name=target.repo_name,
        pr_number=target.pr_number,
        head_branch=target.head_branch,
        base_branch=target.base_branch,
        head_sha=target.head_sha,
        details=details,
    )
