"""Conservative reclamation of positively finalized local-job workspaces."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
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
from .workspace_ownership import MARKER, verify_ownership

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


def _overlaps_protected_roots(target: Path, store: LocalJobStore) -> bool:
    """Reject targets intersecting configured credential or runtime locations."""
    home = Path.home()
    protected = [
        store.path,
        Path(os.environ.get("CODEX_HOME", str(home / ".codex"))),
        Path(os.environ.get("CLAUDE_CONFIG_DIR", str(home / ".claude"))),
        Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config"))) / "opencode",
        Path(os.environ.get("XDG_DATA_HOME", str(home / ".local" / "share"))) / "opencode",
        Path(os.environ.get("AUTO_CODER_RUNTIME_ROOT", tempfile.gettempdir())) / "local-invocations",
        target / "provider-runtime",
    ]
    configured_runtime = os.environ.get("AUTO_CODER_RUNTIME_ROOT")
    if configured_runtime:
        protected.insert(-1, Path(configured_runtime))
    try:
        resolved = target.resolve()
        for path in protected[:-1]:
            other = path.resolve()
            if resolved == other or resolved in other.parents or other in resolved.parents:
                return True
        return protected[-1].exists() or protected[-1].is_symlink()
    except (OSError, RuntimeError):
        return True


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


def authenticated_unsuccessful_disposition(store: LocalJobStore, record: LocalJobRecord) -> tuple[str, str] | None:
    """Verify an exact terminated result and independently available source inputs."""
    if record.state is not LocalJobState.SETTLED:
        return None
    continuation = store.get_continuation(record.job_id)
    if continuation is None or continuation.execution_incarnation != record.execution_incarnation or not continuation.executor_token or not continuation.writer_owner or not writer_group_settled(continuation.writer_owner):
        return None
    artifact = store.get_execution_result_artifact(record.job_id, record.execution_incarnation)
    if record.invocation_outcome in {InvocationOutcome.FAILED, InvocationOutcome.INTERRUPTED}:
        if continuation.state.value != record.invocation_outcome.value or artifact is None or artifact.artifact_id != record.result_reference or artifact.outcome != record.invocation_outcome:
            return None
        expected = hashlib.sha256(json.dumps([artifact.job_id, artifact.execution_incarnation, artifact.outcome.value, artifact.output], separators=(",", ":")).encode()).hexdigest()
        if expected != artifact.artifact_id:
            return None
        result = artifact.artifact_id, f"terminated:{record.invocation_outcome.value}"
    elif record.invocation_outcome is None and record.kind is LocalJobKind.ISSUE_IMPLEMENTATION:
        receipt = _effect(store, record, "explicit-retry-supersession")
        if artifact is not None or record.result_reference or not receipt or continuation.state is not ContinuationState.INTERRUPTED or not continuation.executor_token.startswith(f"retry:{receipt}:"):
            return None
        result = "", f"terminated:superseded:{receipt}"
    else:
        return None
    if hashlib.sha256(record.invocation_input.encode()).hexdigest() != record.input_identity or not record.invocation_input or store.workspace_has_pending_effects(record):
        return None
    source = store.workspace_reconstruction_source(record)
    target = Path(record.workspace_path).parent
    if not source or not record.source_ref or not record.repository or record.target_number <= 0:
        return None
    source_path = Path(source).resolve()
    if not source_path.is_dir() or source_path == target.resolve() or target.resolve() in source_path.parents:
        return None
    environment = {key: value for key, value in os.environ.items() if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_INDEX_FILE"}}
    environment["GIT_NO_LAZY_FETCH"] = "1"
    try:
        common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=source_path, env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=10, check=True)
        common_dir = Path(common.stdout.strip())
        if not common_dir.is_absolute():
            common_dir = source_path / common_dir
        common_dir = common_dir.resolve()
        objects = common_dir / "objects"
        resolved_objects = objects.resolve()
        if common_dir == target.resolve() or target.resolve() in common_dir.parents or resolved_objects == target.resolve() or target.resolve() in resolved_objects.parents or objects.is_symlink() or (objects / "info" / "alternates").exists():
            return None

        def fail_walk(error: OSError) -> None:
            raise error

        for current, directories, files in os.walk(objects, followlinks=False, onerror=fail_walk):
            if any((Path(current) / name).is_symlink() for name in (*directories, *files)):
                return None
    except (OSError, subprocess.SubprocessError):
        return None
    commits = [record.source_commit]
    if record.kind in {LocalJobKind.PR_CI_FAILURE, LocalJobKind.PR_MERGE_CONFLICT}:
        authority = store.get_pr_repair_claim(record.job_id)
        if authority is None or authority.repository != record.repository or authority.pr_number != record.target_number or authority.head_sha != record.source_commit or not authority.source_evidence_identity:
            return None
        commits.append(authority.base_identity)
    elif record.kind is not LocalJobKind.ISSUE_IMPLEMENTATION:
        return None
    for commit in commits:
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            return None
        try:
            checked_commit = subprocess.run(["git", "--no-replace-objects", "rev-list", "--objects", "--missing=print", commit], cwd=source_path, env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        if checked_commit.returncode or any(line.startswith(b"?") for line in checked_commit.stdout.splitlines()):
            return None
    return result


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


def _writers_released(store: LocalJobStore, record: LocalJobRecord, target: Path, *, deleting: bool = False) -> bool:
    anchor = store.workspace_ownership_anchor(record)
    if (anchor or (target / MARKER).exists()) and not verify_ownership(target, anchor, record.job_id, record.execution_incarnation):
        return False
    continuation = store.get_continuation(record.job_id)
    if continuation is None:
        return set(path.name for path in target.iterdir()) <= {"repository", MARKER}
    expected_state = (
        ContinuationState.FINAL_CONFIRMED
        if record.invocation_outcome is InvocationOutcome.COMPLETED
        else ContinuationState(record.invocation_outcome.value) if record.invocation_outcome in {InvocationOutcome.FAILED, InvocationOutcome.INTERRUPTED} else ContinuationState.INTERRUPTED if _effect(store, record, "explicit-retry-supersession") else None
    )
    if continuation.execution_incarnation != record.execution_incarnation or continuation.state is not expected_state:
        return False
    try:
        provider = Path(continuation.provider_workspace).resolve()
        relative_provider = provider.relative_to(target.resolve())
        if not relative_provider.parts:
            return False
        attributed_roots = {"repository", relative_provider.parts[0]}
        if (target / MARKER).exists():
            attributed_roots.add(MARKER)
        observed = {path.name for path in target.iterdir()}
        if (not deleting and observed != attributed_roots) or (deleting and not observed <= attributed_roots):
            return False
    except (OSError, ValueError):
        return False
    return bool(continuation.writer_owner) and writer_group_settled(continuation.writer_owner)


def _remove_owned_workspace(target: Path, anchor: str, job_id: str, incarnation: str) -> None:
    """Pin the verified directory while removing its children and marker."""
    fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        marker_fd = os.open(MARKER, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            marker_stat = os.fstat(marker_fd)
            anchor_stat = Path(anchor).stat(follow_symlinks=False)
            with os.fdopen(marker_fd, "r", encoding="utf-8", closefd=False) as stream:
                binding = json.load(stream)
            if not stat.S_ISREG(marker_stat.st_mode) or not stat.S_ISREG(anchor_stat.st_mode) or (marker_stat.st_dev, marker_stat.st_ino) != (anchor_stat.st_dev, anchor_stat.st_ino) or binding != [1, job_id, incarnation, str(target)]:
                raise RuntimeError("workspace ownership changed at deletion boundary")
        finally:
            os.close(marker_fd)
        for name in os.listdir(fd):
            if name == MARKER:
                continue
            entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(entry.st_mode):
                shutil.rmtree(name, dir_fd=fd)
            else:
                os.unlink(name, dir_fd=fd)
        os.unlink(MARKER, dir_fd=fd)
        observed = target.stat(follow_symlinks=False)
        pinned = os.fstat(fd)
        if (observed.st_dev, observed.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise RuntimeError("workspace path changed during deletion")
        target.rmdir()
    finally:
        os.close(fd)


def _matches_identity(observed: os.stat_result, device: int, inode: int, producer_root: Path) -> bool:
    """Compare a producer-bound physical identity, tolerating filesystem renumbering.

    Block device numbers are not stable across reboots or remounts (for example,
    NVMe enumeration order can change ``259:4`` into ``259:2``), while inode
    numbers on the same filesystem are. A differing device is accepted only when
    the inode matches and the producer root itself now reports the observed
    device, i.e. the whole filesystem was renumbered rather than the target
    being replaced by a directory from another filesystem.
    """
    if inode <= 0 or observed.st_ino != inode:
        return False
    if observed.st_dev == device:
        return True
    try:
        root_device = producer_root.stat().st_dev
    except OSError:
        return False
    return root_device == observed.st_dev


def _same_identity(path: Path, record: WorkspaceReclamationRecord, producer_root: Path) -> bool:
    try:
        observed = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return record.phase is WorkspaceReclamationPhase.DELETING
    return not path.is_symlink() and stat.S_ISDIR(observed.st_mode) and _matches_identity(observed, record.device, record.inode, producer_root)


def _record_decision(results: list[WorkspaceCleanupResult], result: WorkspaceCleanupResult) -> None:
    results.append(result)
    logger.info("Workspace cleanup decision={} job={} path={} reason={}", result.decision, result.job_id, result.path, result.reason)


def reclaim_unentered_workspaces(store: LocalJobStore, *, pr_root: Path | None = None) -> tuple[WorkspaceCleanupResult, ...]:
    """Dispose only incarnations durably released before provider entry."""
    root = (pr_root or default_pr_repair_workspace_root()).resolve()
    results: list[WorkspaceCleanupResult] = []
    for release in store.discover_unentered_workspace_reclamations():
        target = Path(release.workspace_root)
        lock = lock_path(release.repository, store.path, "local-job-workspace", f"{release.job_id}:{release.execution_incarnation}")
        try:
            ensure_lock_directory(lock)
            with file_lock(lock):
                current = store.get(release.job_id)
                if current is None or current.repository != release.repository or current.kind != release.kind or current.execution_incarnation == release.execution_incarnation:
                    raise RuntimeError("unentered release is not confirmed")
                if release.kind not in {LocalJobKind.PR_CI_FAILURE, LocalJobKind.PR_MERGE_CONFLICT} or target != root / release.job_id / release.execution_incarnation or target.resolve() != target:
                    raise RuntimeError("unentered workspace is outside its producer root")
                if store.get_execution_result_artifact(release.job_id, release.execution_incarnation) is not None:
                    raise RuntimeError("unentered workspace has retained result evidence")
                release = store.get_workspace_reclamation(release.job_id, release.execution_incarnation) or release
                if release.phase is WorkspaceReclamationPhase.DELETED:
                    continue
                if _overlaps_protected_roots(target, store):
                    raise RuntimeError("workspace overlaps a protected credential or runtime root")
                if target.exists():
                    anchor = store.get_workspace_ownership_anchor(release.job_id, release.execution_incarnation, str(target))
                    if (anchor or (target / MARKER).exists()) and not verify_ownership(target, anchor, release.job_id, release.execution_incarnation):
                        raise RuntimeError("unentered workspace ownership evidence changed")
                    if not _same_identity(target, release, root) or {path.name for path in target.iterdir()} - {"repository", MARKER}:
                        raise RuntimeError("unentered workspace identity or ownership changed")
                if release.phase is WorkspaceReclamationPhase.RELEASED:
                    if not store.transition_workspace_reclamation(release, WorkspaceReclamationPhase.RELEASED, WorkspaceReclamationPhase.DELETING):
                        raise RuntimeError("could not checkpoint unentered deletion")
                if target.exists():
                    shutil.rmtree(target)
                if not store.transition_workspace_reclamation(release, WorkspaceReclamationPhase.DELETING, WorkspaceReclamationPhase.DELETED):
                    raise RuntimeError("could not checkpoint unentered disposal")
                _record_decision(results, WorkspaceCleanupResult(release.job_id, str(target), "reclaimed", "positively unentered incarnation released"))
        except Exception as exc:
            logger.opt(exception=True).warning("Workspace cleanup decision=failed job={} path={} reason={}", release.job_id, target, exc)
            results.append(WorkspaceCleanupResult(release.job_id, str(target), "failed", str(exc)))
    return tuple(results)


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
    results = list(reclaim_unentered_workspaces(store, pr_root=pr_workspace_root))
    for job in store.discover_settled_with_workspaces():
        path = str(Path(job.workspace_path).parent)
        lock = workspace_reclamation_lock(store, job)
        ensure_lock_directory(lock)
        try:
            with file_lock(lock):
                current = store.get(job.job_id)
                if current is None or current.execution_incarnation != job.execution_incarnation:
                    raise RuntimeError("durable job binding changed")
                evidence = authenticated_successful_disposition(store, current) or authenticated_unsuccessful_disposition(store, current)
                target_info = _target(current, issue_workspace_root, pr_workspace_root)
                producer_root = issue_workspace_root if current.kind is LocalJobKind.ISSUE_IMPLEMENTATION else pr_workspace_root
                release = store.get_workspace_reclamation(current.job_id, current.execution_incarnation)
                if _overlaps_protected_roots(Path(current.workspace_path).parent, store):
                    _record_decision(results, WorkspaceCleanupResult(current.job_id, path, "retained", "workspace overlaps a protected credential or runtime root"))
                    continue
                if release is None:
                    anchor = store.workspace_ownership_anchor(current)
                    if target_info is not None and anchor and not verify_ownership(target_info[0], anchor, current.job_id, current.execution_incarnation):
                        _record_decision(results, WorkspaceCleanupResult(current.job_id, path, "retained", "producer-bound physical identity is unavailable or changed"))
                        continue
                    if current.invocation_outcome in {InvocationOutcome.FAILED, InvocationOutcome.INTERRUPTED} and not anchor:
                        _record_decision(results, WorkspaceCleanupResult(current.job_id, path, "retained", "workspace ownership is unverified"))
                        continue
                    if evidence is not None and evidence[1].startswith("terminated:") and (target_info is None or not verify_ownership(target_info[0], store.workspace_ownership_anchor(current), current.job_id, current.execution_incarnation)):
                        _record_decision(results, WorkspaceCleanupResult(current.job_id, path, "retained", "workspace ownership is unverified"))
                        continue
                    if evidence is None or target_info is None or not _writers_released(store, current, target_info[0]):
                        _record_decision(results, WorkspaceCleanupResult(current.job_id, path, "retained", "positive release evidence is incomplete"))
                        continue
                    target, identity = target_info
                    if current.workspace_device <= 0 or not _matches_identity(identity, current.workspace_device, current.workspace_inode, producer_root):
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
                    if release.disposition.startswith("terminated:") and not verify_ownership(target, store.workspace_ownership_anchor(current), current.job_id, current.execution_incarnation):
                        _record_decision(results, WorkspaceCleanupResult(current.job_id, release.workspace_root, "retained", "workspace ownership is unverified"))
                        continue
                    if target_info is None or str(target_info[0]) != release.workspace_root or not _same_identity(target, release, producer_root):
                        _record_decision(results, WorkspaceCleanupResult(current.job_id, release.workspace_root, "retained", "release authority or physical identity changed"))
                        continue
                    if not _writers_released(store, current, target, deleting=release.phase is WorkspaceReclamationPhase.DELETING):
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
                    if release.disposition.startswith("terminated:"):
                        _remove_owned_workspace(target, store.workspace_ownership_anchor(current), current.job_id, current.execution_incarnation)
                    else:
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
