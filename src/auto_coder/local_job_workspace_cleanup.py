"""Conservative reclamation of positively finalized local-job workspaces."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from .issue_job_workspace import default_issue_job_workspace_root
from .local_job_handoff import (
    ContinuationState,
    InvocationOutcome,
    LocalJobKind,
    LocalJobRecord,
    LocalJobState,
    LocalJobStore,
    WorkspaceReclamationPhase,
    WorkspaceReclamationRecord,
)
from .local_job_runner import writer_group_settled
from .pr_repair_workspace import default_pr_repair_workspace_root
from .runtime_locks import ensure_lock_directory, file_lock, lock_path

DEFAULT_RETENTION_SECONDS = 86400.0


@dataclass(frozen=True)
class WorkspaceCleanupResult:
    job_id: str
    path: str
    decision: str
    reason: str


def workspace_reclamation_lock(store: LocalJobStore, record: LocalJobRecord) -> Path:
    return lock_path(record.repository, store.path, "local-job-workspace", f"{record.job_id}:{record.execution_incarnation}")


def workspace_retention_seconds() -> float:
    raw = os.environ.get("AUTO_CODER_JOB_WORKSPACE_RETENTION_SECONDS")
    if raw is None:
        return DEFAULT_RETENTION_SECONDS
    try:
        value = float(raw)
        if not math.isfinite(value) or value < 0:
            raise ValueError
        return value
    except ValueError:
        logger.warning("Invalid AUTO_CODER_JOB_WORKSPACE_RETENTION_SECONDS={!r}; using {} seconds", raw, DEFAULT_RETENTION_SECONDS)
        return DEFAULT_RETENTION_SECONDS


def _effect(store: LocalJobStore, record: LocalJobRecord, name: str, state: str = "completed") -> str | None:
    value = store.get_effect_strict(record.job_id, record.execution_incarnation, name)
    return value.evidence if value is not None and value.state == state else None


def authenticated_successful_disposition(store: LocalJobStore, record: LocalJobRecord) -> tuple[str, str] | None:
    if record.state is not LocalJobState.SETTLED or record.invocation_outcome is not InvocationOutcome.COMPLETED:
        return None
    artifact = store.get_execution_result_artifact(record.job_id, record.execution_incarnation)
    if artifact is None or artifact.artifact_id != record.result_reference or artifact.outcome is not InvocationOutcome.COMPLETED:
        return None
    expected_artifact_id = hashlib.sha256(
        json.dumps(
            [artifact.job_id, artifact.execution_incarnation, artifact.outcome.value, artifact.output],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if artifact.artifact_id != expected_artifact_id:
        return None
    if record.kind is LocalJobKind.ISSUE_IMPLEMENTATION:
        publication = store.get_effect_strict(record.job_id, record.execution_incarnation, "publication")
        commit = store.get_effect_strict(record.job_id, record.execution_incarnation, "commit")
        if record.diagnostic == "no_change":
            if publication is None or publication.state != "skipped" or commit is None or commit.state != "skipped":
                return None
            return artifact.artifact_id, "no_change"
        pr_number = _effect(store, record, "pr")
        commit_sha = _effect(store, record, "commit")
        if not pr_number or not pr_number.isdigit() or int(pr_number) <= 0 or record.diagnostic != f"pr:{pr_number}":
            return None
        if _effect(store, record, "association") != pr_number or not commit_sha or _effect(store, record, "push") != commit_sha:
            return None
        return artifact.artifact_id, f"published:{pr_number}:{commit_sha}"

    if record.kind not in {LocalJobKind.PR_CI_FAILURE, LocalJobKind.PR_MERGE_CONFLICT}:
        return None
    authority = store.get_pr_repair_claim(record.job_id)
    try:
        manifest = json.loads(artifact.output)
    except (TypeError, json.JSONDecodeError):
        return None
    required = {
        "schema_version": 1,
        "job_id": record.job_id,
        "execution_incarnation": record.execution_incarnation,
        "kind": record.kind.value,
        "repository": record.repository,
        "pr_number": record.target_number,
        "workspace": record.workspace_path,
        "outcome": InvocationOutcome.COMPLETED.value,
    }
    if authority is None or not isinstance(manifest, dict) or any(manifest.get(key) != value for key, value in required.items()):
        return None
    authority_values = {
        "upstream_incarnation": authority.upstream_incarnation,
        "head_repository": authority.head_repository,
        "head_ref": authority.head_ref,
        "head_sha": authority.head_sha,
        "base_repository": authority.base_repository,
        "base_ref": authority.base_ref,
        "base_sha": authority.base_identity,
        "remote": record.publication_remote,
    }
    if any(manifest.get(key) != value for key, value in authority_values.items()):
        return None
    commit_raw = _effect(store, record, "commit")
    push_raw = _effect(store, record, "push")
    wake_raw = _effect(store, record, "ci_wake")
    try:
        commit = json.loads(commit_raw or "")
        push = json.loads(push_raw or "")
        wake = json.loads(wake_raw or "")
        candidate = str(commit["candidate_sha"])
        parents = commit["expected_parents"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    expected_parents = [authority.head_sha]
    if record.kind is LocalJobKind.PR_MERGE_CONFLICT:
        expected_parents.append(authority.base_identity)
    if not candidate or commit.get("artifact_id") != artifact.artifact_id or parents != expected_parents:
        return None
    if push.get("candidate_sha") != candidate or push.get("artifact_id") != artifact.artifact_id or push.get("head_ref") != authority.head_ref:
        return None
    if wake.get("repository") != record.repository or wake.get("pr_number") != record.target_number or wake.get("head_sha") != candidate:
        return None
    return artifact.artifact_id, f"published:{candidate}"


def _target(record: LocalJobRecord, issue_root: Path, pr_root: Path) -> tuple[Path, os.stat_result] | None:
    try:
        workspace = Path(record.workspace_path)
        if not workspace.is_absolute() or workspace.name != "repository":
            return None
        target = workspace.parent
        expected_root = issue_root if record.kind is LocalJobKind.ISSUE_IMPLEMENTATION else pr_root
        expected = expected_root.resolve() / record.job_id / record.execution_incarnation
        if target.resolve() != expected or target.is_symlink() or workspace.is_symlink():
            return None
        target_stat = target.stat(follow_symlinks=False)
        if not stat.S_ISDIR(target_stat.st_mode):
            return None
        return target, target_stat
    except OSError:
        return None


def _writers_released(store: LocalJobStore, record: LocalJobRecord, target: Path) -> bool:
    continuation = store.get_continuation(record.job_id)
    if continuation is None:
        return set(path.name for path in target.iterdir()) <= {"repository"}
    if continuation.execution_incarnation != record.execution_incarnation or continuation.state is not ContinuationState.FINAL_CONFIRMED:
        return False
    try:
        provider = Path(continuation.provider_workspace).resolve()
        relative_provider = provider.relative_to(target.resolve())
        if not relative_provider.parts:
            return False
        attributed_roots = {"repository", relative_provider.parts[0]}
        if {path.name for path in target.iterdir()} != attributed_roots:
            return False
    except (OSError, ValueError):
        return False
    return bool(continuation.writer_owner) and writer_group_settled(continuation.writer_owner)


def _same_identity(path: Path, record: WorkspaceReclamationRecord) -> bool:
    try:
        observed = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return record.phase is WorkspaceReclamationPhase.DELETING
    return not path.is_symlink() and stat.S_ISDIR(observed.st_mode) and observed.st_dev == record.device and observed.st_ino == record.inode


def _record_decision(results: list[WorkspaceCleanupResult], result: WorkspaceCleanupResult) -> None:
    results.append(result)
    logger.info("Workspace cleanup decision={} job={} path={} reason={}", result.decision, result.job_id, result.path, result.reason)


def reclaim_finalized_workspaces(
    store: LocalJobStore,
    *,
    now: float | None = None,
    retention_seconds: float | None = None,
    issue_root: Path | None = None,
    pr_root: Path | None = None,
) -> tuple[WorkspaceCleanupResult, ...]:
    """Release and, when due, remove exact positively-finalized incarnations."""
    timestamp = time.time() if now is None else now
    retention = workspace_retention_seconds() if retention_seconds is None else retention_seconds
    issue_workspace_root = issue_root or default_issue_job_workspace_root()
    pr_workspace_root = pr_root or default_pr_repair_workspace_root()
    results: list[WorkspaceCleanupResult] = []
    for job in store.discover_settled_with_workspaces():
        path = str(Path(job.workspace_path).parent)
        lock = workspace_reclamation_lock(store, job)
        ensure_lock_directory(lock)
        try:
            with file_lock(lock):
                current = store.get(job.job_id)
                if current is None or current.execution_incarnation != job.execution_incarnation:
                    raise RuntimeError("durable job binding changed")
                evidence = authenticated_successful_disposition(store, current)
                target_info = _target(current, issue_workspace_root, pr_workspace_root)
                release = store.get_workspace_reclamation(current.job_id, current.execution_incarnation)
                if release is None:
                    if evidence is None or target_info is None or not _writers_released(store, current, target_info[0]):
                        _record_decision(results, WorkspaceCleanupResult(current.job_id, path, "retained", "positive release evidence is incomplete"))
                        continue
                    target, identity = target_info
                    if current.workspace_device <= 0 or current.workspace_inode <= 0 or (identity.st_dev, identity.st_ino) != (current.workspace_device, current.workspace_inode):
                        _record_decision(results, WorkspaceCleanupResult(current.job_id, path, "retained", "producer-bound physical identity is unavailable or changed"))
                        continue
                    release = store.release_workspace(WorkspaceReclamationRecord(current.job_id, current.execution_incarnation, current.kind, current.repository, str(target), evidence[0], evidence[1], timestamp, WorkspaceReclamationPhase.RELEASED, identity.st_dev, identity.st_ino))
                if release.phase is WorkspaceReclamationPhase.DELETED:
                    _record_decision(results, WorkspaceCleanupResult(current.job_id, release.workspace_root, "retained", "already deleted"))
                    continue
                if timestamp < release.first_release_at + retention:
                    _record_decision(results, WorkspaceCleanupResult(current.job_id, release.workspace_root, "retained", "retention period has not elapsed"))
                    continue
                target = Path(release.workspace_root)
                if evidence is None or evidence != (release.artifact_id, release.disposition):
                    _record_decision(results, WorkspaceCleanupResult(current.job_id, release.workspace_root, "retained", "release authority or physical identity changed"))
                    continue
                if target.exists():
                    if target_info is None or str(target_info[0]) != release.workspace_root or not _same_identity(target, release):
                        _record_decision(results, WorkspaceCleanupResult(current.job_id, release.workspace_root, "retained", "release authority or physical identity changed"))
                        continue
                    if not _writers_released(store, current, target):
                        _record_decision(results, WorkspaceCleanupResult(current.job_id, release.workspace_root, "retained", "workspace writer or provider-root ownership is unresolved"))
                        continue
                elif release.phase is not WorkspaceReclamationPhase.DELETING:
                    _record_decision(results, WorkspaceCleanupResult(current.job_id, release.workspace_root, "retained", "release authority or physical identity changed"))
                    continue
                if release.phase is WorkspaceReclamationPhase.RELEASED:
                    if not store.transition_workspace_reclamation(release, WorkspaceReclamationPhase.RELEASED, WorkspaceReclamationPhase.DELETING):
                        raise RuntimeError("could not checkpoint deleting phase")
                    release = store.get_workspace_reclamation(current.job_id, current.execution_incarnation) or release
                if target.exists():
                    shutil.rmtree(target)
                if target.exists():
                    raise RuntimeError("target remains after recursive deletion")
                if not store.transition_workspace_reclamation(release, WorkspaceReclamationPhase.DELETING, WorkspaceReclamationPhase.DELETED):
                    raise RuntimeError("could not checkpoint deleted phase")
                _record_decision(results, WorkspaceCleanupResult(current.job_id, release.workspace_root, "reclaimed", "positive release expired"))
        except Exception as exc:
            logger.opt(exception=True).error("Workspace cleanup decision=failed job={} path={} reason={}", job.job_id, path, exc)
            results.append(WorkspaceCleanupResult(job.job_id, path, "failed", str(exc)))
            continue
    return tuple(results)
