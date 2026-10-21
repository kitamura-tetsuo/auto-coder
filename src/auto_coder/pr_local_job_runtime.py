"""Production composition for durable pull-request local jobs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .local_conflict_repair_job import EditorFactory, LocalConflictRepairJobAdapter, build_conflict_editor
from .local_job_handoff import LocalJobKind, LocalJobStore
from .local_job_runner import LocalJobDomainAdapter
from .pr_correction_job import PRCorrectionJobAdapter, build_captured_executor
from .pr_repair_workspace import PRRepairSource, PRRepairWorkspaceProducer


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
    from .local_ci_repair_job import LocalCIRepairJobPorts, build_local_ci_repair_job_adapter
    from .pr_processor import (
        _is_codex_or_claude_pr,
        _is_dependabot_pr,
        _is_jules_pr,
        _is_local_llm_pr,
        build_durable_ci_correction,
    )

    def conflict_route(_job: object, metadata: dict[str, Any]) -> bool:
        return not (_is_codex_or_claude_pr(metadata) or _is_jules_pr(metadata) or _is_dependabot_pr(metadata))

    conflict = LocalConflictRepairJobAdapter(
        store,
        PRRepairSource((repository_path or Path.cwd()).resolve()),
        # Resolve the strict reader only when a conflict job actually runs.
        # Lightweight engine embeddings that never admit PR repair jobs are
        # not required to implement that production GitHub capability.
        lambda repository, number: github_client.get_pull_request_metadata_strict(repository, number),
        conflict_route,
        conflict_editor_factory,
        workspace_producer=conflict_workspace_producer,
    )
    ci_ports = LocalCIRepairJobPorts(
        source=lambda _job: PRRepairSource(repository_path or Path.cwd()),
        github_client=lambda _job: github_client,
        authority=current_ci_failure_authority,
        local_route=lambda _job, metadata: _is_local_llm_pr(metadata),
        correction=lambda job, prepared: build_durable_ci_correction(job, prepared, github_client, config),
    )
    return {
        LocalJobKind.PR_REVIEW_CORRECTION: PRCorrectionJobAdapter(github_client, build_captured_executor, store),
        LocalJobKind.PR_MERGE_CONFLICT: conflict,
        LocalJobKind.PR_CI_FAILURE: build_local_ci_repair_job_adapter(store, ci_ports),
    }
