"""Publication-free durable execution of an admitted local conflict repair."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .conflict_resolver import ConflictInspection, inspect_conflicts
from .local_job_handoff import InvocationOutcome, LocalJobClaim, LocalJobKind, LocalJobRecord, LocalJobResultArtifact, LocalJobStore
from .local_job_runner import LocalJobExecutionResult, LocalJobProviderEntryRefused
from .pr_repair_workspace import PRRepairSource, PRRepairWorkspaceError, PRRepairWorkspaceProducer
from .prompt_loader import render_prompt

MetadataReader = Callable[[str, int], dict[str, Any]]
RouteAuthority = Callable[[LocalJobRecord, dict[str, Any]], bool]
RemediationAuthority = Callable[[LocalJobRecord, dict[str, Any]], bool]
ConflictEditor = Callable[[str, Path], str]
EditorFactory = Callable[[str, LocalJobRecord], ConflictEditor]


@dataclass(frozen=True)
class MergePreparation:
    conflicted: bool
    inspection: ConflictInspection


def _classify_editor_output(response: str) -> InvocationOutcome:
    """Accept only the backend's unambiguous success protocol."""
    stripped = response.strip()
    lines = tuple(line.strip() for line in stripped.splitlines() if line.strip())
    if not stripped:
        return InvocationOutcome.FAILED
    if "CANNOT_FIX" in stripped.upper():
        return InvocationOutcome.CANNOT_FIX
    if len(lines) != 1 or not lines[0].startswith("ACTION_SUMMARY:") or not lines[0][len("ACTION_SUMMARY:") :].strip():
        return InvocationOutcome.FAILED
    return InvocationOutcome.COMPLETED


