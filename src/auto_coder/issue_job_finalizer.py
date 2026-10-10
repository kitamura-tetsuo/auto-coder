"""Crash-safe publication of completed local Issue implementation jobs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from .checkout_lock import checkout_lock
from .git_branch import git_commit_with_retry
from .git_commit import git_push
from .implementation_slots import ImplementationOwner, ImplementationSlotRepository
from .local_job_handoff import (
    InvocationOutcome,
    LocalJobClaim,
    LocalJobKind,
    LocalJobRecord,
    LocalJobState,
    LocalJobStore,
)


@dataclass(frozen=True)
class IssuePublicationResult:
    job_id: str
    disposition: str
    pr_number: Optional[int] = None
    diagnostic: str = ""


class IssueJobFinalizer:
    """Consume exact completed artifacts and checkpoint every external effect."""

    def __init__(
        self,
        store: LocalJobStore,
        slots: ImplementationSlotRepository,
        lookup_pr: Callable[[str, str], Optional[Mapping[str, object]]],
        create_pr: Callable[[LocalJobRecord, str], object],
    ) -> None:
        self._store = store
        self._slots = slots
        self._lookup_pr = lookup_pr
        self._create_pr = create_pr

    @staticmethod
    def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True)

    @staticmethod
    def _claim(record: LocalJobRecord) -> LocalJobClaim:
        # Downstream ownership retains the runner incarnation; it is not a new
        # execution claim and therefore must never mint a replacement identity.
        return LocalJobClaim(record, True)

    def resume_all(self, repository: str) -> tuple[IssuePublicationResult, ...]:
        results = []
        for record in self._store.discover_unsettled():
            if (
                record.repository == repository
                and record.kind is LocalJobKind.ISSUE_IMPLEMENTATION
                and record.state
                in {
                    LocalJobState.RESULT_RECORDED,
                    LocalJobState.DOWNSTREAM_EFFECTS_PENDING,
                }
            ):
                results.append(self.finalize(record.job_id))
        return tuple(results)

    def finalize(self, job_id: str) -> IssuePublicationResult:
        record = self._store.get(job_id)
        if record is not None and record.workspace_path:
            with checkout_lock(record.workspace_path):
                return self._finalize_locked(job_id)
        return self._finalize_locked(job_id)

    def _finalize_locked(self, job_id: str) -> IssuePublicationResult:
        """Finalize while excluding concurrent consumers of the job workspace."""
        record = self._store.get(job_id)
        if record is None or record.kind is not LocalJobKind.ISSUE_IMPLEMENTATION:
            return IssuePublicationResult(job_id, "rejected", diagnostic="missing or non-Issue job")
        claim = self._claim(record)
        if record.state not in {LocalJobState.RESULT_RECORDED, LocalJobState.DOWNSTREAM_EFFECTS_PENDING}:
            return IssuePublicationResult(job_id, "pending", diagnostic=f"job state is {record.state.value}")
        artifact = self._store.get_result_artifact(record.result_reference)
        if artifact is None or artifact.job_id != record.job_id or artifact.execution_incarnation != record.execution_incarnation or artifact.outcome != record.invocation_outcome:
            return IssuePublicationResult(job_id, "pending", diagnostic="exact result artifact is unavailable")

        if record.invocation_outcome is not InvocationOutcome.COMPLETED:
            if record.state is LocalJobState.RESULT_RECORDED and not self._store.mark_downstream_pending(claim):
                return IssuePublicationResult(job_id, "pending", diagnostic="could not checkpoint disposition")
            refreshed = self._store.get(job_id)
            if refreshed is None or not self._store.settle(self._claim(refreshed), record.invocation_outcome.value):
                return IssuePublicationResult(job_id, "pending", diagnostic="could not settle unsuccessful result")
            self._finish_execution(refreshed)
            return IssuePublicationResult(job_id, record.invocation_outcome.value)

        owner = ImplementationOwner("issue", record.target_number)
        if not record.owner_incarnation or self._slots.owner_incarnation(owner) != record.owner_incarnation or self._slots.implementation_generation(owner) != record.owner_generation:
            return IssuePublicationResult(job_id, "pending", diagnostic="exact implementation owner is no longer current")

        try:
            manifest = json.loads(artifact.output)
        except (TypeError, json.JSONDecodeError):
            return IssuePublicationResult(job_id, "pending", diagnostic="result manifest is unreadable")
        workspace = Path(record.workspace_path)
        if (
            not workspace.is_absolute()
            or str(manifest.get("workspace", "")) != str(workspace)
            or manifest.get("source_commit") != record.source_commit
            or manifest.get("work_branch") != record.work_branch
            or not isinstance(manifest.get("changed_files"), dict)
            or not isinstance(manifest.get("workspace_files"), dict)
        ):
            return IssuePublicationResult(job_id, "pending", diagnostic="result manifest contradicts job authority")
        actual_files: dict[str, dict[str, object]] = {}
        for current, names, filenames in os.walk(workspace, followlinks=False):
            symlinked_directories = [name for name in names if (Path(current) / name).is_symlink()]
            names[:] = [name for name in names if name != ".git" and name not in symlinked_directories]
            for name in filenames + symlinked_directories:
                path = Path(current) / name
                relative = str(path.relative_to(workspace))
                contents = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
                actual_files[relative] = {
                    "checksum": hashlib.sha256(contents).hexdigest(),
                    "mode": stat.S_IMODE(path.lstat().st_mode if path.is_symlink() else path.stat().st_mode),
                    "symlink": path.is_symlink(),
                }
        if actual_files != manifest["workspace_files"]:
            return IssuePublicationResult(job_id, "pending", diagnostic="retained workspace differs from the confirmed result")
        for relative, expected in manifest["changed_files"].items():
            if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
                return IssuePublicationResult(job_id, "pending", diagnostic="result manifest contains an unsafe path")
            path = workspace / relative
            if expected is None:
                if path.exists() or path.is_symlink():
                    return IssuePublicationResult(job_id, "pending", diagnostic="retained workspace differs from the confirmed result")
                continue
            if not isinstance(expected, dict) or not (path.exists() or path.is_symlink()):
                return IssuePublicationResult(job_id, "pending", diagnostic="retained workspace differs from the confirmed result")
            contents = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
            if hashlib.sha256(contents).hexdigest() != expected.get("checksum"):
                return IssuePublicationResult(job_id, "pending", diagnostic="retained workspace differs from the confirmed result")
        branch = self._git(workspace, "branch", "--show-current")
        head = self._git(workspace, "rev-parse", "HEAD")
        remote = self._git(workspace, "remote", "get-url", "origin")
        ancestry = self._git(workspace, "merge-base", "--is-ancestor", record.source_commit, "HEAD")
        if branch.returncode or branch.stdout.strip() != record.work_branch or head.returncode or ancestry.returncode or remote.returncode or remote.stdout.strip() != record.publication_remote:
            return IssuePublicationResult(job_id, "pending", diagnostic="retained workspace no longer has the authorized branch history")

        if record.state is LocalJobState.RESULT_RECORDED:
            if not self._store.mark_downstream_pending(claim):
                return IssuePublicationResult(job_id, "pending", diagnostic="could not transfer result to finalization")
            record = self._store.get(job_id) or record
            claim = self._claim(record)

        commit_sha = self._completed_evidence(record, "commit")
        if commit_sha and head.stdout.strip() != commit_sha:
            return IssuePublicationResult(job_id, "pending", diagnostic="checkpointed commit is no longer the workspace HEAD")
        if not commit_sha:
            status = self._git(workspace, "status", "--porcelain")
            if status.returncode:
                return self._pending(claim, "commit", status.stderr or "git status failed")
            if not status.stdout.strip() and head.stdout.strip() == record.source_commit:
                self._store.record_effect(claim, "commit", "skipped", "authenticated no-change result")
                self._store.record_effect(claim, "publication", "skipped", "no changes")
                if not self._store.settle(claim, "no_change"):
                    return IssuePublicationResult(job_id, "pending", diagnostic="could not settle no-change result")
                self._finish_execution(record)
                return IssuePublicationResult(job_id, "no_change")
            if not status.stdout.strip() and head.stdout.strip() != record.source_commit:
                parent = self._git(workspace, "rev-parse", "HEAD^")
                if parent.returncode or parent.stdout.strip() != record.source_commit:
                    return self._pending(claim, "commit", "existing commit is not the exact controller commit", "indeterminate")
                commit_sha = head.stdout.strip()
                if not self._store.record_effect(claim, "commit", "completed", commit_sha):
                    return IssuePublicationResult(job_id, "pending", diagnostic="recovered commit checkpoint failed")
            else:
                add = self._git(workspace, "add", "-A")
                if add.returncode:
                    return self._pending(claim, "commit", add.stderr or "git add failed")
                committed = git_commit_with_retry(f"Auto-Coder: Address issue #{record.target_number}", cwd=str(workspace))
                if not committed.success:
                    return self._pending(claim, "commit", committed.stderr or "commit failed", "failed")
                head = self._git(workspace, "rev-parse", "HEAD")
                if head.returncode or not head.stdout.strip() or head.stdout.strip() == record.source_commit:
                    return self._pending(claim, "commit", "commit did not produce a new exact head", "failed")
                commit_sha = head.stdout.strip()
                if not self._store.record_effect(claim, "commit", "completed", commit_sha):
                    return IssuePublicationResult(job_id, "pending", diagnostic="commit checkpoint failed")

        remote = self._git(workspace, "ls-remote", "--heads", "origin", f"refs/heads/{record.work_branch}")
        remote_sha = remote.stdout.split()[0] if remote.returncode == 0 and remote.stdout.strip() else ""
        if remote.returncode != 0:
            return self._pending(claim, "push", remote.stderr or "remote branch lookup unavailable", "indeterminate")
        if remote_sha != commit_sha:
            self._store.record_effect(claim, "push", "pending", commit_sha)
            pushed = git_push(cwd=str(workspace), remote="origin", branch=record.work_branch, commit_message=f"Auto-Coder: Address issue #{record.target_number}")
            if not pushed.success:
                return self._pending(claim, "push", pushed.stderr or "push failed", "indeterminate")
            confirmed = self._git(workspace, "ls-remote", "--heads", "origin", f"refs/heads/{record.work_branch}")
            confirmed_sha = confirmed.stdout.split()[0] if confirmed.returncode == 0 and confirmed.stdout.strip() else ""
            if confirmed.returncode != 0 or confirmed_sha != commit_sha:
                return self._pending(claim, "push", "push could not be authoritatively confirmed", "indeterminate")
        if not self._store.record_effect(claim, "push", "completed", commit_sha):
            return IssuePublicationResult(job_id, "pending", diagnostic="push checkpoint failed")

        try:
            existing = self._lookup_pr(record.repository, record.work_branch)
        except Exception as exc:
            return self._pending(claim, "pr_lookup", f"PR lookup unavailable: {exc}", "indeterminate")
        pr_number = self._attributable_pr(existing, record)
        if existing is not None and pr_number is None:
            return self._pending(claim, "pr", "branch is associated with an unrelated or contradictory PR", "indeterminate")
        if pr_number is None:
            prior_pr_effect = self._store.get_effect(record.job_id, record.execution_incarnation, "pr")
            if prior_pr_effect is not None:
                return self._pending(claim, "pr", "prior PR creation requires authoritative reconciliation", "indeterminate")
            self._store.record_effect(claim, "pr", "pending", "create requested")
            try:
                self._create_pr(record, str(manifest.get("output", "")))
            except Exception:
                pass
            try:
                existing = self._lookup_pr(record.repository, record.work_branch)
            except Exception as exc:
                return self._pending(claim, "pr", f"PR create result is unknown and reconciliation failed: {exc}", "indeterminate")
            pr_number = self._attributable_pr(existing, record)
            if pr_number is None:
                return self._pending(claim, "pr", "PR creation is not authoritatively confirmed", "indeterminate")
        if not self._slots.record_implementation_pr_if_current(owner, pr_number, record.owner_incarnation, record.owner_generation):
            return self._pending(claim, "association", f"ownership association for PR #{pr_number} failed")
        self._store.record_effect(claim, "pr", "completed", str(pr_number))
        if not self._store.record_effect(claim, "association", "completed", str(pr_number)) or not self._store.settle(claim, f"pr:{pr_number}"):
            return IssuePublicationResult(job_id, "pending", pr_number, "final association checkpoint failed")
        self._finish_execution(record)
        return IssuePublicationResult(job_id, "published", pr_number)

    def _finish_execution(self, record: LocalJobRecord) -> None:
        """Release only the exact execution transferred by the Issue worker."""
        if not record.implementation_execution_id:
            return
        owner = ImplementationOwner("issue", record.target_number)
        if self._slots.owner_incarnation(owner) == record.owner_incarnation and self._slots.implementation_generation(owner) == record.owner_generation:
            self._slots.finish_execution(owner, record.implementation_execution_id)

    def _completed_evidence(self, record: LocalJobRecord, name: str) -> str:
        effect = self._store.get_effect(record.job_id, record.execution_incarnation, name)
        return effect.evidence if effect is not None and effect.state == "completed" else ""

    def _pending(self, claim: LocalJobClaim, name: str, diagnostic: str, state: str = "pending") -> IssuePublicationResult:
        self._store.record_effect(claim, name, state, diagnostic)
        return IssuePublicationResult(claim.record.job_id, "pending", diagnostic=diagnostic)

    @staticmethod
    def _attributable_pr(pr: Optional[Mapping[str, object]], record: LocalJobRecord) -> Optional[int]:
        if pr is None:
            return None
        number = pr.get("number")
        head = pr.get("head")
        head_ref = head.get("ref") if isinstance(head, Mapping) else pr.get("head_ref")
        body = str(pr.get("body") or "")
        closes_target = re.search(rf"(?:^|\W)Closes\s+#{record.target_number}(?!\d)", body) is not None
        if isinstance(number, int) and head_ref == record.work_branch and closes_target:
            return number
        return None
