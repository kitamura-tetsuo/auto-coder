"""Production adapter for worker-independent local Issue implementations."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .attempt_manager import get_current_attempt
from .implementation_slots import ImplementationOwner, ImplementationSlotRepository
from .issue_dispatch import IssueAttemptIdentity, IssueDispatchGuard
from .issue_job_workspace import IssueJobSource, IssueJobWorkspaceProducer
from .local_job_handoff import InvocationOutcome, LocalJobClaim, LocalJobRecord, LocalJobStore
from .local_job_runner import LocalJobExecutionResult, LocalJobProviderEntryRefused
from .utils import bind_command_execution_cwd, reset_command_execution_cwd

if TYPE_CHECKING:
    from .automation_engine import AutomationEngine


class IssueLocalJobAdapter:
    """Revalidate an accepted Issue attempt and execute it in its private clone."""

    def __init__(
        self,
        engine: "AutomationEngine",
        repository: str,
        store: LocalJobStore,
        slots: ImplementationSlotRepository,
        repository_path: Path,
    ) -> None:
        self._engine = engine
        self._repository = repository
        self._store = store
        self._slots = slots
        self._repository_path = repository_path.resolve()

    def authorize_provider_entry(self, job: LocalJobRecord) -> bool:
        if job.repository != self._repository:
            return False
        # Ordinary attempts use the numeric attempt projection. Explicit retry
        # attempts use their own opaque durable authority and are fenced by the
        # dispatch incarnation below rather than the unrelated numeric counter.
        if job.upstream_attempt.isdigit() and str(get_current_attempt(job.repository, job.target_number)) != job.upstream_attempt:
            return False
        repository_owner, repository_name = job.repository.split("/", 1)
        identity = IssueAttemptIdentity(
            repository_owner,
            repository_name,
            job.target_number,
            job.upstream_attempt,
        )
        authority = IssueDispatchGuard().inspect_local_job_authority(identity)
        if authority is None or authority.claim_incarnation != job.upstream_incarnation:
            return False
        owner = ImplementationOwner("issue", job.target_number)
        if not job.implementation_execution_id or job.implementation_execution_id not in self._slots.active_execution_ids(owner):
            return False
        try:
            snapshot = self._engine.github.get_issue_dispatch_snapshot_strict(job.repository, job.target_number)
            if not isinstance(snapshot, dict) or not self._engine._is_issue_author_allowed(snapshot):
                return False
            authorized = self._engine._authorize_stale_jules_dispatch(job.repository, job.target_number, snapshot)
            if authorized is None:
                return False
            parent_number = self._engine._get_authoritative_parent_number(job.repository, job.target_number, authorized)
            family_set = self._engine._fetch_authoritative_decomposition_set(job.repository, parent_number) if parent_number is not None else None
            current_generation = self._engine._compute_implementation_generation(job.repository, authorized, family_set)
            return self._slots.implementation_generation(owner) == current_generation
        except Exception:
            return False

    def invoke(self, job: LocalJobRecord) -> LocalJobExecutionResult:
        return self.invoke_at_provider_entry(job, lambda: True)

    def invoke_at_provider_entry(
        self,
        job: LocalJobRecord,
        mark_provider_entered: Callable[[], bool],
    ) -> LocalJobExecutionResult:
        # LocalJobRunner already owns this exact incarnation; the workspace
        # producer only needs the fenced identity for its durable writes.
        claim = LocalJobClaim(job, True)
        source_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self._repository_path, check=True, capture_output=True, text=True).stdout.strip()
        source_ref = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=self._repository_path, check=True, capture_output=True, text=True).stdout.strip()
        source = IssueJobSource(
            job.repository,
            self._repository_path,
            source_ref,
            source_commit,
            f"issue-{job.target_number}-attempt-{job.upstream_attempt}",
        )
        producer = IssueJobWorkspaceProducer(self._store, implementation_slots=self._slots)

        def run(workspace: Path, prompt: str) -> str:
            from .cli_helpers import build_backend_manager
            from .llm_backend_config import get_llm_config

            if not self.authorize_provider_entry(job):
                raise LocalJobProviderEntryRefused("authoritative provider-entry permission denied after preparation")
            if not mark_provider_entered():
                raise LocalJobProviderEntryRefused("provider-entry checkpoint failed")
            config = get_llm_config(repo_name=job.repository)
            model = config.get_model_for_backend(job.backend_name) or ""
            manager = build_backend_manager(selected_backends=[job.backend_name], primary_backend=job.backend_name, models={job.backend_name: model})
            # Command execution uses a context-local root, so independent
            # runner threads can enter separate private repositories without
            # mutating or serializing on the process-wide working directory.
            token = bind_command_execution_cwd(str(workspace))
            try:
                return manager._run_llm_cli(prompt)
            finally:
                reset_command_execution_cwd(token)

        checkpoint = producer.execute(claim, source, run, checkpoint_result=False)
        return LocalJobExecutionResult(InvocationOutcome.COMPLETED, checkpoint.result_reference)
