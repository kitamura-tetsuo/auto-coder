"""Exact-source, retained workspaces and result evidence for local PR repairs."""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from .checkout_lock import checkout_lock
from .issue_job_workspace import IssueJobWorkspaceProducer
from .local_job_handoff import InvocationOutcome, LocalJobClaim, LocalJobKind, LocalJobRecord, LocalJobResultArtifact, LocalJobStore, RetainedPRRepairClaim


class PRRepairWorkspaceError(RuntimeError):
    """Exact PR source or retained output could not be verified."""


@dataclass(frozen=True)
class PRRepairSource:
    repository_path: Path


@dataclass(frozen=True)
class FileIdentity:
    checksum: str
    mode: int
    symlink: bool


@dataclass(frozen=True)
class PreparedPRRepairWorkspace:
    job_id: str
    execution_incarnation: str
    workspace: Path
    authority: RetainedPRRepairClaim
    remote: str
    source_files: tuple[tuple[str, FileIdentity], ...]


@dataclass(frozen=True)
class PRRepairResultManifest:
    schema_version: int
    job_id: str
    execution_incarnation: str
    upstream_incarnation: str
    kind: str
    workspace: str
    repository: str
    pr_number: int
    head_repository: str
    head_ref: str
    head_sha: str
    base_repository: str
    base_ref: str
    base_sha: str
    remote: str
    outcome: str
    completion_source: str
    observed_output: str
    actual_head: str
    merge_head: str
    changed_files: tuple[tuple[str, FileIdentity | None], ...]
    workspace_files: tuple[tuple[str, FileIdentity], ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class VerifiedPRRepairResult:
    manifest: PRRepairResultManifest
    artifact_id: str


def default_pr_repair_workspace_root() -> Path:
    return Path.home() / ".auto-coder" / "pr-repair-job-workspaces"


class PRRepairWorkspaceProducer:
    """Prepare H/B and describe output; adapters retain execution ownership."""

    def __init__(self, store: LocalJobStore, root: Path | None = None) -> None:
        self._store = store
        self._root = (root or default_pr_repair_workspace_root()).resolve()

    @staticmethod
    def _git(cwd: Path, *args: str, check: bool = True) -> str:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        if check and result.returncode != 0:
            raise PRRepairWorkspaceError(result.stderr.strip() or f"git {' '.join(args)} failed")
        return result.stdout.strip() if result.returncode == 0 else ""

    @staticmethod
    def _files(root: Path) -> dict[str, FileIdentity]:
        observed = IssueJobWorkspaceProducer.working_file_identities(root, maintained_disposable_directory_names=frozenset({"test-results"}))
        return {name: FileIdentity(value.checksum, value.mode, value.symlink) for name, value in observed.items()}

    @classmethod
    def _merge_head(cls, root: Path) -> str:
        """Return a verified merge commit, distinguishing absence from corruption."""
        merge_head_path = Path(cls._git(root, "rev-parse", "--git-path", "MERGE_HEAD"))
        if not merge_head_path.is_absolute():
            merge_head_path = root / merge_head_path
        if not merge_head_path.exists():
            return ""
        return cls._git(root, "rev-parse", "--verify", "MERGE_HEAD^{commit}")

    @classmethod
    def _commit_files(cls, root: Path, commit: str) -> dict[str, FileIdentity]:
        """Read the immutable source tree without checking it over retained output."""
        result = subprocess.run(["git", "ls-tree", "-rz", "--full-tree", commit], cwd=root, capture_output=True)
        if result.returncode != 0:
            raise PRRepairWorkspaceError(result.stderr.decode(errors="replace").strip() or "retained source tree is unreadable")
        files: dict[str, FileIdentity] = {}
        for entry in (item for item in result.stdout.split(b"\0") if item):
            metadata, encoded_name = entry.split(b"\t", 1)
            mode_text, object_type, object_id = metadata.decode("ascii").split()
            if object_type != "blob":
                raise PRRepairWorkspaceError(f"unsupported source entry type: {object_type}")
            contents = subprocess.run(["git", "cat-file", "blob", object_id], cwd=root, capture_output=True, check=True).stdout
            mode = int(mode_text, 8)
            files[encoded_name.decode("utf-8", errors="surrogateescape")] = FileIdentity(
                hashlib.sha256(contents).hexdigest(),
                0o777 if mode == 0o120000 else stat.S_IMODE(mode),
                mode == 0o120000,
            )
        return files

    @staticmethod
    def _changed(initial: dict[str, FileIdentity], final: dict[str, FileIdentity]) -> tuple[tuple[str, FileIdentity | None], ...]:
        return tuple((name, final.get(name)) for name in sorted(initial.keys() | final.keys()) if initial.get(name) != final.get(name))

    def prepare(self, claim: LocalJobClaim, source: PRRepairSource) -> PreparedPRRepairWorkspace:
        record = claim.record
        authority = self._store.get_pr_repair_claim(record.job_id)
        if not claim.acquired or record.kind not in {LocalJobKind.PR_CI_FAILURE, LocalJobKind.PR_MERGE_CONFLICT} or authority is None or authority.repository != record.repository or authority.pr_number != record.target_number or authority.upstream_incarnation != record.upstream_incarnation:
            raise PRRepairWorkspaceError("job does not match retained PR repair authority")
        if authority.head_repository != authority.base_repository or authority.base_repository != record.repository:
            raise PRRepairWorkspaceError("foreign-head PR repair workspaces are unsupported")
        repository = source.repository_path.resolve()
        workspace = self._root / record.job_id / record.execution_incarnation / "repository"
        if workspace.exists():
            raise PRRepairWorkspaceError("job workspace already exists without a new binding")
        workspace.parent.mkdir(parents=True, exist_ok=True)
        with checkout_lock(str(repository)):
            remote = self._git(repository, "remote", "get-url", "origin")
        for ref in (authority.head_ref, authority.base_ref):
            self._git(repository, "check-ref-format", f"refs/heads/{ref}")
        self._git(workspace.parent, "init", str(workspace))
        self._git(workspace, "remote", "add", "origin", remote)
        head_private_ref = "refs/remotes/authorized/pr-head"
        base_private_ref = "refs/remotes/authorized/pr-base"
        self._git(workspace, "fetch", "--no-tags", "origin", f"+refs/heads/{authority.head_ref}:{head_private_ref}")
        if self._git(workspace, "rev-parse", f"{head_private_ref}^{{commit}}") != authority.head_sha:
            raise PRRepairWorkspaceError("authorized PR head ref no longer resolves to retained H")
        self._git(workspace, "fetch", "--no-tags", "origin", f"+refs/heads/{authority.base_ref}:{base_private_ref}")
        if self._git(workspace, "rev-parse", f"{base_private_ref}^{{commit}}") != authority.base_identity:
            raise PRRepairWorkspaceError("authorized base ref no longer resolves to retained B")
        branch = f"repair-{record.target_number}-{record.execution_incarnation[:12]}"
        self._git(workspace, "switch", "--no-track", "-c", branch, authority.head_sha)
        if self._git(workspace, "rev-parse", "HEAD") != authority.head_sha or self._git(workspace, "status", "--porcelain"):
            raise PRRepairWorkspaceError("private PR workspace source verification failed")
        if not self._store.bind_workspace(
            claim,
            workspace_path=workspace,
            source_commit=authority.head_sha,
            source_ref=f"{authority.head_ref}@{authority.head_sha};{authority.base_ref}@{authority.base_identity}",
            work_branch=branch,
            publication_remote=remote,
            owner_incarnation=authority.upstream_incarnation,
            owner_generation=None,
        ):
            raise PRRepairWorkspaceError("job authority changed before workspace binding")
        source_files = self._commit_files(workspace, authority.head_sha)
        if source_files != self._files(workspace):
            raise PRRepairWorkspaceError("checked-out PR source does not match retained H tree")
        return PreparedPRRepairWorkspace(record.job_id, record.execution_incarnation, workspace, authority, remote, tuple(sorted(source_files.items())))

    def capture_result(
        self,
        claim: LocalJobClaim,
        prepared: PreparedPRRepairWorkspace,
        outcome: InvocationOutcome,
        observed_output: str,
        *,
        completion_source: str = "model",
    ) -> PRRepairResultManifest:
        record = self._store.get(claim.record.job_id)
        if record is None or record.execution_incarnation != prepared.execution_incarnation or Path(record.workspace_path).resolve() != prepared.workspace:
            raise PRRepairWorkspaceError("retained workspace binding changed before result capture")
        actual_head = self._git(prepared.workspace, "rev-parse", "HEAD")
        merge_head = self._merge_head(prepared.workspace)
        if actual_head != prepared.authority.head_sha:
            raise PRRepairWorkspaceError("repair changed HEAD before publication")
        if record.kind is LocalJobKind.PR_MERGE_CONFLICT and completion_source == "controller_verified_merge" and merge_head != prepared.authority.base_identity:
            raise PRRepairWorkspaceError("conflict result lacks the retained base merge lineage")
        if record.kind is LocalJobKind.PR_CI_FAILURE and merge_head:
            raise PRRepairWorkspaceError("CI edit unexpectedly contains merge state")
        if completion_source not in {"model", "controller_verified_merge"} or (completion_source == "controller_verified_merge" and record.kind is not LocalJobKind.PR_MERGE_CONFLICT):
            raise PRRepairWorkspaceError("unsupported repair completion source")
        if completion_source == "model" and not observed_output:
            raise PRRepairWorkspaceError("model result has no final output")
        initial = self._commit_files(prepared.workspace, prepared.authority.head_sha)
        if initial != dict(prepared.source_files):
            raise PRRepairWorkspaceError("prepared source identity changed before result capture")
        final = self._files(prepared.workspace)
        changed = self._changed(initial, final)
        return PRRepairResultManifest(
            1,
            record.job_id,
            record.execution_incarnation,
            prepared.authority.upstream_incarnation,
            record.kind.value,
            str(prepared.workspace),
            record.repository,
            record.target_number,
            prepared.authority.head_repository,
            prepared.authority.head_ref,
            prepared.authority.head_sha,
            prepared.authority.base_repository,
            prepared.authority.base_ref,
            prepared.authority.base_identity,
            prepared.remote,
            outcome.value,
            completion_source,
            observed_output,
            actual_head,
            merge_head,
            changed,
            tuple(sorted(final.items())),
        )

    def revalidate_result(self, record: LocalJobRecord, artifact: LocalJobResultArtifact, *, candidate_sha: str = "") -> VerifiedPRRepairResult:
        if artifact.job_id != record.job_id or artifact.execution_incarnation != record.execution_incarnation:
            raise PRRepairWorkspaceError("result artifact belongs to another execution")
        try:
            raw = json.loads(artifact.output)
            raw["changed_files"] = tuple((name, FileIdentity(**identity) if identity is not None else None) for name, identity in raw["changed_files"])
            raw["workspace_files"] = tuple((name, FileIdentity(**identity)) for name, identity in raw["workspace_files"])
            manifest = PRRepairResultManifest(**raw)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PRRepairWorkspaceError("result artifact is not a valid PR repair manifest") from exc
        authority = self._store.get_pr_repair_claim(record.job_id)
        if (
            manifest.schema_version != 1
            or authority is None
            or manifest.job_id != record.job_id
            or manifest.execution_incarnation != record.execution_incarnation
            or manifest.head_sha != authority.head_sha
            or manifest.base_sha != authority.base_identity
            or manifest.upstream_incarnation != authority.upstream_incarnation
            or manifest.kind != record.kind.value
            or manifest.repository != record.repository
            or manifest.pr_number != record.target_number
            or manifest.head_repository != authority.head_repository
            or manifest.head_ref != authority.head_ref
            or manifest.base_repository != authority.base_repository
            or manifest.base_ref != authority.base_ref
            or manifest.outcome != artifact.outcome.value
            or manifest.actual_head != authority.head_sha
            or manifest.completion_source not in {"model", "controller_verified_merge"}
            or (manifest.completion_source == "controller_verified_merge" and record.kind is not LocalJobKind.PR_MERGE_CONFLICT)
            or (manifest.completion_source == "model" and not manifest.observed_output)
            or Path(manifest.workspace).resolve() != Path(record.workspace_path).resolve()
            or manifest.remote != record.publication_remote
        ):
            raise PRRepairWorkspaceError("result manifest authority does not match the retained job")
        workspace = Path(manifest.workspace)
        actual_head = self._git(workspace, "rev-parse", "HEAD") if workspace.is_dir() else ""
        expected_parents = [manifest.head_sha]
        if record.kind is LocalJobKind.PR_MERGE_CONFLICT:
            expected_parents.append(manifest.base_sha)
        retained_candidate = bool(candidate_sha) and actual_head == candidate_sha and self._git(workspace, "show", "-s", "--format=%P", candidate_sha).split() == expected_parents
        if not workspace.is_dir() or (actual_head != manifest.actual_head and not retained_candidate):
            raise PRRepairWorkspaceError("retained result lineage is unavailable or changed")
        if not retained_candidate and self._merge_head(workspace) != manifest.merge_head:
            raise PRRepairWorkspaceError("retained merge lineage changed")
        if tuple(sorted(self._files(workspace).items())) != manifest.workspace_files:
            raise PRRepairWorkspaceError("retained result files were altered")
        if retained_candidate and self._commit_files(workspace, candidate_sha) != dict(manifest.workspace_files):
            raise PRRepairWorkspaceError("retained candidate tree does not match the result artifact")
        expected_changes = self._changed(self._commit_files(workspace, authority.head_sha), dict(manifest.workspace_files))
        if manifest.changed_files != expected_changes:
            raise PRRepairWorkspaceError("result manifest changed-file identities are invalid")
        expected_id = hashlib.sha256(json.dumps([artifact.job_id, artifact.execution_incarnation, artifact.outcome.value, artifact.output], separators=(",", ":")).encode()).hexdigest()
        if expected_id != artifact.artifact_id:
            raise PRRepairWorkspaceError("result artifact checksum is invalid")
        return VerifiedPRRepairResult(manifest, artifact.artifact_id)
