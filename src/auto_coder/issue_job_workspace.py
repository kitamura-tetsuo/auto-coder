"""Persistent, job-owned Git workspaces for local Issue implementation.

This is the working-state producer only.  Publication and worker routing consume
its durable checkpoint in later stages.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .checkout_lock import checkout_lock
from .cli_event_stream import extract_cli_response
from .implementation_slots import ImplementationOwner, ImplementationSlotRepository
from .local_job_handoff import InvocationOutcome, LocalJobClaim, LocalJobKind, LocalJobStore
from .security_utils import redact_string
from .worktree_utils import DISPOSABLE_DIRECTORY_NAMES


class IssueJobWorkspaceError(RuntimeError):
    """The authorized source could not be prepared or safely checkpointed."""


class IssueJobCannotFix(IssueJobWorkspaceError):
    """The local provider positively returned the terminal cannot-fix result."""


@dataclass(frozen=True)
class IssueJobSource:
    repository: str
    repository_path: Path
    base_branch: str
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
    def working_file_identities(root: Path) -> dict[str, _FileIdentity]:
        """Read working files without trusting model-controlled Git index flags."""
        tracked_result = subprocess.run(["git", "ls-files", "--cached", "-z"], cwd=root, capture_output=True, check=True)
        tracked = {os.fsdecode(path) for path in tracked_result.stdout.split(b"\0") if path}
        tracked_directories = {str(parent) for relative in tracked for parent in Path(relative).parents}
        files: dict[str, _FileIdentity] = {}
        for current, names, filenames in os.walk(root, followlinks=False):
            symlinked_directories = [name for name in names if (Path(current) / name).is_symlink()]
            names[:] = [name for name in names if name != ".git" and name not in symlinked_directories and (name not in DISPOSABLE_DIRECTORY_NAMES or str((Path(current) / name).relative_to(root)) in tracked_directories)]
            for name in filenames + symlinked_directories:
                path = Path(current) / name
                relative = str(path.relative_to(root))
                if relative not in tracked and any(part in DISPOSABLE_DIRECTORY_NAMES for part in Path(relative).parts):
                    continue
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

    def _prepare(self, claim: LocalJobClaim, source: IssueJobSource) -> tuple[Path, str, str]:
        record = claim.record
        if record.kind is not LocalJobKind.ISSUE_IMPLEMENTATION or record.repository != source.repository:
            raise IssueJobWorkspaceError("job source does not match accepted Issue authority")
        repository = source.repository_path.resolve()
        workspace = self._root / record.job_id / record.execution_incarnation / "repository"
        if workspace.exists():
            raise IssueJobWorkspaceError("job workspace already exists without a durable binding")
        workspace.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Read only the publication remote from the controller checkout.
            # Fetch into the private repository so its HEAD, index, working files,
            # and stale remote-tracking refs cannot become implementation input.
            with checkout_lock(str(repository)):
                publication_remote = self._git(repository, "remote", "get-url", "origin")
            self._git(repository, "check-ref-format", f"refs/heads/{source.base_branch}")
            self._git(workspace.parent, "init", str(workspace))
            self._git(workspace, "remote", "add", "origin", publication_remote)
            source_ref = f"refs/remotes/origin/{source.base_branch}"
            self._git(workspace, "fetch", "--no-tags", "origin", f"+refs/heads/{source.base_branch}:{source_ref}")
            source_commit = self._git(workspace, "rev-parse", f"{source_ref}^{{commit}}")
            self._git(workspace, "switch", "--no-track", "-c", source.work_branch, source_commit)
            if self._git(workspace, "rev-parse", "HEAD") != source_commit or self._git(workspace, "status", "--porcelain"):
                raise IssueJobWorkspaceError("private workspace source verification failed")
            owner = ImplementationOwner("issue", record.target_number)
            owner_incarnation = self._implementation_slots.owner_incarnation(owner) if self._implementation_slots is not None else None
            owner_generation = self._implementation_slots.implementation_generation(owner) if self._implementation_slots is not None else None
            if not self._store.bind_workspace(
                claim,
                workspace_path=workspace,
                source_commit=source_commit,
                source_ref=source_ref,
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
        return workspace, source_commit, source_ref

    def execute(
        self,
        claim: LocalJobClaim,
        source: IssueJobSource,
        invoke: Callable[[Path, str], str],
        *,
        checkpoint_result: bool = True,
    ) -> IssueJobCheckpoint:
        """Run one invocation and retain its clone until downstream retirement."""
        record = claim.record
        workspace, source_commit, source_ref = self._prepare(claim, source)
        initial_files = self.working_file_identities(workspace)
        try:
            output = extract_cli_response(invoke(workspace, claim.record.invocation_input))
        except BaseException:
            # RUNNING plus its bound path is deliberately recoverable and cannot be
            # mistaken for a successful result.
            raise
        if not output:
            raise IssueJobWorkspaceError("local invocation returned no confirmed result")
        if output.strip() == "CANNOT_FIX":
            raise IssueJobCannotFix("local invocation returned CANNOT_FIX")
        lines = [line for line in output.strip().splitlines() if line.strip()]
        if len(lines) != 1 or not lines[0].startswith("ACTION_SUMMARY:") or not lines[0].removeprefix("ACTION_SUMMARY:").strip():
            raise IssueJobWorkspaceError("local invocation did not return a confirmed implementation result; response=" + redact_string(output.strip())[:2000])

        head = self._git(workspace, "rev-parse", "HEAD")
        if head != source_commit:
            raise IssueJobWorkspaceError("model changed private HEAD; refusing result checkpoint")
        final_files = self.working_file_identities(workspace)
        changed_files = {name: ({"checksum": final_state.checksum, "mode": final_state.mode, "symlink": final_state.symlink} if final_state is not None else None) for name in sorted(initial_files.keys() | final_files.keys()) if (final_state := final_files.get(name)) != initial_files.get(name)}
        manifest_json = json.dumps(
            {
                "workspace": str(workspace),
                "source_commit": source_commit,
                "source_ref": source_ref,
                "work_branch": source.work_branch,
                "changed_files": changed_files,
                "workspace_files": {name: {"checksum": identity.checksum, "mode": identity.mode, "symlink": identity.symlink} for name, identity in sorted(final_files.items())},
                "output": output,
            },
            sort_keys=True,
        )
        if not checkpoint_result:
            return IssueJobCheckpoint(record.job_id, record.execution_incarnation, workspace, source_commit, source.work_branch, manifest_json, output)
        artifact = self._store.persist_result_artifact(
            claim,
            InvocationOutcome.COMPLETED,
            manifest_json,
            executor_token=claim.record.runner_owner,
        )
        if artifact is None or not self._store.record_result(
            claim,
            InvocationOutcome.COMPLETED,
            artifact.artifact_id,
            executor_token=claim.record.runner_owner,
        ):
            raise IssueJobWorkspaceError("successful workspace result could not be durably checkpointed")
        return IssueJobCheckpoint(record.job_id, record.execution_incarnation, workspace, source_commit, source.work_branch, artifact.artifact_id, output)
