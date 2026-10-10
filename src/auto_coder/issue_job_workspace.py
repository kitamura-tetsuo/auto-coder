"""Persistent, job-owned Git workspaces for local Issue implementation.

This is the working-state producer only.  Publication and worker routing consume
its durable checkpoint in later stages.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .local_job_handoff import InvocationOutcome, LocalJobClaim, LocalJobKind, LocalJobStore


class IssueJobWorkspaceError(RuntimeError):
    """The authorized source could not be prepared or safely checkpointed."""


@dataclass(frozen=True)
class IssueJobSource:
    repository: str
    repository_path: Path
    source_ref: str
    source_commit: str
    work_branch: str


@dataclass(frozen=True)
class IssueJobCheckpoint:
    job_id: str
    execution_incarnation: str
    workspace: Path
    source_commit: str
    work_branch: str
    result_reference: str
    output: str


def default_issue_job_workspace_root() -> Path:
    return Path.home() / ".auto-coder" / "issue-job-workspaces"


class IssueJobWorkspaceProducer:
    """Prepare, execute and checkpoint an accepted Issue job without a shared lease."""

    def __init__(self, store: LocalJobStore, root: Path | None = None) -> None:
        self._store = store
        self._root = (root or default_issue_job_workspace_root()).resolve()

    @staticmethod
    def _git(cwd: Path, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        if result.returncode != 0:
            raise IssueJobWorkspaceError(result.stderr.strip() or f"git {' '.join(args)} failed")
        return result.stdout.strip()

    def _prepare(self, claim: LocalJobClaim, source: IssueJobSource) -> Path:
        record = claim.record
        if record.kind is not LocalJobKind.ISSUE_IMPLEMENTATION or record.repository != source.repository:
            raise IssueJobWorkspaceError("job source does not match accepted Issue authority")
        repository = source.repository_path.resolve()
        actual = self._git(repository, "rev-parse", f"{source.source_ref}^{{commit}}")
        if actual != source.source_commit:
            raise IssueJobWorkspaceError("authorized source ref no longer identifies the pinned commit")

        workspace = self._root / record.job_id / record.execution_incarnation / "repository"
        if workspace.exists():
            raise IssueJobWorkspaceError("job workspace already exists without a durable binding")
        workspace.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(["git", "clone", "--no-hardlinks", "--no-checkout", str(repository), str(workspace)], capture_output=True, text=True)
        if result.returncode != 0:
            raise IssueJobWorkspaceError(result.stderr.strip() or "private clone failed")
        try:
            self._git(workspace, "checkout", "--detach", source.source_commit)
            self._git(workspace, "switch", "-c", source.work_branch)
            if self._git(workspace, "rev-parse", "HEAD") != source.source_commit:
                raise IssueJobWorkspaceError("private workspace source verification failed")
            if not self._store.bind_workspace(
                claim,
                workspace_path=workspace,
                source_commit=source.source_commit,
                source_ref=source.source_ref,
                work_branch=source.work_branch,
            ):
                raise IssueJobWorkspaceError("job authority changed before workspace binding")
        except Exception:
            # Keep a possibly useful clone for diagnosis. It is never reused without
            # the durable exact-incarnation binding above.
            raise
        return workspace

    def execute(
        self,
        claim: LocalJobClaim,
        source: IssueJobSource,
        invoke: Callable[[Path, str], str],
    ) -> IssueJobCheckpoint:
        """Run one invocation and retain its clone until downstream retirement."""
        record = claim.record
        workspace = self._prepare(claim, source)
        try:
            output = invoke(workspace, claim.record.invocation_input)
        except BaseException:
            # RUNNING plus its bound path is deliberately recoverable and cannot be
            # mistaken for a successful result.
            raise
        if not output:
            raise IssueJobWorkspaceError("local invocation returned no confirmed result")

        head = self._git(workspace, "rev-parse", "HEAD")
        if head != source.source_commit:
            raise IssueJobWorkspaceError("model changed private HEAD; refusing result checkpoint")
        diff = subprocess.run(["git", "diff", "--binary", "--no-ext-diff", source.source_commit, "--"], cwd=workspace, capture_output=True).stdout
        untracked = self._git(workspace, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
        untracked_hashes = {}
        for name in untracked:
            path = workspace / name
            if not name:
                continue
            contents = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
            untracked_hashes[name] = hashlib.sha256(contents).hexdigest()
        state = json.dumps(
            {
                "workspace": str(workspace),
                "source_commit": source.source_commit,
                "source_ref": source.source_ref,
                "work_branch": source.work_branch,
                "diff_sha256": hashlib.sha256(diff).hexdigest(),
                "untracked_sha256": untracked_hashes,
                "output": output,
            },
            sort_keys=True,
        )
        artifact = self._store.persist_result_artifact(claim, InvocationOutcome.COMPLETED, state)
        if artifact is None or not self._store.record_result(claim, InvocationOutcome.COMPLETED, artifact.artifact_id):
            raise IssueJobWorkspaceError("successful workspace result could not be durably checkpointed")
        return IssueJobCheckpoint(record.job_id, record.execution_incarnation, workspace, source.source_commit, source.work_branch, artifact.artifact_id, output)
