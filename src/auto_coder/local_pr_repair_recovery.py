"""Durable pending-work integration for local PR repair publication."""

from __future__ import annotations

from .github_pending_work import PendingObligation, PendingReason, PendingWorkStore, StageOutcome, WorkIdentity
from .local_job_handoff import LocalJobKind, LocalJobState, LocalJobStore
from .pr_repair_finalizer import PRRepairJobFinalizer, PRRepairPublicationDisposition

LOCAL_PR_REPAIR_FINALIZATION_STAGE = "local-pr-repair-finalization"
LOCAL_PR_REPAIR_FINALIZATION_EFFECT = "finalize-local-pr-repair"


def finalization_identity(repository: str, job_id: str, execution_incarnation: str) -> WorkIdentity:
    return WorkIdentity(repository, f"local-job:{job_id}", LOCAL_PR_REPAIR_FINALIZATION_STAGE, execution_incarnation)


class LocalPRRepairFinalizationHandler:
    """Resume only the finalizer journal for one exact retained job."""

    def __init__(self, repository: str, jobs: LocalJobStore, finalizer: PRRepairJobFinalizer) -> None:
        self._repository = repository
        self._jobs = jobs
        self._finalizer = finalizer

    def _run(self, obligation: PendingObligation) -> StageOutcome:
        prefix = "local-job:"
        if obligation.identity.repository != self._repository or not obligation.identity.entity.startswith(prefix):
            return StageOutcome(blocked_reason=PendingReason.EVALUATION_FAILED, blocked_error="finalization identity is outside this repository")
        job_id = obligation.identity.entity[len(prefix) :]
        job = self._jobs.get(job_id)
        if job is None or job.repository != self._repository or job.kind not in {LocalJobKind.PR_CI_FAILURE, LocalJobKind.PR_MERGE_CONFLICT} or job.execution_incarnation != obligation.identity.revision:
            return StageOutcome(superseded=True)
        result = self._finalizer.finalize(job_id)
        if result.disposition in {
            PRRepairPublicationDisposition.PUBLISHED,
            PRRepairPublicationDisposition.NO_CHANGE,
            PRRepairPublicationDisposition.CANNOT_FIX,
            PRRepairPublicationDisposition.STALE,
            PRRepairPublicationDisposition.REFUSED,
        }:
            return StageOutcome(completed_effects=(LOCAL_PR_REPAIR_FINALIZATION_EFFECT,))
        # Preserve the existing due time and let the scheduler re-enter this
        # exact controller. Typed GitHub failures are returned by the finalizer
        # when available; local lock/store uncertainty remains unfinished.
        if result.retry_error is not None and hasattr(result.retry_error, "outcome"):
            return StageOutcome(error=result.retry_error)  # type: ignore[arg-type]
        return StageOutcome()

    def dispatch(self, obligation: PendingObligation) -> StageOutcome:
        return self._run(obligation)

    def recover(self, obligation: PendingObligation) -> StageOutcome:
        return self._run(obligation)


def rediscover_pr_repair_finalizations(repository: str, jobs: LocalJobStore, pending: PendingWorkStore, *, strict: bool = False) -> tuple[WorkIdentity, ...]:
    """Ensure publication obligations for exact recoverable PR results."""
    identities = []
    retained = jobs.discover_unsettled_strict() if strict else jobs.discover_unsettled()
    for job in retained:
        if job.repository != repository or job.kind not in {LocalJobKind.PR_CI_FAILURE, LocalJobKind.PR_MERGE_CONFLICT} or job.state not in {LocalJobState.RESULT_RECORDED, LocalJobState.DOWNSTREAM_EFFECTS_PENDING}:
            continue
        identity = finalization_identity(repository, job.job_id, job.execution_incarnation)
        pending.ensure_reevaluation(identity, (LOCAL_PR_REPAIR_FINALIZATION_EFFECT,))
        identities.append(identity)
    return tuple(identities)