class LocalConflictRepairJobAdapter:
    """Turn a retained H/B claim into immutable, nonpublishing result evidence."""

    def __init__(
        self,
        store: LocalJobStore,
        source: PRRepairSource,
        metadata_reader: MetadataReader,
        route_authority: RouteAuthority,
        remediation_authority: RemediationAuthority,
        editor_factory: EditorFactory,
        *,
        workspace_producer: PRRepairWorkspaceProducer | None = None,
    ) -> None:
        self._store = store
        self._source = source
        self._metadata_reader = metadata_reader
        self._route_authority = route_authority
        self._remediation_authority = remediation_authority
        self._editor_factory = editor_factory
        self._workspaces = workspace_producer or PRRepairWorkspaceProducer(store)

    def _authorized(self, job: LocalJobRecord) -> tuple[bool, dict[str, Any]]:
        authority = self._store.get_pr_repair_claim(job.job_id)
        if authority is None or job.kind is not LocalJobKind.PR_MERGE_CONFLICT:
            return False, {}
        try:
            metadata = self._metadata_reader(job.repository, job.target_number)
            head, base = metadata.get("head") or {}, metadata.get("base") or {}
            exact = (
                str((head.get("repo") or {}).get("full_name") or "") == authority.head_repository
                and str(head.get("ref") or "") == authority.head_ref
                and str(head.get("sha") or "") == authority.head_sha
                and str((base.get("repo") or {}).get("full_name") or "") == authority.base_repository
                and str(base.get("ref") or "") == authority.base_ref
                and str(base.get("sha") or base.get("identity") or "") == authority.base_identity
            )
            live = str(metadata.get("state") or "").lower() == "open" and not bool(metadata.get("merged") or metadata.get("merged_at"))
            return live and exact and self._route_authority(job, metadata) and self._remediation_authority(job, metadata), metadata
        except Exception:
            return False, {}

    def authorize_provider_entry(self, job: LocalJobRecord) -> bool:
        allowed, _ = self._authorized(job)
        return allowed

    @staticmethod
    def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)

    def _prepare_merge(self, root: Path, base_sha: str) -> MergePreparation:
        # A merge writes identity-bearing metadata even with --no-commit on
        # some Git versions. Keep the deterministic controller identity local
        # to this invocation instead of mutating repository configuration.
        merge = self._git(
            root,
            "-c",
            "user.name=Auto-Coder",
            "-c",
            "user.email=auto-coder@localhost",
            "merge",
            "--no-commit",
            "--no-ff",
            base_sha,
        )
        merge_head = self._git(root, "rev-parse", "--verify", "MERGE_HEAD^{commit}")
        if merge_head.returncode or merge_head.stdout.strip() != base_sha:
            detail = merge.stderr.strip() or merge_head.stderr.strip()
            raise PRRepairWorkspaceError(f"merge attempt lacks retained base lineage: {detail}")
        inspection = inspect_conflicts(root)
        if not inspection.complete:
            raise PRRepairWorkspaceError(inspection.reason)
        conflicted = bool(inspection.unmerged_paths)
        if merge.returncode and not conflicted:
            raise PRRepairWorkspaceError("merge failed without an observable conflict")
        if conflicted:
            staged = self._git(root, "add", "-A")
            if staged.returncode:
                raise PRRepairWorkspaceError(staged.stderr.strip() or "failed to retain conflict snapshot")
            staged_inspection = inspect_conflicts(root)
            if not staged_inspection.complete or not staged_inspection.marker_paths:
                raise PRRepairWorkspaceError("staged conflict snapshot lost its marker contents")
            inspection = staged_inspection
        return MergePreparation(conflicted, inspection)

    @staticmethod
    def _prompt(job: LocalJobRecord, metadata: dict[str, Any], conflict_info: str) -> str:
        base = metadata.get("base") or {}
        return render_prompt(
            "pr.merge_conflict_resolution",
            base_branch=str(base.get("ref") or ""),
            pr_number=job.target_number,
            pr_title=str(metadata.get("title") or "Unknown"),
            pr_body=str(metadata.get("body") or "")[:500],
            conflict_info=conflict_info,
            commit_log="(Captured exact-head merge job)",
            linked_issues_context="",
        )

    def invoke_at_provider_entry(self, job: LocalJobRecord, checkpoint: Callable[[], bool]) -> LocalJobExecutionResult:
        from .pr_processing_evidence import begin_invocation_observation, observed_invocations

        begin_invocation_observation()
        claim = LocalJobClaim(job, True)
        try:
            prepared = self._workspaces.prepare(claim, self._source)
            merge = self._prepare_merge(prepared.workspace, prepared.authority.base_identity)
            if not merge.conflicted:
                if merge.inspection.marker_paths:
                    manifest = self._workspaces.capture_result(
                        claim,
                        prepared,
                        InvocationOutcome.FAILED,
                        "clean merge contains conflict-marker content",
                        completion_source="controller_verified_merge",
                    )
                    return LocalJobExecutionResult(InvocationOutcome.FAILED, manifest.to_json(), "marker-bearing clean merge is nonpublishing")
                manifest = self._workspaces.capture_result(claim, prepared, InvocationOutcome.COMPLETED, "clean exact-head merge", completion_source="controller_verified_merge")
                return LocalJobExecutionResult(InvocationOutcome.COMPLETED, manifest.to_json(), "clean merge; editor not entered")

            allowed, metadata = self._authorized(job)
            if not allowed:
                raise LocalJobProviderEntryRefused("exact PR/base authority changed before editor entry")
            editor = self._editor_factory(job.backend_name, job)
            if not checkpoint():
                raise LocalJobProviderEntryRefused("provider-entry checkpoint failed")
            response = editor(self._prompt(job, metadata, "\n".join(merge.inspection.marker_paths)), prepared.workspace)
            outcome = _classify_editor_output(response or "")
            if outcome is not InvocationOutcome.COMPLETED:
                manifest = self._workspaces.capture_result(claim, prepared, outcome, response or "missing model output", editing_invocations=observed_invocations())
                return LocalJobExecutionResult(outcome, manifest.to_json(), "nonpublishing editor outcome")
            staged = self._git(prepared.workspace, "add", "-A")
            inspection = inspect_conflicts(prepared.workspace)
            if staged.returncode or not inspection.complete or inspection.unmerged_paths or inspection.marker_paths:
                manifest = self._workspaces.capture_result(claim, prepared, InvocationOutcome.FAILED, response, editing_invocations=observed_invocations())
                return LocalJobExecutionResult(InvocationOutcome.FAILED, manifest.to_json(), inspection.reason or "unresolved conflict state")
            manifest = self._workspaces.capture_result(claim, prepared, InvocationOutcome.COMPLETED, response, editing_invocations=observed_invocations())
            return LocalJobExecutionResult(InvocationOutcome.COMPLETED, manifest.to_json(), "resolution ready for downstream publication")
        except LocalJobProviderEntryRefused:
            raise
        except Exception as exc:
            return LocalJobExecutionResult(InvocationOutcome.FAILED, f"{type(exc).__name__}: {exc}", "conflict workspace preparation failed")

    def invoke(self, job: LocalJobRecord) -> LocalJobExecutionResult:
        raise RuntimeError("conflict repair requires the provider-entry checkpoint")

    def validate_controller_completion(self, job: LocalJobRecord, artifact: LocalJobResultArtifact) -> bool:
        try:
            verified = self._workspaces.revalidate_result(job, artifact)
            return verified.manifest.completion_source == "controller_verified_merge"
        except PRRepairWorkspaceError:
            return False


def build_conflict_editor(backend_name: str, job: LocalJobRecord) -> ConflictEditor:
    """Capture the admitted backend without enabling automatic session resume."""
    from .cli_helpers import build_backend_manager
    from .llm_backend_config import active_repo_context, get_llm_config
    from .utils import bind_command_execution_cwd, reset_command_execution_cwd

    with active_repo_context(job.repository):
        config = get_llm_config(repo_name=job.repository)
        manager = build_backend_manager(
            selected_backends=[backend_name],
            primary_backend=backend_name,
            models={backend_name: config.get_model_for_backend(backend_name) or backend_name},
            automatic_session_resume=False,
        )

    def edit(prompt: str, root: Path) -> str:
        from .pr_processing_evidence import record_editing_invocation

        with active_repo_context(job.repository):
            token = bind_command_execution_cwd(str(root))
            try:
                response = manager.run_prompt(prompt)
                record_editing_invocation(manager, "local-conflict-repair", job.repository)
                return response
            finally:
                reset_command_execution_cwd(token)

    return edit
