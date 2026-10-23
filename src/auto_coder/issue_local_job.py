"""Production adapter for worker-independent local Issue implementations."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol

from .attempt_manager import get_current_attempt
from .implementation_slots import ImplementationOwner, ImplementationSlotRepository
from .issue_dispatch import IssueAttemptIdentity, IssueDispatchGuard
from .issue_job_workspace import IssueJobCannotFix, IssueJobSource, IssueJobWorkspaceProducer
from .local_issue_start_notification import LocalIssueStartNotifier
from .local_job_handoff import InvocationOutcome, LocalJobClaim, LocalJobContinuation, LocalJobRecord, LocalJobStore
from .local_job_runner import LocalJobExecutionResult, LocalJobProviderEntryRefused
from .local_llm_observability import bind_local_llm_start_callback, reset_local_llm_start_callback
from .muse_background_observation import bind_muse_job_observation
from .utils import bind_command_execution_cwd, reset_command_execution_cwd


class IssueJobGitHubPort(Protocol):
    """GitHub operations needed before a local provider may be entered."""

    def get_issue_dispatch_snapshot_strict(self, repository: str, number: int) -> Mapping[str, object]: ...

    def add_comment_to_issue(self, repository: str, issue_number: int, body: str) -> None: ...

    def get_issue_comments_strict(self, repository: str, issue_number: int) -> list[dict[str, object]]: ...


@dataclass(frozen=True)
class IssueJobAuthorizationPorts:
    """Narrow controller policy used by the durable Issue adapter."""

    github: IssueJobGitHubPort
    main_branch: str
    author_allowed: Callable[[Any], bool]
    authorize_dispatch: Callable[[str, int, Any], Optional[Any]]
    parent_number: Callable[[str, int, Any], Optional[int]]
    decomposition_set: Callable[[str, int], Any]
    implementation_generation: Callable[[str, Any, Any], str]


def issue_job_authorization_ports(controller: object) -> IssueJobAuthorizationPorts:
    """Project a controller-shaped object onto the adapter's explicit ports."""
    config = getattr(controller, "config")
    return IssueJobAuthorizationPorts(
        github=getattr(controller, "github"),
        main_branch=getattr(config, "MAIN_BRANCH"),
        author_allowed=getattr(controller, "_is_issue_author_allowed"),
        authorize_dispatch=getattr(controller, "_authorize_stale_jules_dispatch"),
        parent_number=getattr(controller, "_get_authoritative_parent_number"),
        decomposition_set=getattr(controller, "_fetch_authoritative_decomposition_set"),
        implementation_generation=getattr(controller, "_compute_implementation_generation"),
    )


