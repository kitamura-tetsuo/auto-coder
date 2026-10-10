"""Persistent, job-owned Git workspaces for local Issue implementation.

This is the working-state producer only.  Publication and worker routing consume
its durable checkpoint in later stages.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .checkout_lock import checkout_lock
from .implementation_slots import ImplementationOwner, ImplementationSlotRepository
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


@dataclass(frozen=True)
class _FileIdentity:
    checksum: str
    mode: int
    symlink: bool


def default_issue_job_workspace_root() -> Path:
    return Path.home() / ".auto-coder" / "issue-job-workspaces"


class IssueJobWorkspaceProducer:
    """Prepare, execute and checkpoint an accepted Issue job without a shared lease."""

    def __init__(self, store: LocalJobStore, root: Path | None = None, implementation_slots: ImplementationSlotRepository | None = None) -> None:
        self._store = store
        self._root = (root or default_issue_job_workspace_root()).resolve()
        self._implementation_slots = implementation_slots

    @staticmethod
    def _git(cwd: Path, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        if result.returncode != 0:
            raise IssueJobWorkspaceError(result.stderr.strip() or f"git {' '.join(args)} failed")
        return result.stdout.strip()

    @staticmethod
    def _working_files(root: Path) -> dict[str, _FileIdentity]:
        """Read working files without trusting model-controlled Git index flags."""
        files: dict[str, _FileIdentity] = {}
        for current, names, filenames in os.walk(root, followlinks=False):
            symlinked_directories = [name for name in names if (Path(current) / name).is_symlink()]
            names[:] = [name for name in names if name != ".git" and name not in symlinked_directories]
            for name in filenames + symlinked_directories:
                path = Path(current) / name
                relative = str(path.relative_to(root))
                if path.is_symlink():
                    contents = os.fsencode(os.readlink(path))
                    mode = stat.S_IMODE(path.lstat().st_mode)
                    symlink = True
                else:
                    if not path.is_file():
                        raise IssueJobWorkspaceError(f"unsupported source file type: {relative}")
                    contents = path.read_bytes()
                    mode = stat.S_IMODE(path.stat().st_mode)
                    symlink = False
                files[relative] = _FileIdentity(hashlib.sha256(contents).hexdigest(), mode, symlink)
        return files

    @classmethod
    def _copy_working_files(cls, source: Path, destination: Path, expected: dict[str, _FileIdentity]) -> None:
        destination_files = cls._working_files(destination)
        for relative in destination_files.keys() - expected.keys():
            (destination / relative).unlink()
        for relative, identity in expected.items():
            source_path = source / relative
            destination_path = destination / relative
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            if destination_path.exists() or destination_path.is_symlink():
                destination_path.unlink()
            if identity.symlink:
                destination_path.symlink_to(os.readlink(source_path))
            else:
                shutil.copyfile(source_path, destination_path, follow_symlinks=False)
                destination_path.chmod(identity.mode)

    def _prepare(self, claim: LocalJobClaim, source: IssueJobSource) -> Path:
        record = claim.record
        if record.kind is not LocalJobKind.ISSUE_IMPLEMENTATION or record.repository != source.repository:
            raise IssueJobWorkspaceError("job source does not match accepted Issue authority")
        repository = source.repository_path.resolve()
        workspace = self._root / record.job_id / record.execution_incarnation / "repository"
        if workspace.exists():
            raise IssueJobWorkspaceError("job workspace already exists without a durable binding")
        workspace.parent.mkdir(parents=True, exist_ok=True)
        try:
            # The shared lease is held only while an exact source snapshot is
            # established. It is released before the callback/model can enter.
            with checkout_lock(str(repository)):
                actual = self._git(repository, "rev-parse", f"{source.source_ref}^{{commit}}")
                if actual != source.source_commit:
                    raise IssueJobWorkspaceError("authorized source ref no longer identifies the pinned commit")
                checkout_commit = self._git(repository, "rev-parse", "HEAD")
                if checkout_commit != source.source_commit:
                    raise IssueJobWorkspaceError("shared checkout does not contain the authorized source commit")
                source_symbolic_ref = subprocess.run(
                    ["git", "rev-parse", "--symbolic-full-name", source.source_ref],
                    cwd=repository,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                checkout_symbolic_ref = subprocess.run(
                    ["git", "symbolic-ref", "-q", "HEAD"],
                    cwd=repository,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                if source_symbolic_ref.startswith("refs/heads/") and checkout_symbolic_ref != source_symbolic_ref:
                    raise IssueJobWorkspaceError("shared checkout is not on the authorized source ref")
                source_files = self._working_files(repository)
                source_index_tree = self._git(repository, "write-tree")
                staged_patch = subprocess.run(
                    ["git", "diff", "--cached", "--binary", "--full-index", source.source_commit, "--"],
                    cwd=repository,
                    capture_output=True,
                )
                if staged_patch.returncode != 0:
                    raise IssueJobWorkspaceError("authorized source index could not be captured")
                result = subprocess.run(["git", "clone", "--no-hardlinks", "--no-checkout", str(repository), str(workspace)], capture_output=True, text=True)
                if result.returncode != 0:
                    raise IssueJobWorkspaceError(result.stderr.strip() or "private clone failed")
                self._git(workspace, "checkout", "--detach", source.source_commit)
                self._git(workspace, "switch", "-c", source.work_branch)
                remote_result = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repository, capture_output=True, text=True)
                publication_remote = remote_result.stdout.strip() if remote_result.returncode == 0 else str(repository)
                self._git(workspace, "remote", "set-url", "origin", publication_remote)
                if staged_patch.stdout:
                    apply_index = subprocess.run(
                        ["git", "apply", "--cached", "--binary", "-"],
                        cwd=workspace,
                        input=staged_patch.stdout,
                        capture_output=True,
                    )
                    if apply_index.returncode != 0:
                        raise IssueJobWorkspaceError("authorized source index could not be restored")
                self._copy_working_files(repository, workspace, source_files)
                if self._git(repository, "write-tree") != source_index_tree or self._git(workspace, "write-tree") != source_index_tree or self._working_files(repository) != source_files or self._working_files(workspace) != source_files:
                    raise IssueJobWorkspaceError("authorized source working state changed during capture")
                if self._git(workspace, "rev-parse", "HEAD") != source.source_commit:
                    raise IssueJobWorkspaceError("private workspace source verification failed")
            owner = ImplementationOwner("issue", record.target_number)
            owner_incarnation = self._implementation_slots.owner_incarnation(owner) if self._implementation_slots is not None else None
            owner_generation = self._implementation_slots.implementation_generation(owner) if self._implementation_slots is not None else None
            if not self._store.bind_workspace(
                claim,
                workspace_path=workspace,
                source_commit=source.source_commit,
                source_ref=source.source_ref,
                work_branch=source.work_branch,
                publication_remote=publication_remote,
                owner_incarnation=owner_incarnation or "",
                owner_generation=owner_generation,
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
        initial_files = self._working_files(workspace)
        try:
            output = invoke(workspace, claim.record.invocation_input)
        except BaseException:
            # RUNNING plus its bound path is deliberately recoverable and cannot be
            # mistaken for a successful result.
            raise
        if not output:
            raise IssueJobWorkspaceError("local invocation returned no confirmed result")
        lines = [line for line in output.strip().splitlines() if line.strip()]
        if len(lines) != 1 or not lines[0].startswith("ACTION_SUMMARY:") or not lines[0].removeprefix("ACTION_SUMMARY:").strip():
            raise IssueJobWorkspaceError("local invocation did not return a confirmed implementation result")

        head = self._git(workspace, "rev-parse", "HEAD")
        if head != source.source_commit:
            raise IssueJobWorkspaceError("model changed private HEAD; refusing result checkpoint")
        final_files = self._working_files(workspace)
        changed_files = {name: ({"checksum": final_state.checksum, "mode": final_state.mode, "symlink": final_state.symlink} if final_state is not None else None) for name in sorted(initial_files.keys() | final_files.keys()) if (final_state := final_files.get(name)) != initial_files.get(name)}
        manifest_json = json.dumps(
            {
                "workspace": str(workspace),
                "source_commit": source.source_commit,
                "source_ref": source.source_ref,
                "work_branch": source.work_branch,
                "changed_files": changed_files,
                "workspace_files": {name: {"checksum": identity.checksum, "mode": identity.mode, "symlink": identity.symlink} for name, identity in sorted(final_files.items())},
                "output": output,
            },
            sort_keys=True,
        )
        artifact = self._store.persist_result_artifact(claim, InvocationOutcome.COMPLETED, manifest_json)
        if artifact is None or not self._store.record_result(claim, InvocationOutcome.COMPLETED, artifact.artifact_id):
            raise IssueJobWorkspaceError("successful workspace result could not be durably checkpointed")
        return IssueJobCheckpoint(record.job_id, record.execution_incarnation, workspace, source.source_commit, source.work_branch, artifact.artifact_id, output)
