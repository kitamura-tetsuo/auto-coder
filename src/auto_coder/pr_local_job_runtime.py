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
    store: LocalJobStore,
    *,
    repository_path: Path | None = None,
    conflict_editor_factory: EditorFactory = build_conflict_editor,
    conflict_workspace_producer: PRRepairWorkspaceProducer | None = None,
) -> dict[LocalJobKind, LocalJobDomainAdapter]:
    """Register every PR job kind executed by the production daemon."""
    from .pr_processor import _is_codex_or_claude_pr, _is_dependabot_pr, _is_jules_pr

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
    return {
        LocalJobKind.PR_REVIEW_CORRECTION: PRCorrectionJobAdapter(github_client, build_captured_executor, store),
        LocalJobKind.PR_MERGE_CONFLICT: conflict,
    }
