"""Durable runner adapter for explicit-local PR review corrections."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict
from typing import Callable

from .durable_repair_allowance import GenerationLifecycleState, RepairAllowanceLedger
from .local_job_handoff import InvocationOutcome, LocalJobClaim, LocalJobKind, LocalJobOffer, LocalJobRecord, LocalJobState, LocalJobStore
from .local_job_runner import LocalJobExecutionResult
from .local_review_repair import (
    LocalRepairAllowanceAuthority,
    LocalReviewRepairClaim,
    LocalReviewRepairRequest,
    LocalReviewRepairStore,
    execute_local_review_repair,
    local_review_repair_db_path,
)

ExecutorFactory = Callable[[str, LocalReviewRepairRequest], Callable[[LocalReviewRepairRequest, str], str]]


def resume_pr_correction_publication(job: LocalJobRecord, job_store: LocalJobStore) -> str:
    """Resume a retained commit before downstream validation is awakened."""
    artifact = job_store.get_result_artifact(job.result_reference)
    if artifact is None:
        raise RuntimeError("local correction result artifact is unavailable")
    result = json.loads(artifact.output)
    phase = str(result.get("phase", ""))
    if phase != "publication_pending":
        return phase
    request = PRCorrectionJobAdapter._request(job)
    ledger = RepairAllowanceLedger()
    snapshot = ledger.get_snapshot("https://api.github.com", request.repository, request.pr_number)
    generation = snapshot.get_outstanding_generation()
    if generation is None or generation.generation_id != job.owner_generation:
        raise RuntimeError("local correction allowance changed before publication recovery")
    authority = LocalRepairAllowanceAuthority(ledger, request, generation.generation_id, snapshot.epoch)
    outcome = execute_local_review_repair(
        request,
        store=LocalReviewRepairStore(local_review_repair_db_path(request.repository)),
        allowance_authority=authority,
    )
    if outcome.phase == "publication_pending":
        raise RuntimeError(outcome.reason)
    return outcome.phase


def build_captured_executor(backend_name: str, request: LocalReviewRepairRequest) -> Callable[[LocalReviewRepairRequest, str], str]:
    """Build only the backend policy captured by the accepted envelope."""
    from .cli_helpers import build_backend_manager
    from .llm_backend_config import active_repo_context, get_llm_config
    from .utils import bind_command_execution_cwd, reset_command_execution_cwd

    with active_repo_context(request.repository):
        config = get_llm_config(repo_name=request.repository)
        manager = build_backend_manager(
            selected_backends=[backend_name],
            primary_backend=backend_name,
            models={backend_name: config.get_model_for_backend(backend_name) or backend_name},
            automatic_session_resume=False,
        )

    def invoke(_request: LocalReviewRepairRequest, worktree: str) -> str:
        with active_repo_context(request.repository):
            token = bind_command_execution_cwd(worktree)
            try:
                return manager.run_prompt(request.prompt)
            finally:
                reset_command_execution_cwd(token)

    return invoke


def offer_pr_correction_job(
    request: LocalReviewRepairRequest,
    backend_name: str,
    *,
    store: LocalJobStore | None = None,
    repair_store: LocalReviewRepairStore | None = None,
    allowance_ledger: RepairAllowanceLedger | None = None,
) -> LocalJobRecord | None:
    """Atomically transfer a definitely-unentered correction to the runner."""
    store = store or LocalJobStore()
    repair_store = repair_store or LocalReviewRepairStore(local_review_repair_db_path(request.repository))
    allowance_ledger = allowance_ledger or RepairAllowanceLedger()
    retained = repair_store.get(request)
    if retained is not None and retained.phase == "not_started" and retained.local_job_id:
        existing = store.get(retained.local_job_id)
        snapshot = allowance_ledger.get_snapshot("https://api.github.com", request.repository, request.pr_number)
        generation = snapshot.get_outstanding_generation()
        if (
            existing is not None
            and existing.state is LocalJobState.PENDING
            and existing.upstream_attempt == request.attempt_id
            and existing.owner_incarnation == str(retained.incarnation)
            and generation is not None
            and generation.lifecycle_state is GenerationLifecycleState.RESERVED
            and generation.generation_id == existing.owner_generation
        ):
            return existing
    claim = repair_store.admit(request)
    if not claim.admitted:
        retained = repair_store.get(request, claim.attempt_id)
        return store.get(retained.local_job_id) if retained is not None and retained.local_job_id else None
    payload = json.dumps(asdict(request), sort_keys=True, separators=(",", ":"))
    offer = LocalJobOffer(LocalJobKind.PR_REVIEW_CORRECTION, request.repository, request.pr_number, request.attempt_id, backend_name, payload)
    return store.offer_pr_correction(offer, request, repair_store, allowance_ledger)


class PRCorrectionJobAdapter:
    """Revalidate exact-head authority immediately before local model entry."""

    def __init__(self, github_client: object, executor_factory: ExecutorFactory, job_store: LocalJobStore | None = None) -> None:
        self.github_client = github_client
        self.executor_factory = executor_factory
        self.job_store = job_store or LocalJobStore()
        self._executor_lock = threading.Lock()
        self._executors: dict[str, Callable[[LocalReviewRepairRequest, str], str]] = {}

    @staticmethod
    def _request(job: LocalJobRecord) -> LocalReviewRepairRequest:
        raw = json.loads(job.invocation_input)
        raw["feedback_identities"] = tuple(raw["feedback_identities"])
        return LocalReviewRepairRequest(**raw)

    def authorize_provider_entry(self, job: LocalJobRecord) -> bool:
        with self._executor_lock:
            self._executors.pop(job.job_id, None)
        authorized = self._authority_is_current(job)
        if authorized:
            request = self._request(job)
            executor = self.executor_factory(job.backend_name, request)
            with self._executor_lock:
                self._executors[job.job_id] = executor
        return authorized

    def _authority_is_current(self, job: LocalJobRecord) -> bool:
        request = self._request(job)
        if job.kind is not LocalJobKind.PR_REVIEW_CORRECTION or request.attempt_id != job.upstream_attempt:
            return False
        metadata = self.github_client.get_pull_request_routing_metadata_strict(request.repository, request.pr_number)  # type: ignore[attr-defined]
        if metadata.state != "open" or (metadata.head_sha, metadata.head_ref, metadata.head_repository) != (request.head_sha, request.head_ref, request.head_repository):
            return False
        from .pr_processor import (
            ReviewRepairRouteDisposition,
            _revalidate_local_review_repair_route,
            _review_feedback_identity,
            _select_review_repair_route,
            is_adjudication_envelope,
            is_change_provenance_thread,
        )

        current_pr = {
            "number": request.pr_number,
            "body": metadata.body,
            "head": {"ref": metadata.head_ref, "sha": metadata.head_sha, "repo": {"full_name": metadata.head_repository}},
        }
        route = _select_review_repair_route(request.repository, current_pr, self.github_client)
        route = _revalidate_local_review_repair_route(route, request.repository, current_pr, self.github_client)
        if route.disposition is not ReviewRepairRouteDisposition.LOCAL_REQUIRED or route.evidence is None or route.evidence.head_sha != request.head_sha:
            return False
        threads = self.github_client.get_pr_review_threads_strict(request.repository, request.pr_number)  # type: ignore[attr-defined]
        actionable: set[str] = set()
        for thread in threads:
            if thread.is_resolved or thread.is_outdated or is_change_provenance_thread(thread):
                continue
            addressed_through = max(
                (index for index, comment in enumerate(thread.comments) if "<!-- auto-coder-review-addressed:v1 -->" in comment.body),
                default=-1,
            )
            for index, comment in enumerate(thread.comments):
                if index <= addressed_through or is_adjudication_envelope(comment.body):
                    continue
                actionable.add(_review_feedback_identity(f"{request.repository}#{request.pr_number}:local:", thread, index))
        if not set(request.feedback_identities) <= actionable:
            return False
        repair = LocalReviewRepairStore(local_review_repair_db_path(request.repository)).get(request)
        generation = RepairAllowanceLedger().get_snapshot("https://api.github.com", request.repository, request.pr_number).get_outstanding_generation()
        return bool(repair and repair.local_job_id == job.job_id and not repair.invocation_entered and generation and generation.lifecycle_state is GenerationLifecycleState.RESERVED and generation.generation_id == job.owner_generation and generation.bundle_reference == request.attempt_id)

    def invoke(self, job: LocalJobRecord) -> LocalJobExecutionResult:
        """Invoke directly while preserving the durable entry checkpoint."""
        return self.invoke_at_provider_entry(job, lambda: self.job_store.mark_provider_entered(LocalJobClaim(job, True)))

    def invoke_at_provider_entry(self, job: LocalJobRecord, checkpoint: Callable[[], bool]) -> LocalJobExecutionResult:
        """Prepare first, then revalidate and checkpoint actual model entry."""
        request = self._request(job)
        with self._executor_lock:
            executor = self._executors.pop(job.job_id)
        store = LocalReviewRepairStore(local_review_repair_db_path(request.repository))
        incarnation = int(job.upstream_incarnation.split(":", 1)[0])
        ledger = RepairAllowanceLedger()
        snapshot = ledger.get_snapshot("https://api.github.com", request.repository, request.pr_number)
        authority = LocalRepairAllowanceAuthority(ledger, request, job.owner_generation or "", snapshot.epoch)
        outcome = execute_local_review_repair(
            request,
            store=store,
            executor=executor,
            allowance_authority=authority,
            accepted_claim=LocalReviewRepairClaim(True, "executing", request.attempt_id, incarnation),
            local_job_id=job.job_id,
            provider_entry_authorizer=lambda: self._authority_is_current(job),
            provider_entry_checkpoint=checkpoint,
        )
        result = InvocationOutcome.COMPLETED
        if outcome.phase == "terminal_failure":
            result = InvocationOutcome.CANNOT_FIX
        elif outcome.phase in {"indeterminate", "publication_pending"}:
            result = InvocationOutcome.FAILED
        return LocalJobExecutionResult(
            result,
            json.dumps(asdict(outcome), sort_keys=True),
            outcome.reason,
            definitely_not_started=outcome.phase == "not_started",
        )

    def recover_interrupted(self, job: LocalJobRecord) -> LocalJobExecutionResult | None:
        """Resume a retained controller commit without entering the model again."""
        request = self._request(job)
        store = LocalReviewRepairStore(local_review_repair_db_path(request.repository))
        retained = store.get(request)
        if retained is None or retained.phase != "publication_pending" or retained.local_job_id != job.job_id:
            return None
        ledger = RepairAllowanceLedger()
        snapshot = ledger.get_snapshot("https://api.github.com", request.repository, request.pr_number)
        generation = snapshot.get_outstanding_generation()
        if generation is None or generation.generation_id != job.owner_generation:
            return None
        authority = LocalRepairAllowanceAuthority(ledger, request, generation.generation_id, snapshot.epoch)
        outcome = execute_local_review_repair(request, store=store, allowance_authority=authority)
        if outcome.phase == "publication_pending":
            return None
        result = InvocationOutcome.FAILED if outcome.phase == "indeterminate" else InvocationOutcome.COMPLETED
        return LocalJobExecutionResult(result, json.dumps(asdict(outcome), sort_keys=True), outcome.reason)
