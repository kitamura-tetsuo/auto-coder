"""Durable producer for selected local PR CI and conflict repairs.

This boundary admits work only.  It deliberately does not invoke a model,
prepare a checkout, publish a result, or imply that CI/review/merge succeeded.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Callable

from .ci_repair_authority import CIRepairAuthority, evaluate_ci_repair_snapshot
from .local_job_handoff import LocalJobKind, LocalJobOffer, LocalJobRecord, LocalJobStore, PRRepairAdmission
from .local_review_repair import LocalReviewRepairStore
from .pr_repair_dispatch import RepairKind


@dataclass(frozen=True)
class LocalPRRepairOffer:
    repository: str
    pr_number: int
    head_repository: str
    head_ref: str
    head_sha: str
    base_repository: str
    base_ref: str
    base_identity: str
    kind: RepairKind
    backend_name: str
    upstream_attempt: str
    upstream_incarnation: str
    source_evidence_identity: str
    invocation_input: str


OwnerAdmission = Callable[[LocalPRRepairOffer, dict[str, Any]], bool]


def _evidence_identity(values: tuple[str, ...]) -> str:
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def offer_local_pr_repair_job(
    request: LocalPRRepairOffer,
    github_client: Any,
    *,
    owner_admission: OwnerAdmission,
    ci_authority: CIRepairAuthority | None = None,
    conflict_eligible: bool = False,
    store: LocalJobStore | None = None,
    repair_store: LocalReviewRepairStore | None = None,
) -> LocalJobRecord | None:
    """Reconfirm routing/target facts and atomically offer an exact operation."""
    if not request.backend_name or request.head_repository != request.repository or request.base_repository != request.repository:
        return None
    try:
        metadata = github_client.get_pull_request_metadata_strict(request.repository, request.pr_number)
        head = metadata.get("head") or {}
        base = metadata.get("base") or {}
        live_head_repo = str((head.get("repo") or {}).get("full_name") or "")
        live_base_repo = str((base.get("repo") or {}).get("full_name") or "")
        exact = (
            str(head.get("ref") or "") == request.head_ref
            and str(head.get("sha") or "") == request.head_sha
            and live_head_repo == request.head_repository
            and str(base.get("ref") or "") == request.base_ref
            and live_base_repo == request.base_repository
            and str(base.get("sha") or base.get("identity") or "") == request.base_identity
        )
        opened = str(metadata.get("state") or "").lower() == "open"
        unmerged = not bool(metadata.get("merged") or metadata.get("merged_at"))
        locally_routed = owner_admission(request, metadata)
    except Exception:
        return None

    failed: tuple[str, ...] = ()
    complete_failure = False
    if request.kind is RepairKind.CI_FAILURE:
        if ci_authority is None or ci_authority.head_sha != request.head_sha or not ci_authority.allowed or ci_authority.snapshot is None:
            return None
        complete_failure, _ = evaluate_ci_repair_snapshot(ci_authority.snapshot)
        failed = ci_authority.failure_identities
    elif request.kind is RepairKind.MERGE_CONFLICT:
        # Conflict eligibility must come from the caller's final authoritative
        # mergeability/remediation read; CI emptiness is intentionally ignored.
        if not conflict_eligible:
            return None
    else:
        return None

    kind = LocalJobKind.PR_CI_FAILURE if request.kind is RepairKind.CI_FAILURE else LocalJobKind.PR_MERGE_CONFLICT
    payload = json.dumps(asdict(request), sort_keys=True, separators=(",", ":"), default=lambda value: value.value)
    offer = LocalJobOffer(kind, request.repository, request.pr_number, request.upstream_attempt, request.backend_name, payload)
    admission = PRRepairAdmission(
        request.head_repository,
        request.head_ref,
        request.head_sha,
        request.base_repository,
        request.base_ref,
        request.base_identity,
        request.kind.value,
        request.source_evidence_identity,
        request.upstream_incarnation,
        failed,
        opened and exact,
        unmerged,
        locally_routed,
        locally_routed,
        conflict_eligible,
        complete_failure,
    )
    return (store or LocalJobStore()).offer_pr_repair(offer, admission, repair_store)


def local_pr_repair_evidence_identity(
    source_identity: str,
    head_sha: str,
    base_identity: str,
    kind: RepairKind,
    failed_check_identities: tuple[str, ...] = (),
) -> str:
    """Build the stable identity required by :func:`offer_local_pr_repair_job`."""
    return _evidence_identity((source_identity, head_sha, base_identity, kind.value, *failed_check_identities))
