"""Prepare management-service runtimes without touching the serving runtime."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from auto_coder.tested_source_refs import TestedSourceError, TestedSourcePublisher, TestedSourceReadback
from auto_coder.worker_versions import (
    PreparationFailure,
    PrepareIntent,
    ReadyReceipt,
    WorkerVersionConfig,
    WorkerVersionError,
    WorkerVersionPreparer,
)

MANAGEMENT_READY_SCHEMA = 1


@dataclass(frozen=True)
class ManagementRuntimeConfig:
    """Controller-owned boundaries for one management instance."""

    instance_id: str
    trusted_repository: str
    source_repository: Path
    version_root: Path
    python_executable: Path
    uv_executable: Path
    uv_version: str
    runtime_profile: str = "management-python-3.12"
    active_environment: Path | None = None
    rollback_environment: Path | None = None
    protected_roots: tuple[Path, ...] = ()
    management_config: Path | None = None
    source_authority: TestedSourcePublisher | None = None


@dataclass(frozen=True)
class ManagementPrepareIntent:
    source_sha: str
    attempt_id: str


@dataclass(frozen=True)
class ManagementReadyReceipt:
    schema_version: int
    management_runtime_id: str
    instance_id: str
    source_sha: str
    source_tree: str
    tested_source_ref: str
    tested_source_object: str
    test_run_id: int
    test_run_attempt: int
    lock_sha256: str
    runtime_profile: str
    python_version: str
    installer_identity: str
    environment_path: str
    interpreter_path: str
    distribution_version: str
    entrypoint: str
    worker_receipt_identity: str
    installed_content_sha256: str
    configuration_sha256: str
    receipt_identity: str


@dataclass(frozen=True)
class PreparedManagementRuntime:
    receipt: ManagementReadyReceipt
    reused: bool


class _ManagementEnvironmentPreparer(WorkerVersionPreparer):
    """Use the generic installer with a management-specific runtime probe."""

    def _verify_entrypoint(self, environment: Path) -> None:
        try:
            self._run([str(environment / "bin" / "auto-coder"), "management", "--help"], env=self._safe_environment())
        except WorkerVersionError as exc:
            raise WorkerVersionError(PreparationFailure.MISSING_CAPABILITY, "installed management entry point is unavailable or incompatible") from exc


class ManagementVersionStager:
    """Management-only adapter around the immutable environment producer."""

    def __init__(self, config: ManagementRuntimeConfig):
        self.config = config
        worker_config = WorkerVersionConfig(
            config.source_repository,
            config.version_root,
            config.python_executable,
            config.uv_executable,
            config.uv_version,
            config.runtime_profile,
        )
        self._preparer = _ManagementEnvironmentPreparer(worker_config)

    def prepare(self, intent: ManagementPrepareIntent) -> PreparedManagementRuntime:
        """Authenticate source evidence, install, probe, and publish READY."""

        try:
            self._validate_boundaries()
            self._reserve_root()
            proof = self._authenticated_proof(intent.source_sha)
            prepared = self._preparer.prepare(PrepareIntent(intent.source_sha, intent.attempt_id))
            receipt = self._management_receipt(prepared.receipt, proof)
            path = Path(prepared.receipt.environment_path).parent / "MANAGEMENT_READY.json"
            if path.exists():
                existing = self.lookup(path)
                if existing != receipt:
                    raise WorkerVersionError(PreparationFailure.CONFLICT, "management READY evidence conflicts with the installed runtime")
            else:
                WorkerVersionPreparer._publish(path, receipt)  # type: ignore[arg-type]
            return PreparedManagementRuntime(self.lookup(path), prepared.reused)
        except WorkerVersionError:
            raise
        except TestedSourceError as exc:
            raise WorkerVersionError(PreparationFailure.UNAVAILABLE, "authoritative tested-source evidence is unavailable") from exc
        except OSError as exc:
            raise WorkerVersionPreparer._filesystem_error(exc) from exc

    def lookup(self, receipt_path: Path) -> ManagementReadyReceipt:
        """Return only a currently authenticated, immutable configured runtime."""

        try:
            self._validate_boundaries()
            self._verify_root_reservation()
            path = receipt_path.resolve(strict=True)
            path.relative_to(self._root)
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or set(raw) != set(ManagementReadyReceipt.__dataclass_fields__):
                raise ValueError("invalid schema")
            receipt = ManagementReadyReceipt(**raw)
            worker = self._preparer.read_ready(path.parent / "READY.json")
            proof = self._authenticated_proof(receipt.source_sha)
            expected = self._management_receipt(worker, proof)
            if receipt != expected or receipt.runtime_profile != self.config.runtime_profile:
                raise WorkerVersionError(PreparationFailure.VERIFICATION_MISMATCH, "management READY receipt no longer matches installed content")
            return receipt
        except WorkerVersionError:
            raise
        except TestedSourceError as exc:
            raise WorkerVersionError(PreparationFailure.UNAVAILABLE, "authoritative tested-source evidence is unavailable") from exc
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise WorkerVersionError(PreparationFailure.CORRUPT_READY, "management READY receipt is malformed") from exc

    def _authenticated_proof(self, source_sha: str) -> TestedSourceReadback:
        authority = self.config.source_authority
        if authority is None:
            raise WorkerVersionError(PreparationFailure.UNAVAILABLE, "tested-source authority is not configured")
        proof = authority.read(source_sha)
        if proof is None:
            raise WorkerVersionError(PreparationFailure.UNAVAILABLE, "source has no immutable tested-source evidence")
        authority.verify_run(proof.record, recorded_attempt=True)
        record = proof.record
        lock = self._preparer._run_bytes(["git", "-C", str(self.config.source_repository.resolve()), "show", f"{source_sha}:uv.lock"])
        if proof.direct_commit_sha != source_sha or record.repository != self.config.trusted_repository or hashlib.sha256(lock).hexdigest() != record.lock_sha256:
            raise WorkerVersionError(PreparationFailure.VERIFICATION_MISMATCH, "authoritative source evidence does not match the requested source and lock")
        return proof

    def _management_receipt(self, worker: ReadyReceipt, proof: TestedSourceReadback) -> ManagementReadyReceipt:
        record = proof.record
        if worker.distribution_version != record.distribution_version:
            raise WorkerVersionError(PreparationFailure.VERIFICATION_MISMATCH, "installed distribution does not match authoritative source evidence")
        tree = self._preparer._run(["git", "-C", str(self.config.source_repository.resolve()), "rev-parse", f"{worker.source_sha}^{{tree}}"], output=True).strip()
        receipt = ManagementReadyReceipt(
            MANAGEMENT_READY_SCHEMA,
            worker.installed_runtime_id,
            self.config.instance_id,
            worker.source_sha,
            tree,
            proof.tag_ref,
            proof.tag_object_oid,
            record.run_id,
            record.run_attempt,
            worker.lock_sha256,
            worker.runtime_profile,
            record.python_version,
            f"uv {self.config.uv_version}",
            worker.environment_path,
            worker.interpreter_path,
            worker.distribution_version,
            "auto-coder management",
            worker.content_identity,
            self._content_identity(Path(worker.environment_path)),
            self._configuration_identity(),
            "pending",
        )
        return replace(receipt, receipt_identity=self._receipt_identity(receipt))

    @staticmethod
    def _receipt_identity(receipt: ManagementReadyReceipt) -> str:
        values = asdict(receipt)
        values.pop("receipt_identity")
        return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _validate_boundaries(self) -> None:
        supplied = self.config.version_root.expanduser()
        if not supplied.is_absolute():
            raise WorkerVersionError(PreparationFailure.UNSUPPORTED, "management version root must be absolute")
        self._root = supplied.resolve()
        source = self.config.source_repository.expanduser().resolve()
        if not self.config.instance_id or self.config.runtime_profile != "management-python-3.12":
            raise WorkerVersionError(PreparationFailure.UNSUPPORTED, "management instance or runtime profile is unsupported")
        protected_values = (self.config.active_environment, self.config.rollback_environment, *self.config.protected_roots)
        protected = [path.expanduser().resolve() for path in protected_values if path is not None]
        if self._root == source or self._root in source.parents or source in self._root.parents or any(self._root == path or self._root in path.parents or path in self._root.parents for path in protected):
            raise WorkerVersionError(PreparationFailure.CONFLICT, "management staging overlaps a protected runtime, store, or repository")
        if self.config.management_config is None or not self.config.management_config.expanduser().is_absolute():
            raise WorkerVersionError(PreparationFailure.UNSUPPORTED, "an absolute management configuration is required")

    def _reserve_root(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker = self._root / ".management-root.json"
        expected = {"schema_version": 1, "instance_id": self.config.instance_id}
        if marker.exists():
            self._verify_root_reservation()
            return
        foreign = [entry for entry in self._root.iterdir() if entry.name != ".management-root.json"]
        if foreign:
            raise WorkerVersionError(PreparationFailure.CONFLICT, "version root contains unreserved foreign state")
        try:
            with marker.open("x", encoding="utf-8") as stream:
                json.dump(expected, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            self._verify_root_reservation()

    def _verify_root_reservation(self) -> None:
        try:
            value = json.loads((self._root / ".management-root.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkerVersionError(PreparationFailure.CONFLICT, "management root reservation is absent or corrupt") from exc
        if value != {"schema_version": 1, "instance_id": self.config.instance_id}:
            raise WorkerVersionError(PreparationFailure.CONFLICT, "management root belongs to another instance")

    def _configuration_identity(self) -> str:
        assert self.config.management_config is not None
        try:
            return hashlib.sha256(self.config.management_config.read_bytes()).hexdigest()
        except OSError as exc:
            raise WorkerVersionPreparer._filesystem_error(exc) from exc

    def _verify_compatibility(self, environment: Path) -> None:
        assert self.config.management_config is not None
        script = (
            "from pathlib import Path; from auto_coder.management.app import create_management_app; "
            "from auto_coder.management.oauth_config import ManagementSettings, load_management_settings; "
            "s=load_management_settings(Path(__import__('sys').argv[1])); "
            "assert isinstance(s, ManagementSettings) and callable(create_management_app)"
        )
        env = self._preparer._safe_environment()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            self._preparer._run([str(environment / "bin" / "python"), "-c", script, str(self.config.management_config)], env=env)
            self._preparer._run([str(environment / "bin" / "auto-coder"), "management", "serve", "--help"], env=env)
        except WorkerVersionError as exc:
            raise WorkerVersionError(PreparationFailure.MISSING_CAPABILITY, "installed management serving configuration is incompatible") from exc

    def _content_identity(self, environment: Path) -> str:
        self._verify_compatibility(environment)
        digest = hashlib.sha256()
        for path in sorted(environment.rglob("*"), key=lambda item: item.relative_to(environment).as_posix()):
            relative = path.relative_to(environment).as_posix().encode()
            if path.is_symlink():
                digest.update(b"L\0" + relative + b"\0" + os.readlink(path).encode() + b"\0")
            elif path.is_file():
                digest.update(b"F\0" + relative + b"\0")
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                digest.update(b"\0")
        return digest.hexdigest()
