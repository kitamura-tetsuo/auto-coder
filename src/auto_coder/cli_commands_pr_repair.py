"""Operator CLI for pull request repair allowance inspection and resumption."""

from __future__ import annotations

import json
import sys
from typing import Optional

import click

from .canonical_pr_blocker_ledger import CanonicalPRBlockerLedger
from .durable_repair_allowance import (
    OUTSTANDING_LIFECYCLE_STATES,
    GenerationLifecycleState,
    InvalidOperatorGrantError,
    RepairAllowanceError,
    RepairAllowanceIdempotencyConflictError,
    RepairAllowanceLedger,
    RepairAllowanceStatus,
    reconcile_unfulfilled_grant_reevaluations,
)
from .github_pending_work import PendingWorkStore, WorkIdentity
from .llm_backend_config import get_pr_repair_max_failed_corrections
from .logger_config import get_logger
from .pr_repair_guard import check_pr_repair_exhaustion
from .trace_logger import get_trace_logger
from .util.github_request_outcome import normalize_api_origin

logger = get_logger(__name__)


@click.group(name="pr-repair")
def pr_repair_group() -> None:
    """Inspect and manage pull request repair allowances and operator resumptions."""
    pass


@pr_repair_group.command(name="status")
@click.option("--repo", required=True, help="Repository in owner/repo form, e.g. acme/widgets")
@click.option("--pr", required=True, type=int, help="Pull request number")
@click.option("--json", "json_output", is_flag=True, help="Output status in JSON format")
def pr_repair_status(repo: str, pr: int, json_output: bool) -> None:
    """Inspect the durable state of canonical blockers and repair allowances (REQ-008).

    Read-only command that performs zero state mutations.
    """
    allowance_ledger = RepairAllowanceLedger()
    blocker_ledger = CanonicalPRBlockerLedger()
    norm_origin = normalize_api_origin("https://api.github.com")

    # Read-only inspection
    try:
        a_snapshot = allowance_ledger.get_snapshot(norm_origin, repo, pr, require_retained_state=False)
    except Exception:
        a_snapshot = None

    try:
        b_snapshot = blocker_ledger.get_snapshot(norm_origin, repo, pr, require_retained_state=False)
    except Exception:
        b_snapshot = None

    exhaustion_info = check_pr_repair_exhaustion(
        repo_name=repo,
        pr_number=pr,
        api_origin=norm_origin,
        allowance_ledger=allowance_ledger,
        blocker_ledger=blocker_ledger,
    )

    open_blockers = b_snapshot.get_open_blockers() if b_snapshot else ()
    open_blocker_ids = [b.blocker_id for b in open_blockers]
    if not open_blocker_ids and a_snapshot:
        open_blocker_ids = [b.blocker_id for b in a_snapshot.blockers]

    repair_state = "ALLOWABLE"
    if exhaustion_info and exhaustion_info.is_exhausted:
        repair_state = "EXHAUSTED"
    elif a_snapshot and any(b.status == RepairAllowanceStatus.RECONCILIATION_REQUIRED for b in a_snapshot.blockers):
        repair_state = "RECONCILIATION_REQUIRED"

    current_epoch = a_snapshot.epoch if a_snapshot else 0
    exhausted_ids = list(exhaustion_info.exhausted_blocker_ids) if exhaustion_info else []

    blocker_data = []
    if a_snapshot:
        b_map = {b.blocker_id: b for b in b_snapshot.blockers} if b_snapshot else {}
        for b_allowance in a_snapshot.blockers:
            bid = b_allowance.blocker_id
            b_snap = b_map.get(bid)
            q_reqs = [[qr.issue_number, qr.requirement_id] for qr in b_snap.qualified_requirements] if b_snap else []
            blocker_data.append(
                {
                    "blocker_id": bid,
                    "status": b_allowance.status.value,
                    "total_failed_count": b_allowance.total_failed_count,
                    "limit": b_allowance.limit,
                    "remaining": b_allowance.remaining,
                    "historical_unknown": bool(b_allowance.historical_unknown),
                    "exhaustion_reason": b_allowance.exhaustion_reason or "",
                    "qualified_requirements": q_reqs,
                }
            )

    outstanding_gens = []
    if a_snapshot:
        for gen in a_snapshot.generations:
            if gen.is_outstanding():
                outstanding_gens.append(
                    {
                        "generation_id": gen.generation_id,
                        "lifecycle_state": gen.lifecycle_state.value,
                        "owning_identity": gen.owning_identity,
                    }
                )

    status_dict = {
        "repository": repo,
        "pr_number": pr,
        "repair_state": repair_state,
        "current_epoch": current_epoch,
        "is_exhausted": bool(exhaustion_info and exhaustion_info.is_exhausted),
        "exhausted_blocker_ids": exhausted_ids,
        "open_blocker_ids": open_blocker_ids,
        "blockers": blocker_data,
        "outstanding_generations": outstanding_gens,
    }

    if json_output:
        click.echo(json.dumps(status_dict, indent=2))
        return

    # Formatted human output
    click.echo(f"=== PR Repair Status: {repo} #{pr} ===")
    click.echo(f"Repair State:  {repair_state}")
    click.echo(f"Current Epoch: {current_epoch}")
    click.echo(f"Exhausted:     {status_dict['is_exhausted']}")
    if exhausted_ids:
        click.echo(f"Exhausted Open Blockers: {', '.join(exhausted_ids)}")
    if open_blocker_ids:
        click.echo(f"Open Blockers:           {', '.join(open_blocker_ids)}")

    if blocker_data:
        click.echo("\nBlockers:")
        for b in blocker_data:
            req_str = f" reqs={b['qualified_requirements']}" if b["qualified_requirements"] else ""
            click.echo(f"  - {b['blocker_id']}: status={b['status']} failed={b['total_failed_count']}/{b['limit']} " f"remaining={b['remaining']}{req_str}")

    if outstanding_gens:
        click.echo("\nOutstanding Generations:")
        for g in outstanding_gens:
            click.echo(f"  - {g['generation_id']}: state={g['lifecycle_state']} owner={g['owning_identity']}")

    if repair_state == "EXHAUSTED":
        resume_cmd = f"auto-coder pr-repair resume --repo {repo} --pr {pr} " f"--expected-epoch {current_epoch} --request-id <token>"
        click.echo(f"\nTo resume automatic repair after investigation:\n  {resume_cmd}")


