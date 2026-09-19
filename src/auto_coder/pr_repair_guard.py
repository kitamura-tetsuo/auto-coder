"""PR repair exhaustion guard and operator resumption coordinator.

Implements requirements from GitHub Issue #2142:
- Stop automatic repair across all production origins when allowance is exhausted.
- Enforce refusal at admission/outbound boundaries as well as normal processor routing.
- Keep exhaustion orthogonal to semantic and merge state.
- Expose actionable human summary and deduplicated notification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from .canonical_pr_blocker_ledger import BlockerDisposition, CanonicalPRBlockerLedger
from .durable_repair_allowance import (
    OUTSTANDING_LIFECYCLE_STATES,
    GenerationLifecycleState,
    RepairAllowanceLedger,
    RepairAllowanceStatus,
)
from .logger_config import get_logger
from .util.github_request_outcome import normalize_api_origin

logger = get_logger(__name__)

EXHAUSTION_COMMENT_MARKER = "<!-- auto-coder:pr-repair-exhausted -->"


@dataclass(frozen=True)
class BlockerAllowanceInfo:
    """Read-only allowance snapshot details for a single canonical blocker."""

    blocker_id: str = ""
    status: str = "ALLOWABLE"
    total_failed_count: int = 0
    active_limit: int = 3
    remaining: int = 3
    historical_unknown: bool = False
    exhaustion_reason: str = ""
    qualified_requirements: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class PrRepairExhaustionInfo:
    """Inspection and diagnostic information for a PR's repair allowance state."""

    is_exhausted: bool = False
    repo_name: str = ""
    pr_number: int = 0
    current_epoch: int = 0
    exhausted_blocker_ids: tuple[str, ...] = ()
    open_blocker_ids: tuple[str, ...] = ()
    blockers: tuple[BlockerAllowanceInfo, ...] = ()
    outstanding_generation_ids: tuple[str, ...] = ()
    reason: str = ""
    machine_readable_reason: str = "AUTO_REPAIR_EXHAUSTED"
    resume_command: str = ""
    repair_state: str = "ALLOWABLE"


def check_pr_repair_exhaustion(
    repo_name: str,
    pr_number: int,
    api_origin: str = "https://api.github.com",
    allowance_ledger: Optional[RepairAllowanceLedger] = None,
    blocker_ledger: Optional[CanonicalPRBlockerLedger] = None,
) -> Optional[PrRepairExhaustionInfo]:
    """Check whether a pull request has an active repair exhaustion hold.

    Returns PrRepairExhaustionInfo if any open blocker has exhausted its captured
    allowance; returns None if repair is allowable or unconstrained.
    """
    norm_origin = normalize_api_origin(api_origin)
    b_ledger = blocker_ledger or CanonicalPRBlockerLedger()
    a_ledger = allowance_ledger or RepairAllowanceLedger()

    try:
        b_snapshot = b_ledger.get_snapshot(norm_origin, repo_name, pr_number, require_retained_state=False)
    except Exception as exc:
        logger.debug(f"Could not load canonical blocker ledger snapshot for {repo_name}#{pr_number}: {exc}")
        b_snapshot = None

    try:
        a_snapshot = a_ledger.get_snapshot(norm_origin, repo_name, pr_number, require_retained_state=False)
    except Exception as exc:
        logger.debug(f"Could not load repair allowance snapshot for {repo_name}#{pr_number}: {exc}")
        a_snapshot = None

    if a_snapshot is None:
        return None

    open_blockers = b_snapshot.get_open_blockers() if b_snapshot else ()
    open_ids = {b.blocker_id for b in open_blockers}

    # If canonical blocker ledger has no blockers registered yet, fall back to allowance blockers
    if not open_ids and not b_snapshot:
        open_ids = {b.blocker_id for b in a_snapshot.blockers}

    blocker_infos: list[BlockerAllowanceInfo] = []
    exhausted_ids: list[str] = []
    has_reconciliation_required = False

    b_map = {b.blocker_id: b for b in b_snapshot.blockers} if b_snapshot else {}

    for b_allowance in a_snapshot.blockers:
        bid = b_allowance.blocker_id
        b_snap = b_map.get(bid)
        q_reqs = tuple((qr.issue_number, qr.requirement_id) for qr in b_snap.qualified_requirements) if b_snap else ()
        b_info = BlockerAllowanceInfo(
            blocker_id=bid,
            status=b_allowance.status.value,
            total_failed_count=b_allowance.total_failed_count,
            active_limit=b_allowance.limit,
            remaining=b_allowance.remaining,
            historical_unknown=bool(b_allowance.historical_unknown),
            exhaustion_reason=b_allowance.exhaustion_reason or "",
            qualified_requirements=q_reqs,
        )
        blocker_infos.append(b_info)
        if bid in open_ids and b_allowance.status == RepairAllowanceStatus.EXHAUSTED:
            exhausted_ids.append(bid)
        if b_allowance.status == RepairAllowanceStatus.RECONCILIATION_REQUIRED:
            has_reconciliation_required = True

    outstanding_gids = tuple(g.generation_id for g in a_snapshot.generations if g.is_outstanding())

    is_exhausted = len(exhausted_ids) > 0
    if not is_exhausted:
        return None

    repair_state = "EXHAUSTED"
    reason = f"PR #{pr_number} has exhausted its automatic repair allowance ({a_snapshot.blockers[0].limit if a_snapshot.blockers else 3} attempts) " f"for open blocker(s): {', '.join(sorted(exhausted_ids))}"
    resume_cmd = f"auto-coder pr-repair resume --repo {repo_name} --pr {pr_number} " f"--expected-epoch {a_snapshot.epoch} --request-id <token>"

    return PrRepairExhaustionInfo(
        is_exhausted=True,
        repo_name=repo_name,
        pr_number=pr_number,
        current_epoch=a_snapshot.epoch,
        exhausted_blocker_ids=tuple(sorted(exhausted_ids)),
        open_blocker_ids=tuple(sorted(open_ids)),
        blockers=tuple(blocker_infos),
        outstanding_generation_ids=outstanding_gids,
        reason=reason,
        machine_readable_reason="AUTO_REPAIR_EXHAUSTED",
        resume_command=resume_cmd,
        repair_state=repair_state,
    )