class IssueLocalJobAdapter:
    """Revalidate an accepted Issue attempt and execute it in its private clone."""

    def __init__(
        self,
        ports: IssueJobAuthorizationPorts,
        repository: str,
        store: LocalJobStore,
        slots: ImplementationSlotRepository,
        repository_path: Path,
    ) -> None:
        self._ports = ports
        self._repository = repository
        self._store = store
        self._slots = slots
        self._repository_path = repository_path.resolve()
        self._start_notifier = LocalIssueStartNotifier(store, ports.github)

    def recover_start_notifications(self, repository: str, job_ids: frozenset[str] | None) -> None:
        """Recover notification effects independently of invocation settlement."""
        self._start_notifier.recover(repository, job_ids)

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
            snapshot = self._ports.github.get_issue_dispatch_snapshot_strict(job.repository, job.target_number)
            if not isinstance(snapshot, Mapping) or not self._ports.author_allowed(snapshot):
                return False
            authorized = self._ports.authorize_dispatch(job.repository, job.target_number, snapshot)
            if authorized is None:
                return False
            parent_number = self._ports.parent_number(job.repository, job.target_number, authorized)
            family_set = self._ports.decomposition_set(job.repository, parent_number) if parent_number is not None else None
            current_generation = self._ports.implementation_generation(job.repository, authorized, family_set)
            return self._slots.implementation_generation(owner) == current_generation
        except Exception:
            return False

    def authorize_recovery(self, job: LocalJobRecord) -> bool:
        """Revalidate the same accepted attempt without fresh admission."""
        return self.authorize_provider_entry(job)

    def recover_continuation(
        self,
        job: LocalJobRecord,
        provider_session: str,
        remaining_seconds: float,
    ) -> LocalJobExecutionResult | None:
        """Promote only an exact output already confirmed by Muse settlement."""
        del provider_session, remaining_seconds
        continuation = self._store.get_continuation(job.job_id)
        if continuation is None or not continuation.final_output:
            return None
        checkpoint = IssueJobWorkspaceProducer(
            self._store,
            implementation_slots=self._slots,
        ).recover_confirmed_output(LocalJobClaim(job, True), continuation.final_output)
        return LocalJobExecutionResult(InvocationOutcome.COMPLETED, checkpoint.result_reference)

    def invoke(self, job: LocalJobRecord) -> LocalJobExecutionResult:
        return self.invoke_at_provider_entry(job, lambda: True)

    def recover_resume_result(self, job: LocalJobRecord) -> LocalJobExecutionResult | None:
        """Recover settled provider edits that may not yet have reached handoff."""
        from .worktree_utils import bind_existing_local_workspace, sync_worktree_changes_back

        continuation = self._store.get_continuation(job.job_id)
        if continuation is None or not continuation.final_output:
            return None
        self.validate_resume(job, continuation)
        with bind_existing_local_workspace(Path(continuation.provider_workspace), Path(job.workspace_path)) as binding:
            sync_worktree_changes_back(binding.workspace, binding.caller_root, binding)
        if continuation.final_output.strip() == "CANNOT_FIX":
            return LocalJobExecutionResult(InvocationOutcome.CANNOT_FIX, "CANNOT_FIX")
        return self.recover_continuation(job, continuation.provider_session, 0.0)

    def recover_pending_result(self, job: LocalJobRecord) -> LocalJobExecutionResult | None:
        """Recover a settled candidate held only by proven predecessor task history."""
        from .muse_retained_result import confirm_muse_retained_result

        continuation = self._store.get_continuation(job.job_id)
        if continuation is None or not continuation.candidate_output:
            return None
        self.validate_resume(job, continuation)
        if not confirm_muse_retained_result(self._store, job):
            return None
        return self.recover_resume_result(job)

    def validate_resume(self, job: LocalJobRecord, continuation: LocalJobContinuation) -> None:
        """Refuse reuse until both the controller and every old writer are gone."""
        from .llm_backend_config import get_llm_config
        from .local_job_runner import runner_owner_alive, writer_group_settled

        if get_llm_config(repo_name=job.repository).resolve_backend_type(job.backend_name) != "muse":
            raise RuntimeError("Explicit session resumption currently supports Muse only")
        if continuation.execution_incarnation != job.execution_incarnation or not continuation.provider_workspace or not continuation.writer_owner:
            raise RuntimeError("Retained session lacks its provider workspace or writer identity")
        if runner_owner_alive(continuation.writer_owner) is not False:
            raise RuntimeError("Previous Muse host is live or its liveness is unknown")
        if not writer_group_settled(continuation.writer_owner):
            raise RuntimeError("Previous Muse process group still has live or unverified writers")
        workspace = Path(continuation.provider_workspace)
        producer = IssueJobWorkspaceProducer(self._store)
        if not workspace.is_dir() or not Path(job.workspace_path).is_dir():
            raise RuntimeError("Retained workspace is unavailable")
        if producer._git(workspace, "rev-parse", "HEAD") != job.source_commit or producer._git(Path(job.workspace_path), "rev-parse", "HEAD") != job.source_commit:
            raise RuntimeError("Retained workspace HEAD no longer matches the accepted source")

    def resume_continuation(self, job: LocalJobRecord, provider_session: str, executor_token: str) -> LocalJobExecutionResult:
        """Submit a follow-up in the original Muse root and checkpoint its handoff."""
        from .local_execution_boundary import bind_local_execution_boundary
        from .muse_client import MuseClient
        from .prompt_loader import render_prompt
        from .worktree_utils import bind_existing_local_workspace, sync_worktree_changes_back

        continuation = self._store.get_continuation(job.job_id)
        if continuation is None or continuation.executor_token != executor_token or continuation.provider_session != provider_session:
            raise RuntimeError("Retained session binding changed before resume")
        client = MuseClient(backend_name=job.backend_name)
        if not self._store.bind_continuation_writer(job.job_id, executor_token, Path(continuation.provider_workspace), continuation.writer_owner, deadline_at=time.time() + client.timeout):
            raise RuntimeError("Explicit continuation budget could not be retained")
        prompt = render_prompt("issue.local_resume", task_prompt=job.invocation_input)
        with bind_existing_local_workspace(Path(continuation.provider_workspace), Path(job.workspace_path)) as binding:
            with bind_local_execution_boundary(binding, backend_type="muse", editable=True) as boundary:
                if not self.authorize_recovery(job):
                    raise RuntimeError("Retained authority changed before Muse continuation")
                with bind_muse_job_observation(self._store, job, executor_token):
                    output = client.continue_session(provider_session, prompt)
                boundary.record_backend_success(binding.invocation_id)
                if not boundary.evidence().handoff_authorized:
                    raise RuntimeError("Resumed Muse writers did not settle")
                sync_worktree_changes_back(binding.workspace, binding.caller_root, binding)
        if output.strip() == "CANNOT_FIX":
            return LocalJobExecutionResult(InvocationOutcome.CANNOT_FIX, "CANNOT_FIX")
        checkpoint = IssueJobWorkspaceProducer(self._store, implementation_slots=self._slots).recover_confirmed_output(LocalJobClaim(job, True), output)
        return LocalJobExecutionResult(InvocationOutcome.COMPLETED, checkpoint.result_reference)

    def invoke_at_provider_entry(
        self,
        job: LocalJobRecord,
        mark_provider_entered: Callable[[], bool],
    ) -> LocalJobExecutionResult:
        # LocalJobRunner already owns this exact incarnation; the workspace
        # producer only needs the fenced identity for its durable writes.
        claim = LocalJobClaim(job, True)
        source = IssueJobSource(
            job.repository,
            self._repository_path,
            self._ports.main_branch,
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
            notification_token = bind_local_llm_start_callback(lambda start: self._start_notifier.observe_and_publish(job, backend=start.backend, model=start.model))
            try:
                observed_job = self._store.get(job.job_id) or job
                with bind_muse_job_observation(self._store, observed_job, observed_job.runner_owner):
                    return manager._run_llm_cli(prompt)
            finally:
                reset_local_llm_start_callback(notification_token)
                reset_command_execution_cwd(token)

        try:
            checkpoint = producer.execute(claim, source, run, checkpoint_result=False)
        except IssueJobCannotFix:
            return LocalJobExecutionResult(InvocationOutcome.CANNOT_FIX, "CANNOT_FIX")
        return LocalJobExecutionResult(InvocationOutcome.COMPLETED, checkpoint.result_reference)
