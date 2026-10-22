"""Publish one verified local PR repair artifact without invoking an editor."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from .checkout_lock import checkout_lock
from .git_branch import git_commit_with_retry
from .git_commit import git_push
from .local_job_handoff import InvocationOutcome, LocalJobClaim, LocalJobKind, LocalJobRecord, LocalJobState, LocalJobStore
from .pr_repair_workspace import PRRepairResultManifest, PRRepairWorkspaceError, PRRepairWorkspaceProducer
from .runtime_locks import ensure_lock_directory, file_lock, lock_path


class PRRepairPublicationDisposition(str, Enum):
    PUBLISHED = "published"
    PENDING = "pending"
    STALE = "stale"
    REFUSED = "refused"
    NO_CHANGE = "no_change"
    CANNOT_FIX = "cannot_fix"


@dataclass(frozen=True)
class PRRepairTargetObservation:
    repository: str
    pr_number: int
    head_repository: str
    head_ref: str
    head_sha: str
    base_repository: str
    base_ref: str
    base_sha: str
    open: bool
    merged: bool
    owner_allowed: bool


@dataclass(frozen=True)
class PRRepairPublicationResult:
    job_id: str
    disposition: PRRepairPublicationDisposition
    candidate_sha: str = ""
    diagnostic: str = ""
    retry_error: Exception | None = None


TargetObserver = Callable[[str, int], PRRepairTargetObservation]
InvalidationWake = Callable[[str, int, str], bool]
ExecutionCompletion = Callable[[LocalJobRecord], None]


class PRRepairJobFinalizer:
    """Controller boundary between retained repair output and a PR branch."""

    def __init__(
        self,
        store: LocalJobStore,
        observe_target: TargetObserver,
        wake: InvalidationWake,
        *,
        finish_execution: ExecutionCompletion | None = None,
    ) -> None:
        self._store = store
        self._observe_target = observe_target
        self._wake = wake
        self._finish_execution = finish_execution
        self._workspace = PRRepairWorkspaceProducer(store)

    @staticmethod
    def _git(cwd: Path, *args: str, check: bool = True) -> str:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        if check and result.returncode:
            raise PRRepairWorkspaceError(result.stderr.strip() or f"git {' '.join(args)} failed")
        return result.stdout.strip() if result.returncode == 0 else ""

    @staticmethod
    def _evidence(**values: object) -> str:
        return json.dumps({"schema_version": 1, **values}, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _decode(evidence: str) -> dict[str, object]:
        value = json.loads(evidence)
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("unsupported publication evidence")
        return value

    @staticmethod
    def _matches_target(observed: PRRepairTargetObservation, manifest: PRRepairResultManifest, *, candidate: str = "") -> bool:
        allowed_heads = {manifest.head_sha}
        if candidate:
            allowed_heads.add(candidate)
        return (
            observed.repository == manifest.repository
            and observed.pr_number == manifest.pr_number
            and observed.head_repository == manifest.head_repository == manifest.repository
            and observed.head_ref == manifest.head_ref
            and observed.head_sha in allowed_heads
            and observed.base_repository == manifest.base_repository
            and observed.base_ref == manifest.base_ref
            and observed.base_sha == manifest.base_sha
            and observed.open
            and not observed.merged
            and observed.owner_allowed
        )

    def _result(self, job_id: str, disposition: PRRepairPublicationDisposition, candidate: str = "", diagnostic: str = "", error: Exception | None = None) -> PRRepairPublicationResult:
        return PRRepairPublicationResult(job_id, disposition, candidate, diagnostic, error)

    def finalize(self, job_id: str) -> PRRepairPublicationResult:
        record = self._store.get(job_id)
        if record is None:
            return self._result(job_id, PRRepairPublicationDisposition.REFUSED, diagnostic="job is unavailable")
        if record.kind not in {LocalJobKind.PR_CI_FAILURE, LocalJobKind.PR_MERGE_CONFLICT}:
            return self._result(job_id, PRRepairPublicationDisposition.REFUSED, diagnostic="job is not a PR repair")
        lock = lock_path(record.repository, self._store.path, "pr-repair-publication", str(record.target_number))
        ensure_lock_directory(lock)
        try:
            with file_lock(lock), checkout_lock(record.workspace_path):
                return self._finalize_locked(job_id)
        except Exception as exc:
            return self._result(job_id, PRRepairPublicationDisposition.PENDING, diagnostic=str(exc), error=exc)

    def _finalize_locked(self, job_id: str) -> PRRepairPublicationResult:
        record = self._store.get(job_id)
        if record is None or record.state not in {LocalJobState.RESULT_RECORDED, LocalJobState.DOWNSTREAM_EFFECTS_PENDING, LocalJobState.SETTLED}:
            return self._result(job_id, PRRepairPublicationDisposition.PENDING, diagnostic="job has no completed result")
        claim = LocalJobClaim(record, True)
        if record.invocation_outcome is InvocationOutcome.CANNOT_FIX:
            return self._finish_terminal(record, PRRepairPublicationDisposition.CANNOT_FIX, "local PR repair cannot fix the selected failure")
        if record.invocation_outcome is not InvocationOutcome.COMPLETED:
            return self._result(job_id, PRRepairPublicationDisposition.REFUSED, diagnostic="result is not completed")
        artifact = self._store.get_execution_result_artifact(job_id, record.execution_incarnation)
        if artifact is None or artifact.artifact_id != record.result_reference:
            return self._result(job_id, PRRepairPublicationDisposition.REFUSED, diagnostic="exact result artifact is unavailable")
        candidate = self._candidate(record.job_id, record.execution_incarnation)
        recovery_candidate = candidate or self._pending_commit_candidate(record, artifact.artifact_id)
        verified = self._workspace.revalidate_result(record, artifact, candidate_sha=recovery_candidate)
        manifest = verified.manifest
        workspace = Path(manifest.workspace)

        if self._git(workspace, "remote", "get-url", "--push", "origin") != manifest.remote:
            return self._result(job_id, PRRepairPublicationDisposition.REFUSED, candidate, "publication remote changed after artifact capture")

        if not candidate and recovery_candidate and recovery_candidate != manifest.head_sha:
            expected_parents = [manifest.head_sha]
            if record.kind is LocalJobKind.PR_MERGE_CONFLICT:
                expected_parents.append(manifest.base_sha)
            if not self._store.record_effect(
                claim,
                "commit",
                "completed",
                self._evidence(candidate_sha=recovery_candidate, artifact_id=verified.artifact_id, expected_parents=expected_parents),
            ):
                return self._result(job_id, PRRepairPublicationDisposition.PENDING, recovery_candidate, "recovered candidate checkpoint was not retained")
            candidate = recovery_candidate

        if record.state is LocalJobState.SETTLED:
            push_receipt = self._store.get_effect_strict(job_id, record.execution_incarnation, "push")
            wake_receipt = self._store.get_effect_strict(job_id, record.execution_incarnation, "ci_wake")
            if candidate and push_receipt is not None and push_receipt.state == "completed" and wake_receipt is not None and wake_receipt.state == "completed":
                self._retire_execution(record)
                return self._result(job_id, PRRepairPublicationDisposition.PUBLISHED, candidate)
            return self._result(job_id, PRRepairPublicationDisposition.REFUSED, candidate, "settled job lacks complete publication receipts")

        observed = self._observe_target(record.repository, record.target_number)
        if not self._matches_target(observed, manifest, candidate=candidate):
            return self._finish_terminal(record, PRRepairPublicationDisposition.STALE, "PR target or publication ownership changed", candidate)

        if not candidate:
            candidate = self._commit(claim, manifest, verified.artifact_id)
            if not candidate:
                return self._result(job_id, PRRepairPublicationDisposition.NO_CHANGE, diagnostic="artifact contains no CI edit")

        push = self._store.get_effect_strict(job_id, record.execution_incarnation, "push")
        if push is None:
            # Re-observe immediately before claiming the single send permission.
            observed = self._observe_target(record.repository, record.target_number)
            if not self._matches_target(observed, manifest) or observed.head_sha != manifest.head_sha:
                return self._finish_terminal(record, PRRepairPublicationDisposition.STALE, "remote head changed before push", candidate)
            intent = self._store.claim_effect_intent_if_absent(
                claim,
                "push",
                self._evidence(candidate_sha=candidate, expected_head=manifest.head_sha, head_ref=manifest.head_ref, artifact_id=verified.artifact_id),
            )
            if intent.inserted:
                result = git_push(cwd=str(workspace), remote="origin", branch=f"{candidate}:{manifest.head_ref}", expected_remote_sha=manifest.head_sha)
                if not result.success:
                    self._store.record_effect(claim, "push", "indeterminate", intent.effect.evidence)

        # Every retained intent, including the invocation that sent it, is
        # reconciled by observation.  H is intentionally not permission to retry.
        observed = self._observe_target(record.repository, record.target_number)
        if observed.head_sha != candidate or not self._matches_target(observed, manifest, candidate=candidate):
            return self._result(job_id, PRRepairPublicationDisposition.PENDING, candidate, "push outcome is not the exact candidate")
        if not self._store.record_effect(claim, "push", "completed", self._evidence(candidate_sha=candidate, head_ref=manifest.head_ref, artifact_id=verified.artifact_id)):
            return self._result(job_id, PRRepairPublicationDisposition.PENDING, candidate, "publication receipt was not retained")
        if record.state is LocalJobState.RESULT_RECORDED and not self._store.mark_downstream_pending(claim):
            return self._result(job_id, PRRepairPublicationDisposition.PENDING, candidate, "downstream state was not retained")
        record = self._store.get(job_id) or record
        claim = LocalJobClaim(record, True)
        wake = self._store.get_effect_strict(job_id, record.execution_incarnation, "ci_wake")
        if wake is None or wake.state != "completed":
            identity = self._evidence(repository=record.repository, pr_number=record.target_number, head_sha=candidate)
            self._store.claim_effect_intent_if_absent(claim, "ci_wake", identity)
            if not self._wake(record.repository, record.target_number, candidate):
                return self._result(job_id, PRRepairPublicationDisposition.PENDING, candidate, "new-head invalidation is pending")
            if not self._store.record_effect(claim, "ci_wake", "completed", identity):
                return self._result(job_id, PRRepairPublicationDisposition.PENDING, candidate, "wake acknowledgement was not retained")
        if record.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING and not self._store.settle(claim, "exact PR repair publication completed"):
            return self._result(job_id, PRRepairPublicationDisposition.PENDING, candidate, "job settlement is pending")
        self._retire_execution(self._store.get(job_id) or record)
        return self._result(job_id, PRRepairPublicationDisposition.PUBLISHED, candidate)

    def _retire_execution(self, record: LocalJobRecord) -> None:
        if self._finish_execution is not None and record.implementation_execution_id:
            self._finish_execution(record)

    def _finish_terminal(
        self,
        record: LocalJobRecord,
        disposition: PRRepairPublicationDisposition,
        diagnostic: str,
        candidate: str = "",
    ) -> PRRepairPublicationResult:
        """Settle terminal repair work before releasing its exact execution."""
        claim = LocalJobClaim(record, True)
        if record.state is LocalJobState.RESULT_RECORDED:
            if not self._store.mark_downstream_pending(claim):
                return self._result(record.job_id, PRRepairPublicationDisposition.PENDING, candidate, "terminal disposition retention is pending")
            record = self._store.get(record.job_id) or record
            claim = LocalJobClaim(record, True)
        if record.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING:
            if not self._store.settle(claim, diagnostic):
                return self._result(record.job_id, PRRepairPublicationDisposition.PENDING, candidate, "terminal job settlement is pending")
            record = self._store.get(record.job_id) or record
        if record.state is not LocalJobState.SETTLED:
            return self._result(record.job_id, PRRepairPublicationDisposition.PENDING, candidate, "terminal job state is unavailable")
        self._retire_execution(record)
        return self._result(record.job_id, disposition, candidate, diagnostic)

    def _candidate(self, job_id: str, incarnation: str) -> str:
        effect = self._store.get_effect_strict(job_id, incarnation, "commit")
        if effect is None or effect.state != "completed":
            return ""
        value = self._decode(effect.evidence)
        return str(value.get("candidate_sha") or "")

    def _pending_commit_candidate(self, record: LocalJobRecord, artifact_id: str) -> str:
        """Find an uncheckpointed HEAD only through its exact commit intent."""
        effect = self._store.get_effect_strict(record.job_id, record.execution_incarnation, "commit")
        if effect is None or effect.state != "pending":
            return ""
        evidence = self._decode(effect.evidence)
        if evidence.get("artifact_id") != artifact_id or not record.workspace_path:
            return ""
        return self._git(Path(record.workspace_path), "rev-parse", "HEAD")

    def _commit(self, claim: LocalJobClaim, manifest: PRRepairResultManifest, artifact_id: str) -> str:
        workspace = Path(manifest.workspace)
        changed = [name for name, _identity in manifest.changed_files]
        if not changed and claim.record.kind is LocalJobKind.PR_CI_FAILURE:
            self._store.record_effect(claim, "commit", "skipped", self._evidence(artifact_id=artifact_id, reason="no_change"))
            return ""
        if self._git(workspace, "ls-files", "-u"):
            raise PRRepairWorkspaceError("unresolved index entries remain")
        for name in changed:
            path = workspace / name
            if path.is_file() and not path.is_symlink():
                try:
                    contents = path.read_text(errors="strict")
                except (OSError, UnicodeError) as exc:
                    raise PRRepairWorkspaceError(f"cannot inspect changed file {name}") from exc
                if any(marker in contents for marker in ("<<<<<<<", "=======", ">>>>>>>")):
                    raise PRRepairWorkspaceError(f"unresolved conflict marker in {name}")
        parents = [manifest.head_sha]
        if claim.record.kind is LocalJobKind.PR_MERGE_CONFLICT:
            if manifest.merge_head != manifest.base_sha:
                raise PRRepairWorkspaceError("merge artifact lost retained base lineage")
            parents.append(manifest.base_sha)
        intent_evidence = self._evidence(artifact_id=artifact_id, expected_parents=parents, changed_files=changed, head_ref=manifest.head_ref)
        intent = self._store.claim_effect_intent_if_absent(claim, "commit", intent_evidence)
        if not intent.inserted:
            # A commit may have completed before its receipt.  Recover only the
            # exact current commit with the intended ancestry and unchanged files.
            head = self._git(workspace, "rev-parse", "HEAD")
            actual_parents = self._git(workspace, "show", "-s", "--format=%P", head).split()
            if actual_parents == parents and tuple(sorted(self._workspace._files(workspace).items())) == manifest.workspace_files:
                self._store.record_effect(claim, "commit", "completed", self._evidence(candidate_sha=head, artifact_id=artifact_id, expected_parents=parents))
                return head
            raise PRRepairWorkspaceError("commit intent is ambiguous and cannot be replayed")
        # A mixed reset destroys authenticated MERGE_HEAD. CI output has no merge
        # metadata, so only that path sanitizes the possibly model-touched index.
        if claim.record.kind is LocalJobKind.PR_CI_FAILURE:
            self._git(workspace, "reset", "--mixed", "HEAD")
        if changed:
            self._git(workspace, "add", "-A", "--", *changed)
        staged_tree = self._git(workspace, "write-tree")
        if self._workspace._commit_files(workspace, staged_tree) != dict(manifest.workspace_files):
            raise PRRepairWorkspaceError("staged tree does not match authenticated output")
        result = git_commit_with_retry(f"Auto-Coder: repair PR #{manifest.pr_number}", cwd=str(workspace), max_retries=0, allow_repair=False)
        if not result.success:
            raise PRRepairWorkspaceError(result.stderr or "strict repair commit failed")
        candidate = self._git(workspace, "rev-parse", "HEAD")
        actual_parents = self._git(workspace, "show", "-s", "--format=%P", candidate).split()
        if actual_parents != parents or self._workspace._commit_files(workspace, candidate) != dict(manifest.workspace_files) or tuple(sorted(self._workspace._files(workspace).items())) != manifest.workspace_files:
            raise PRRepairWorkspaceError("commit hooks changed authenticated output or ancestry")
        if not self._store.record_effect(claim, "commit", "completed", self._evidence(candidate_sha=candidate, artifact_id=artifact_id, expected_parents=parents)):
            raise PRRepairWorkspaceError("candidate checkpoint was not retained")
        return candidate

    def resume_all(self, repository: str) -> tuple[PRRepairPublicationResult, ...]:
        jobs = (job for job in self._store.discover_unsettled() if job.repository == repository and job.kind in {LocalJobKind.PR_CI_FAILURE, LocalJobKind.PR_MERGE_CONFLICT} and job.state in {LocalJobState.RESULT_RECORDED, LocalJobState.DOWNSTREAM_EFFECTS_PENDING})
        return tuple(self.finalize(job.job_id) for job in jobs)
