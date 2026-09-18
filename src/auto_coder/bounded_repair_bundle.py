"""Bounded correction contract preservation across repair handoff and incremental rereview.

Implements the specification and requirements from GitHub Issue #2139:
Stage S5 of the convergent PR review tracking family (#2134).
Carries the same bounded correction contract through repair and incremental
rereview instead of repeatedly expanding or re-summarizing it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .canonical_pr_blocker_ledger import (
    BlockerDisposition,
    BlockerLedgerSnapshot,
    BlockerSnapshot,
    CorrectionScope,
    QualifiedRequirement,
)
from .logger_config import get_logger
from .util.github_request_outcome import normalize_api_origin

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Exceptions (REQ-003, REQ-009)
# ---------------------------------------------------------------------------


class BoundedBundleError(Exception):
    """Base exception for bounded repair bundle errors."""


class BundleSupersededError(BoundedBundleError):
    """Raised when an existing bundle has been superseded by newer authority/scope."""

    def __init__(self, bundle_id: str, reason: str):
        super().__init__(f"Repair handoff bundle '{bundle_id}' is superseded: {reason}")
        self.bundle_id = bundle_id
        self.reason = reason


class BundleStaleError(BoundedBundleError):
    """Raised when a bundle's head commit, manifest revision, or scope is stale."""

    def __init__(self, bundle_id: str, reason: str):
        super().__init__(f"Repair handoff bundle '{bundle_id}' is stale: {reason}")
        self.bundle_id = bundle_id
        self.reason = reason


class BundleDataAbsentError(BoundedBundleError):
    """Raised when required canonical blocker data is absent from the snapshot."""

    def __init__(self, message: str):
        super().__init__(f"Canonical blocker bundle data is absent: {message}")
        self.message = message


# ---------------------------------------------------------------------------
# Domain Dataclasses (REQ-001, REQ-002, REQ-003, REQ-004)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BoundedBlockerHandoff:
    """A canonical blocker within a bounded repair handoff (REQ-001)."""

    blocker_id: str = ""
    category: str = "IMPLEMENTATION"  # IMPLEMENTATION, TEST_ORACLE, SPECIFICATION, REGRESSION
    authoritative_boundary: str = ""
    qualified_requirements: tuple[QualifiedRequirement, ...] = ()
    requirement_texts: tuple[str, ...] = ()
    original_correction_scope: str = ""
    owned_concern_ids: tuple[str, ...] = ()
    required_corrective_outcome: str = ""
    production_boundary_oracle: str = ""
    reviewed_head_sha: str = ""
    requirement_manifest_revision: str = ""
    current_evidence: str = ""
    evidence_availability: str = "KNOWN"
    is_unmet_prior_correction: bool = False
    unmet_reasons: tuple[str, ...] = ()
    unmet_concern_ids: tuple[str, ...] = ()
    non_authoritative_context: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepairHandoffBundle:
    """Immutable repair handoff bundle with stable identity (REQ-003)."""

    bundle_id: str = ""
    api_origin: str = "https://api.github.com"
    repo_name: str = ""
    pr_number: int = 0
    head_branch: str = ""
    base_branch: str = ""
    reviewed_head_sha: str = ""
    requirement_manifest_revision: str = ""
    original_objective: Optional[str] = None
    blockers: tuple[BoundedBlockerHandoff, ...] = ()
    is_failed_correction: bool = False
    supersedes_bundle_id: Optional[str] = None
    created_at: str = ""
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BundleValidationResult:
    """Outcome of validating a repair bundle against active PR/ledger state."""

    is_valid: bool = True
    is_superseded: bool = False
    reason: Optional[str] = None
    stale_attributes: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Deterministic Identity Hashing (REQ-003)
# ---------------------------------------------------------------------------


