"""Durable, publication-free execution adapter for admitted local CI repairs."""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .ci_repair_authority import CIRepairAuthority
from .local_ci_repair_execution import (
    CorrectionPorts,
    LocalCIRepairRequest,
    LocalCIRepairResult,
    execute_correction_without_publication,
)
from .local_job_handoff import InvocationOutcome, LocalJobClaim, LocalJobKind, LocalJobRecord, LocalJobStore
from .local_job_runner import LocalJobExecutionResult, LocalJobProviderEntryRefused
from .pr_repair_workspace import PreparedPRRepairWorkspace, PRRepairSource, PRRepairWorkspaceProducer


@dataclass(frozen=True)
class DurableCICorrection:
    """Workspace-bound inputs for the existing focused correction loop."""

    request: LocalCIRepairRequest
    diagnostics: str
    failed_tests: list[str] | None
    ports: CorrectionPorts
    extract_failed_tests: Callable[[str], list[str]]


@dataclass(frozen=True)
class LocalCIRepairJobPorts:
    """Production seams whose implementations retain GitHub/backend ownership."""

    source: Callable[[LocalJobRecord], PRRepairSource]
    github_client: Callable[[LocalJobRecord], object]
    authority: Callable[[object, str, int, str], AbstractContextManager[CIRepairAuthority]]
    local_route: Callable[[LocalJobRecord, dict[str, object]], bool]
    correction: Callable[[LocalJobRecord, PreparedPRRepairWorkspace], DurableCICorrection | None]


class LocalCIRepairJobAdapter:
    """Execute exactly one retained CI job in its private workspace.

    Preparation is read-only.  The final PR/CI read, durable entry checkpoint,
    and first editing call occur under the same invalidation fence.
    """

    def __init__(self, store: LocalJobStore, workspace: PRRepairWorkspaceProducer, ports: LocalCIRepairJobPorts) -> None:
        self._store = store
        self._workspace = workspace
        self._ports = ports

    def _claim(self, job: LocalJobRecord) -> LocalJobClaim:
        current = self._store.get(job.job_id)
        if current is None or current.execution_incarnation != job.execution_incarnation or current.runner_owner != job.runner_owner:
            raise LocalJobProviderEntryRefused("retained CI job ownership changed")
        return LocalJobClaim(current, True)

    def authorize_provider_entry(self, job: LocalJobRecord) -> bool:
        claim = self._store.get_pr_repair_claim(job.job_id)
        return bool(job.kind is LocalJobKind.PR_CI_FAILURE and claim is not None and claim.upstream_incarnation == job.upstream_incarnation)

    def invoke(self, job: LocalJobRecord) -> LocalJobExecutionResult:
        raise LocalJobProviderEntryRefused("CI repair entry requires the runner checkpoint callback")

    def invoke_at_provider_entry(self, job: LocalJobRecord, checkpoint: Callable[[], bool]) -> LocalJobExecutionResult:
        claim = self._claim(job)
        retained = self._store.get_pr_repair_claim(job.job_id)
        if retained is None or not retained.failed_check_identities:
            raise LocalJobProviderEntryRefused("retained CI failure authority is unavailable")
        try:
            prepared = self._workspace.prepare(claim, self._ports.source(job))
            correction = self._ports.correction(job, prepared)
            github = self._ports.github_client(job)
        except Exception as exc:
            raise LocalJobProviderEntryRefused(f"CI repair preparation unavailable: {exc}") from exc
        if correction is None or github is None or correction.request.expected_head != retained.head_sha:
            raise LocalJobProviderEntryRefused("captured backend or correction inputs are unavailable")

        with self._ports.authority(github, job.repository, job.target_number, retained.head_sha) as authority:
            if not authority.allowed or authority.failure_identities != retained.failed_check_identities:
                raise LocalJobProviderEntryRefused(authority.reason or "selected CI failure generation changed")
            try:
                metadata = github.get_pull_request_metadata_strict(job.repository, job.target_number)  # type: ignore[attr-defined]
                base = metadata.get("base") or {}
                base_repo = (base.get("repo") or {}).get("full_name")
                exact_base = base_repo == retained.base_repository and base.get("ref") == retained.base_ref and (base.get("sha") or base.get("identity")) == retained.base_identity
                routed = self._ports.local_route(job, metadata)
            except Exception as exc:
                raise LocalJobProviderEntryRefused(f"fresh PR route read unavailable: {exc}") from exc
            if not exact_base or not routed:
                raise LocalJobProviderEntryRefused("PR base or local repair route changed")
            if not checkpoint():
                raise LocalJobProviderEntryRefused("provider-entry checkpoint failed")
            result = execute_correction_without_publication(
                correction.request,
                correction.diagnostics,
                correction.failed_tests,
                correction.ports,
                correction.extract_failed_tests,
            )

        output = json.dumps(asdict(result), sort_keys=True, default=str)
        changed = tuple(path for path, identity in self._workspace.capture_result(claim, prepared, InvocationOutcome.COMPLETED, output).changed_files if identity is not None)
        if result.failure_reason:
            outcome = InvocationOutcome.FAILED
        elif result.attempt_started and changed:
            outcome = InvocationOutcome.COMPLETED
        else:
            outcome = InvocationOutcome.CANNOT_FIX
        manifest = self._workspace.capture_result(claim, prepared, outcome, output)
        diagnostic = "publication pending" if outcome is InvocationOutcome.COMPLETED else "no publishable correction"
        return LocalJobExecutionResult(outcome, manifest.to_json(), diagnostic)


def build_local_ci_repair_job_adapter(
    store: LocalJobStore,
    ports: LocalCIRepairJobPorts,
    *,
    workspace_root: Path | None = None,
) -> LocalCIRepairJobAdapter:
    """Compose the production adapter with the durable workspace producer."""
    return LocalCIRepairJobAdapter(store, PRRepairWorkspaceProducer(store, workspace_root), ports)
