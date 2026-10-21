"""Production composition for durable pull-request local jobs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .local_conflict_repair_job import EditorFactory, LocalConflictRepairJobAdapter, build_conflict_editor
from .local_job_handoff import LocalJobKind, LocalJobStore
from .local_job_runner import LocalJobDomainAdapter
from .pr_correction_job import PRCorrectionJobAdapter, build_captured_executor
from .pr_repair_workspace import PRRepairSource, PRRepairWorkspaceProducer


def build_pr_repair_finalizer(
    github_client: Any,
    store: LocalJobStore,
    wake: Callable[[str, int, str], bool],
    implementation_slots: Any | None = None,
):
    """Compose the exact-head publisher from production GitHub and wake ports."""
    from .pr_repair_finalizer import PRRepairJobFinalizer, PRRepairTargetObservation

    def observe(repository: str, number: int) -> PRRepairTargetObservation:
        metadata = github_client.get_pull_request_metadata_strict(repository, number)
        head = metadata.get("head") or {}
        base = metadata.get("base") or {}
        return PRRepairTargetObservation(
            repository,
            number,
            str((head.get("repo") or {}).get("full_name") or ""),
            str(head.get("ref") or ""),
            str(head.get("sha") or ""),
            str((base.get("repo") or {}).get("full_name") or ""),
            str(base.get("ref") or ""),
            str(base.get("sha") or base.get("identity") or ""),
            str(metadata.get("state") or "").lower() == "open",
            bool(metadata.get("merged") or metadata.get("merged_at")),
            True,
        )

    finish_execution = None
    if implementation_slots is not None:
        finish_execution = lambda record: implementation_slots.finish_execution_by_id(record.implementation_execution_id)
    return PRRepairJobFinalizer(store, observe, wake, finish_execution=finish_execution)


def build_pr_local_job_adapters(
    github_client: Any,
    config: Any,
    store: LocalJobStore,
    *,
    repository_path: Path | None = None,
    conflict_editor_factory: EditorFactory = build_conflict_editor,
    conflict_workspace_producer: PRRepairWorkspaceProducer | None = None,
) -> dict[LocalJobKind, LocalJobDomainAdapter]:
    """Register every PR job kind executed by the production daemon."""
    from .ci_repair_authority import current_ci_failure_authority
    from .cloud_manager import CloudManager
    from .codex_pr_attribution import AttributionDisposition, CodexPrAttributionRepository
    from .local_ci_repair_job import LocalCIRepairJobPorts, build_local_ci_repair_job_adapter
    from .pr_processor import (
        _is_codex_or_claude_pr,
        _is_dependabot_pr,
        _is_jules_pr,
        _is_local_llm_pr,
        build_durable_ci_correction,
    )
    from .pr_repair_guard import check_pr_repair_exhaustion

    def explicit_local_checkout(job: Any) -> bool:
        try:
            payload = json.loads(job.invocation_input)
        except (TypeError, ValueError):
            return False
        return isinstance(payload, dict) and payload.get("explicit_local_checkout") is True

    def conflict_route(_job: object, metadata: dict[str, Any]) -> bool:
        return not (_is_codex_or_claude_pr(metadata) or _is_jules_pr(metadata) or _is_dependabot_pr(metadata))

    def conflict_authority(job: Any, _metadata: dict[str, Any]) -> bool:
        """Refresh every revocable execution-time conflict authority."""
        try:
            if not bool(config.ENABLE_MERGEABILITY_REMEDIATION):
                return False
            exhaustion = check_pr_repair_exhaustion(job.repository, job.target_number, require_authoritative_snapshot=True)
            if exhaustion is not None and exhaustion.is_exhausted:
                return False
            if CloudManager(job.repository).read_bindings_strict().get(str(job.target_number)) is not None:
                return False
            attribution = CodexPrAttributionRepository(job.repository).get(job.target_number)
            return attribution.disposition is AttributionDisposition.UNRESOLVED
        except Exception:
            return False

    conflict = LocalConflictRepairJobAdapter(
        store,
        PRRepairSource((repository_path or Path.cwd()).resolve()),
        # Resolve the strict reader only when a conflict job actually runs.
        # Lightweight engine embeddings that never admit PR repair jobs are
        # not required to implement that production GitHub capability.
        lambda repository, number: github_client.get_pull_request_metadata_strict(repository, number),
        conflict_route,
        conflict_authority,
        conflict_editor_factory,
        workspace_producer=conflict_workspace_producer,
    )
    ci_ports = LocalCIRepairJobPorts(
        source=lambda _job: PRRepairSource(repository_path or Path.cwd()),
        github_client=lambda _job: github_client,
        authority=current_ci_failure_authority,
        local_route=lambda job, metadata: explicit_local_checkout(job) or _is_local_llm_pr(metadata),
        correction=lambda job, prepared: build_durable_ci_correction(job, prepared, github_client, config),
    )
    return {
        LocalJobKind.PR_REVIEW_CORRECTION: PRCorrectionJobAdapter(github_client, build_captured_executor, store),
        LocalJobKind.PR_MERGE_CONFLICT: conflict,
        LocalJobKind.PR_CI_FAILURE: build_local_ci_repair_job_adapter(store, ci_ports),
    }