def format_exhaustion_comment(info: PrRepairExhaustionInfo) -> str:
    """Format an actionable human-facing exhaustion notice without provider instruction."""
    blocker_lines = []
    for bid in info.exhausted_blocker_ids:
        b_info = next((b for b in info.blockers if b.blocker_id == bid), None)
        req_str = ""
        if b_info and b_info.qualified_requirements:
            req_str = f" (Requirements: {', '.join(f'#{iss} {rid}' for iss, rid in b_info.qualified_requirements)})"
        count_str = f" [{b_info.total_failed_count}/{b_info.active_limit} failed]" if b_info else ""
        blocker_lines.append(f"- `{bid}`{count_str}{req_str}")

    blockers_text = "\n".join(blocker_lines) if blocker_lines else "- (unidentified blockers)"

    return (
        f"{EXHAUSTION_COMMENT_MARKER}\n"
        f"### 🛑 Auto-Coder: Automatic Repair Exhausted\n\n"
        f"Automatic repair for PR #{info.pr_number} has stopped because its correction allowance has been exhausted.\n\n"
        f"**Exhausted Open Blocker(s):**\n"
        f"{blockers_text}\n\n"
        f"Unresolved review blockers and merge protections remain intact. Automatic merge will not proceed.\n\n"
        f"**To resume automatic repair after manual investigation, run:**\n"
        f"```bash\n"
        f"{info.resume_command}\n"
        f"```"
    )


def publish_exhaustion_comment_deduped(
    github_client: Any,
    repo_name: str,
    pr_number: int,
    info: PrRepairExhaustionInfo,
) -> bool:
    """Publish the exhaustion notice comment once per PR without repeating (REQ-011)."""
    if not github_client:
        return False
    try:
        comments = github_client.get_pr_comments(repo_name, pr_number)
        for comment in comments:
            body = getattr(comment, "body", "") or (comment.get("body", "") if isinstance(comment, dict) else "")
            if EXHAUSTION_COMMENT_MARKER in body:
                logger.info(f"Exhaustion notice already published for PR #{pr_number}; skipping duplicate comment")
                return False

        comment_body = format_exhaustion_comment(info)
        github_client.add_pr_comment(repo_name, pr_number, comment_body)
        logger.info(f"Published automatic repair exhaustion notice for PR #{pr_number}")
        return True
    except Exception as exc:
        logger.warning(f"Could not publish exhaustion comment for PR #{pr_number}: {exc}")
        return False