@pr_repair_group.command(name="resume")
@click.option("--repo", required=True, help="Repository in owner/repo form, e.g. acme/widgets")
@click.option("--pr", required=True, type=int, help="Pull request number")
@click.option("--expected-epoch", required=True, type=int, help="Expected current epoch in RepairAllowanceLedger")
@click.option("--request-id", required=True, help="Unique idempotency token for this operator grant")
@click.option("--target-blocker-id", default=None, help="Optional specific canonical blocker ID to grant allowance for")
@click.option("--new-limit", default=None, type=int, help="Optional new failure limit (defaults to configured limit)")
@click.option("--json", "json_output", is_flag=True, help="Output result in JSON format")
def pr_repair_resume(
    repo: str,
    pr: int,
    expected_epoch: int,
    request_id: str,
    target_blocker_id: Optional[str],
    new_limit: Optional[int],
    json_output: bool,
) -> None:
    """Grant fresh repair allowance and schedule immediate re-evaluation (REQ-007)."""
    norm_origin = normalize_api_origin("https://api.github.com")
    allowance_ledger = RepairAllowanceLedger()
    pending_store = PendingWorkStore()

    # Reconcile unfulfilled grant reevaluations if any exist from prior runs (REQ-012)
    reconcile_unfulfilled_grant_reevaluations(allowance_ledger, pending_store)

    effective_limit = new_limit if new_limit is not None else get_pr_repair_max_failed_corrections(repo_name=repo)
    targets = [target_blocker_id] if target_blocker_id else None

    try:
        grant_result = allowance_ledger.operator_grant(
            api_origin=norm_origin,
            repository=repo,
            pr_number=pr,
            request_id=request_id,
            expected_epoch=expected_epoch,
            target_blocker_ids=targets,
            new_limit=effective_limit,
        )
    except (RepairAllowanceError, ValueError) as exc:
        if json_output:
            click.echo(json.dumps({"error": str(exc), "granted": False}))
        else:
            click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    # Schedule immediate re-evaluation in pending work store
    identity = WorkIdentity(repo, f"pr:{pr}", "pr_processing", "")
    obligation = pending_store.schedule_reevaluation(identity, effects=("pr_processing",))

    # Mark re-evaluation delivered
    allowance_ledger.mark_grant_reevaluation_delivered(request_id)

    new_epoch = grant_result.snapshot.epoch if grant_result.snapshot is not None else (expected_epoch + 1)

    # Trace emission (REQ-013)
    get_trace_logger().log(
        "PR Repair Operator Resumption",
        f"Operator resumed repair for PR #{pr} in {repo}",
        item_type="pr",
        item_number=pr,
        details={
            "request_id": request_id,
            "previous_epoch": expected_epoch,
            "new_epoch": new_epoch,
            "granted_blocker_ids": list(grant_result.granted_blocker_ids),
            "work_key": obligation.identity.key(),
        },
    )

    res_dict = {
        "granted": True,
        "repository": repo,
        "pr_number": pr,
        "request_id": request_id,
        "previous_epoch": expected_epoch,
        "new_epoch": new_epoch,
        "granted_blocker_ids": list(grant_result.granted_blocker_ids),
        "scheduled_work_key": obligation.identity.key(),
    }

    if json_output:
        click.echo(json.dumps(res_dict, indent=2))
    else:
        click.echo(f"Successfully resumed automatic repair for PR #{pr} (epoch: {expected_epoch} -> {new_epoch})")
        click.echo(f"Granted blockers: {', '.join(grant_result.granted_blocker_ids)}")
        click.echo(f"Scheduled re-evaluation work: {obligation.identity.key()}")