def compute_bundle_id(
    api_origin: str,
    repo_name: str,
    pr_number: int,
    head_branch: str,
    base_branch: str,
    reviewed_head_sha: str,
    requirement_manifest_revision: str,
    blockers: Sequence[BoundedBlockerHandoff],
    original_objective: Optional[str] = None,
) -> str:
    """Compute deterministic, collision-resistant bundle identity."""
    normalized_origin = normalize_api_origin(api_origin)
    sorted_blockers = sorted(blockers, key=lambda b: b.blocker_id)
    blocker_entries = []
    for b in sorted_blockers:
        blocker_entries.append(
            {
                "blocker_id": b.blocker_id,
                "category": b.category,
                "authoritative_boundary": b.authoritative_boundary,
                "qualified_requirements": [{"issue_number": qr.issue_number, "requirement_id": qr.requirement_id} for qr in b.qualified_requirements],
                "original_correction_scope": b.original_correction_scope,
                "owned_concern_ids": list(b.owned_concern_ids),
                "required_corrective_outcome": b.required_corrective_outcome,
                "production_boundary_oracle": b.production_boundary_oracle,
                "reviewed_head_sha": b.reviewed_head_sha,
                "requirement_manifest_revision": b.requirement_manifest_revision,
                "is_unmet_prior_correction": b.is_unmet_prior_correction,
                "unmet_concern_ids": list(b.unmet_concern_ids),
            }
        )

    canonical_payload = json.dumps(
        {
            "api_origin": normalized_origin,
            "repo_name": repo_name,
            "pr_number": pr_number,
            "head_branch": head_branch,
            "base_branch": base_branch,
            "reviewed_head_sha": reviewed_head_sha,
            "requirement_manifest_revision": requirement_manifest_revision,
            "original_objective": original_objective or "",
            "blockers": blocker_entries,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
    return f"bnd_{digest[:16]}"


# ---------------------------------------------------------------------------
# Bundle Construction (REQ-001, REQ-002, REQ-004)
# ---------------------------------------------------------------------------


def build_repair_handoff_bundle(
    snapshot: BlockerLedgerSnapshot,
    repo_name: str,
    pr_number: int,
    head_branch: str,
    base_branch: str,
    reviewed_head_sha: str,
    requirement_manifest_revision: str,
    *,
    api_origin: str = "https://api.github.com",
    target_blocker_ids: Optional[Sequence[str]] = None,
    original_objective: Optional[str] = None,
    requirement_texts: Optional[Dict[str, str]] = None,
    failed_corrections: Optional[Dict[str, Tuple[Sequence[str], Sequence[str]]]] = None,
    non_authoritative_contexts: Optional[Dict[str, Sequence[str]]] = None,
    supersedes_bundle_id: Optional[str] = None,
) -> RepairHandoffBundle:
    """Build an immutable repair handoff bundle from the canonical blocker snapshot.

    REQ-001: Build repair inputs from canonical blocker snapshot rather than
    unstructured concatenation of historical roots.
    REQ-002: Preserve fixed Objective verbatim and keep Requirements as sole
    implementation contract; keep examples, suggested techniques, and history
    separately identified as non-authoritative context.
    REQ-004: After unsuccessful corrective generation, communicate which
    original blocker concerns remain unmet and why submitted changes fail.
    """
    normalized_origin = normalize_api_origin(api_origin)
    req_texts = requirement_texts or {}
    failed_info = failed_corrections or {}
    contexts = non_authoritative_contexts or {}

    # Identify target blockers
    if target_blocker_ids is not None:
        target_ids_set = set(target_blocker_ids)
        available_blockers = [b for b in snapshot.blockers if b.blocker_id in target_ids_set]
        missing_ids = target_ids_set - {b.blocker_id for b in available_blockers}
        if missing_ids:
            raise BundleDataAbsentError(f"Blocker IDs not found in ledger snapshot: {sorted(missing_ids)}")
    else:
        available_blockers = list(snapshot.get_open_blockers())

    if not available_blockers:
        raise BundleDataAbsentError(f"No open canonical blockers available for PR #{pr_number}")

    # Determine objective
    inferred_objective = original_objective
    if inferred_objective is None:
        for b in available_blockers:
            if b.original_objective_anchor:
                inferred_objective = b.original_objective_anchor
                break

    handoff_blockers: List[BoundedBlockerHandoff] = []
    has_failed_correction = False

    for b in available_blockers:
        # Resolve qualified requirement texts
        texts: List[str] = []
        for qr in b.qualified_requirements:
            t = req_texts.get(qr.requirement_id) or req_texts.get(f"Issue #{qr.issue_number}: {qr.requirement_id}") or ""
            texts.append(t)

        # Oracle info
        oracle_info = b.evidence_needed
        if b.incorrect_behavior_or_missing_invariant:
            if oracle_info:
                oracle_info = f"{oracle_info} (Invariant / Counterexample: {b.incorrect_behavior_or_missing_invariant})"
            else:
                oracle_info = b.incorrect_behavior_or_missing_invariant

        # Check for failed prior correction on this blocker
        is_unmet = False
        unmet_reasons: Tuple[str, ...] = ()
        unmet_concerns: Tuple[str, ...] = ()
        if b.blocker_id in failed_info:
            is_unmet = True
            has_failed_correction = True
            concerns, reasons = failed_info[b.blocker_id]
            unmet_concerns = tuple(concerns) if concerns else b.concern_ids
            unmet_reasons = tuple(reasons)

        non_auth = tuple(contexts.get(b.blocker_id, ()))

        # Get latest transition evidence if available
        current_ev = ""
        ev_avail = "KNOWN"
        if b.transitions:
            latest_tr = b.transitions[-1]
            current_ev = latest_tr.evidence
            ev_avail = latest_tr.evidence_availability.value

        handoff = BoundedBlockerHandoff(
            blocker_id=b.blocker_id,
            category=b.category,
            authoritative_boundary=b.authoritative_boundary,
            qualified_requirements=b.qualified_requirements,
            requirement_texts=tuple(texts),
            original_correction_scope=b.accepted_scope.description,
            owned_concern_ids=b.concern_ids,
            required_corrective_outcome=b.required_correction_outcome,
            production_boundary_oracle=oracle_info,
            reviewed_head_sha=reviewed_head_sha,
            requirement_manifest_revision=requirement_manifest_revision,
            current_evidence=current_ev,
            evidence_availability=ev_avail,
            is_unmet_prior_correction=is_unmet,
            unmet_reasons=unmet_reasons,
            unmet_concern_ids=unmet_concerns,
            non_authoritative_context=non_auth,
        )
        handoff_blockers.append(handoff)

    bundle_id = compute_bundle_id(
        api_origin=normalized_origin,
        repo_name=repo_name,
        pr_number=pr_number,
        head_branch=head_branch,
        base_branch=base_branch,
        reviewed_head_sha=reviewed_head_sha,
        requirement_manifest_revision=requirement_manifest_revision,
        blockers=handoff_blockers,
        original_objective=inferred_objective,
    )

    created_at = datetime.now(timezone.utc).isoformat()
    return RepairHandoffBundle(
        bundle_id=bundle_id,
        api_origin=normalized_origin,
        repo_name=repo_name,
        pr_number=pr_number,
        head_branch=head_branch,
        base_branch=base_branch,
        reviewed_head_sha=reviewed_head_sha,
        requirement_manifest_revision=requirement_manifest_revision,
        original_objective=inferred_objective,
        blockers=tuple(handoff_blockers),
        is_failed_correction=has_failed_correction,
        supersedes_bundle_id=supersedes_bundle_id,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# Bundle Validation and Supersession (REQ-003, AS-004)
# ---------------------------------------------------------------------------


def validate_repair_handoff_bundle(
    bundle: RepairHandoffBundle,
    current_head_sha: str,
    current_manifest_revision: str,
    snapshot: Optional[BlockerLedgerSnapshot] = None,
) -> BundleValidationResult:
    """Validate that a repair bundle is fresh and matches current authority.

    REQ-003: Retain exact bundle for subsequent comparison; when current scope
    or authority changes, explicitly supersede/reconcile the bundle rather than
    silently changing the meaning of an existing handoff.
    AS-004: A stale bundle cannot silently acquire new content under the same ID;
    the production sender is given an explicit supersession/refusal.
    """
    stale_attrs: List[str] = []
    reasons: List[str] = []

    if bundle.reviewed_head_sha != current_head_sha:
        stale_attrs.append("reviewed_head_sha")
        reasons.append(f"reviewed head '{bundle.reviewed_head_sha[:8]}' does not match current PR head '{current_head_sha[:8]}'")

    if bundle.requirement_manifest_revision != current_manifest_revision:
        stale_attrs.append("requirement_manifest_revision")
        reasons.append(f"manifest revision '{bundle.requirement_manifest_revision}' does not match current revision '{current_manifest_revision}'")

    if snapshot is not None:
        for b in bundle.blockers:
            snap_blocker = snapshot.get_blocker(b.blocker_id)
            if snap_blocker is None:
                stale_attrs.append(f"blocker:{b.blocker_id}:absent")
                reasons.append(f"blocker '{b.blocker_id}' is no longer present in ledger snapshot")
            elif snap_blocker.disposition not in (BlockerDisposition.OPEN, BlockerDisposition.RECURRENCE):
                stale_attrs.append(f"blocker:{b.blocker_id}:closed")
                reasons.append(f"blocker '{b.blocker_id}' has transitioned to disposition '{snap_blocker.disposition.value}'")
            elif snap_blocker.accepted_scope.description != b.original_correction_scope:
                stale_attrs.append(f"blocker:{b.blocker_id}:scope_rebound")
                reasons.append(f"blocker '{b.blocker_id}' accepted scope was rebound from '{b.original_correction_scope}' to '{snap_blocker.accepted_scope.description}'")

    if stale_attrs:
        return BundleValidationResult(
            is_valid=False,
            is_superseded=True,
            reason="; ".join(reasons),
            stale_attributes=tuple(stale_attrs),
        )

    return BundleValidationResult(is_valid=True, is_superseded=False, reason=None, stale_attributes=())


def reconcile_or_supersede_bundle(
    existing_bundle: RepairHandoffBundle,
    snapshot: BlockerLedgerSnapshot,
    new_head_sha: str,
    new_manifest_revision: str,
    *,
    failed_corrections: Optional[Dict[str, Tuple[Sequence[str], Sequence[str]]]] = None,
    requirement_texts: Optional[Dict[str, str]] = None,
    non_authoritative_contexts: Optional[Dict[str, Sequence[str]]] = None,
) -> RepairHandoffBundle:
    """Explicitly supersede an existing bundle with updated authority/scope (REQ-003)."""
    target_ids = [b.blocker_id for b in existing_bundle.blockers]
    # Filter to blockers still open in snapshot
    open_target_ids: list[str] = []
    for bid in target_ids:
        b = snapshot.get_blocker(bid)
        if b is not None and b.disposition in (BlockerDisposition.OPEN, BlockerDisposition.RECURRENCE):
            open_target_ids.append(bid)

    new_bundle = build_repair_handoff_bundle(
        snapshot=snapshot,
        repo_name=existing_bundle.repo_name,
        pr_number=existing_bundle.pr_number,
        head_branch=existing_bundle.head_branch,
        base_branch=existing_bundle.base_branch,
        reviewed_head_sha=new_head_sha,
        requirement_manifest_revision=new_manifest_revision,
        api_origin=existing_bundle.api_origin,
        target_blocker_ids=open_target_ids,
        original_objective=existing_bundle.original_objective,
        requirement_texts=requirement_texts,
        failed_corrections=failed_corrections,
        non_authoritative_contexts=non_authoritative_contexts,
        supersedes_bundle_id=existing_bundle.bundle_id,
    )
    return new_bundle


# ---------------------------------------------------------------------------
# Structured Payload Rendering (REQ-001, REQ-002, REQ-004, REQ-008)
# ---------------------------------------------------------------------------


def render_bounded_repair_payload(bundle: RepairHandoffBundle) -> str:
    """Render structured prompt payload enforcing bounded correction contracts.

    REQ-001: Controller-owned blocker identity, qualified requirement references and text,
             original correction scope, owned concern IDs, required outcome, oracle info.
    REQ-002: Fixed Objective verbatim, Requirements as sole implementation contract,
             non-authoritative context explicitly separated.
    REQ-004: Communicate unmet concerns and explanation of failed prior generation.
    REQ-008: Production-boundary coverage requirement, inability reporting.
    """
    sections: List[str] = []

    # Bundle header
    sections.append(f"BOUNDED REPAIR HANDOFF BUNDLE: `{bundle.bundle_id}`")
    sections.append(f"Target PR: #{bundle.pr_number} in `{bundle.repo_name}` (branch: `{bundle.head_branch}` -> `{bundle.base_branch}`)\n" f"Reviewed Head SHA: `{bundle.reviewed_head_sha}`\n" f"Requirement Manifest Revision: `{bundle.requirement_manifest_revision}`")

    if bundle.supersedes_bundle_id:
        sections.append(f"Explicitly supersedes previous bundle: `{bundle.supersedes_bundle_id}`")

    # Fixed Objective verbatim (REQ-002)
    if bundle.original_objective:
        sections.append("## FIXED OBJECTIVE (IMMUTABLE SPECIFICATION SCOPE EVIDENCE)\n" f"{bundle.original_objective.strip()}\n\n" "Directive: Preserve this Objective verbatim. It defines scope evidence only; explicit Requirements below are the sole implementation contract.")

    # Sole Implementation Contract (REQ-002)
    contract_lines: List[str] = ["## AUTHORITATIVE REQUIREMENTS (SOLE IMPLEMENTATION CONTRACT)"]
    seen_reqs = set()
    for b in bundle.blockers:
        for qr, text in zip(b.qualified_requirements, b.requirement_texts):
            req_key = f"{qr.issue_number}:{qr.requirement_id}"
            if req_key not in seen_reqs:
                seen_reqs.add(req_key)
                if text:
                    contract_lines.append(f"- Issue #{qr.issue_number} `{qr.requirement_id}`: {text.strip()}")
                else:
                    contract_lines.append(f"- Issue #{qr.issue_number} `{qr.requirement_id}`")
    sections.append("\n".join(contract_lines))

    # Actionable Canonical Blockers (REQ-001, REQ-004, REQ-008)
    blocker_lines: List[str] = ["## ACTIONABLE CANONICAL BLOCKERS"]
    if bundle.is_failed_correction:
        blocker_lines.append("NOTICE: The latest corrective attempt did not resolve the following actionable feedback.\n" "This finding was delivered for an earlier remediation attempt, but independent validation\n" "established that it remains open. Further correction is required.\n")

    for idx, b in enumerate(bundle.blockers, 1):
        blocker_header = f"### Blocker {idx}: `{b.blocker_id}` ({b.category})"
        b_lines = [blocker_header]
        b_lines.append(f"- **Authoritative Boundary:** `{b.authoritative_boundary}`")
        req_refs = ", ".join(f"Issue #{qr.issue_number} `{qr.requirement_id}`" for qr in b.qualified_requirements)
        b_lines.append(f"- **Qualified Requirements:** {req_refs}")
        b_lines.append(f"- **Original Accepted Scope:** {b.original_correction_scope}")
        b_lines.append(f"- **Owned Concern IDs:** {', '.join(f'`{cid}`' for cid in b.owned_concern_ids)}")
        b_lines.append(f"- **Required Corrective Outcome:** {b.required_corrective_outcome}")
        b_lines.append(f"- **Production-Boundary Oracle Info:** {b.production_boundary_oracle}")

        if b.is_unmet_prior_correction:
            b_lines.append("\n**UNMET PRIOR CORRECTION DETAILS (REQ-004):**")
            b_lines.append(f"- Remaining Unmet Concerns: {', '.join(f'`{cid}`' for cid in b.unmet_concern_ids)}")
            for reason in b.unmet_reasons:
                b_lines.append(f"- Failure Rationale: {reason}")
            b_lines.append("- Defect Warning: A pass-body, renamed test, green helper test, source-text assertion, " "or documentation claim does not prove completion. Observable production-boundary coverage is required.")

        blocker_lines.append("\n".join(b_lines))

    sections.append("\n\n".join(blocker_lines))

    # Non-authoritative Context explicitly separated (REQ-002)
    has_non_auth = any(b.non_authoritative_context for b in bundle.blockers)
    if has_non_auth:
        non_auth_lines = [
            "## NON-AUTHORITATIVE CONTEXT (INFORMATION ONLY — NOT IMPLEMENTATION CONTRACT)",
            "Directive: The following context is non-authoritative. Suggested techniques, examples, reviewer prose, " "and implementation-agent claims illustrate ideas but do NOT create unstated obligations or replace explicit Requirements.\n",
        ]
        for b in bundle.blockers:
            if b.non_authoritative_context:
                non_auth_lines.append(f"### Context for `{b.blocker_id}`:")
                for item in b.non_authoritative_context:
                    non_auth_lines.append(f"- {item.strip()}")
        sections.append("\n".join(non_auth_lines))

    # Implementation Directives (REQ-008)
    directives = [
        "## BOUNDED IMPLEMENTATION DIRECTIVES (REQ-008)",
        "- Apply focused code changes and committed regression tests strictly to satisfy the authoritative Requirements and resolve the canonical blockers above.",
        "- For test-oracle gaps: add only focused committed regression protection crossing the supported production boundary; do NOT modify production code unless a separate demonstrated violation requires it.",
        "- Do NOT enlarge the correction to unrelated improvements or redesign unrelated subsystems.",
        "- If you are unable to deterministically complete the correction per the contract, report inability with `CANNOT_FIX` rather than fabricating coverage or resolving review threads.",
        "- Do NOT resolve, close, or mark GitHub review threads as resolved yourself; leave that decision to Auto-Coder's independent validation.",
    ]
    sections.append("\n".join(directives))

    return "\n\n---\n\n".join(sections)
