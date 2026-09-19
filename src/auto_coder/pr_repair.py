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


def build_existing_pr_repair_prompt(
    target: ExistingPrRepairTarget,
    details: str,
    *,
    bundle: Optional[Any] = None,
) -> str:
    """Render a PR-repair follow-up prompt that enforces the same-PR invariant.

    ``details`` carries the workflow-specific corrective instructions
    (adversarial-validation findings, merge-conflict resolution steps, CI
    failure context, or review feedback). The invariant preamble/suffix is
    identical for every workflow so it cannot be independently weakened.

    When ``bundle`` is provided, validates that the bundle is bound to the
    exact target PR and head commit (REQ-003, REQ-009).
    """
    if bundle is not None:
        from .bounded_repair_bundle import BundleStaleError

        if bundle.reviewed_head_sha != target.head_sha:
            raise BundleStaleError(
                bundle.bundle_id,
                f"Bundle reviewed head '{bundle.reviewed_head_sha[:8]}' does not match target head '{target.head_sha[:8]}'",
            )
        if bundle.pr_number != target.pr_number or bundle.repo_name != target.repo_name:
            raise BundleStaleError(
                bundle.bundle_id,
                f"Bundle target '{bundle.repo_name}#{bundle.pr_number}' does not match target '{target.repo_name}#{target.pr_number}'",
            )

    return render_prompt(
        "pr.existing_pr_repair",
        repo_name=target.repo_name,
        pr_number=target.pr_number,
        head_branch=target.head_branch,
        base_branch=target.base_branch,
        head_sha=target.head_sha,
        details=details,
    )


def build_bounded_existing_pr_repair_prompt(
    target: ExistingPrRepairTarget,
    bundle: Any,
    *,
    extra_details: Optional[str] = None,
) -> str:
    """Render a PR-repair prompt driven directly by a bounded repair bundle (REQ-001..REQ-003)."""
    from .bounded_repair_bundle import render_bounded_repair_payload

    payload = render_bounded_repair_payload(bundle)
    if extra_details:
        payload = f"{payload}\n\n---\n\n{extra_details}"
    return build_existing_pr_repair_prompt(target, payload, bundle=bundle)
