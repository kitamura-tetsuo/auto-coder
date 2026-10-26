"""Prepare management-service runtimes without touching the serving runtime."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from auto_coder.tested_source_refs import TestedSourceReadback, validate_record
from auto_coder.worker_versions import (
    PreparationFailure,
    PreparedEnvironment,
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


@dataclass(frozen=True)
class ManagementPrepareIntent:
    source_sha: str
    attempt_id: str
    tested_source: TestedSourceReadback


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
        """Validate source evidence, install, probe, and publish management READY."""

        self._validate_boundaries()
        record = validate_record(intent.tested_source.record)
        if intent.tested_source.direct_commit_sha != intent.source_sha or record.source_sha != intent.source_sha or record.repository != self.config.trusted_repository:
            raise WorkerVersionError(PreparationFailure.VERIFICATION_MISMATCH, "tested-source evidence does not identify the requested source")
        lock = self._preparer._run_bytes(["git", "-C", str(self.config.source_repository.resolve()), "show", f"{intent.source_sha}:uv.lock"])
        if hashlib.sha256(lock).hexdigest() != record.lock_sha256:
            raise WorkerVersionError(PreparationFailure.VERIFICATION_MISMATCH, "tested-source evidence does not identify the requested lock")

        prepared = self._preparer.prepare(PrepareIntent(intent.source_sha, intent.attempt_id))
        receipt = self._management_receipt(prepared.receipt, intent.tested_source)
        path = Path(prepared.receipt.environment_path).parent / "MANAGEMENT_READY.json"
        if path.exists():
            existing = self.lookup(path)
            if existing != receipt:
                raise WorkerVersionError(PreparationFailure.CONFLICT, "management READY evidence conflicts with the installed runtime")
        else:
            WorkerVersionPreparer._publish(path, receipt)  # type: ignore[arg-type]
        return PreparedManagementRuntime(self.lookup(path), prepared.reused)

    def lookup(self, receipt_path: Path) -> ManagementReadyReceipt:
        """Return only a verified, controller-confined runtime and executable."""

        try:
            path = receipt_path.resolve(strict=True)
            path.relative_to(self.config.version_root.resolve())
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or set(raw) != set(ManagementReadyReceipt.__dataclass_fields__):
                raise ValueError("invalid schema")
            receipt = ManagementReadyReceipt(**raw)
            worker = self._preparer.read_ready(path.parent / "READY.json")
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise WorkerVersionError(PreparationFailure.CORRUPT_READY, "management READY receipt is malformed") from exc
        expected = self._management_receipt_from_values(worker, receipt)
        actual_tree = self._preparer._run(["git", "-C", str(self.config.source_repository.resolve()), "rev-parse", f"{receipt.source_sha}^{{tree}}"], output=True).strip()
        if receipt != expected or receipt.source_tree != actual_tree:
            raise WorkerVersionError(PreparationFailure.VERIFICATION_MISMATCH, "management READY receipt no longer matches installed content")
        self._preparer._verify_entrypoint(Path(receipt.environment_path))
        return receipt

    def _management_receipt(self, worker: ReadyReceipt, proof: TestedSourceReadback) -> ManagementReadyReceipt:
        record = proof.record
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
            "pending",
        )
        return replace(receipt, receipt_identity=self._receipt_identity(receipt))

    def _management_receipt_from_values(self, worker: ReadyReceipt, receipt: ManagementReadyReceipt) -> ManagementReadyReceipt:
        """Rebuild immutable bindings while retaining source-proof values for comparison."""

        expected = ManagementReadyReceipt(
            MANAGEMENT_READY_SCHEMA,
            worker.installed_runtime_id,
            self.config.instance_id,
            worker.source_sha,
            receipt.source_tree,
            receipt.tested_source_ref,
            receipt.tested_source_object,
            receipt.test_run_id,
            receipt.test_run_attempt,
            worker.lock_sha256,
            worker.runtime_profile,
            "3.12",
            f"uv {self.config.uv_version}",
            worker.environment_path,
            worker.interpreter_path,
            worker.distribution_version,
            "auto-coder management",
            worker.content_identity,
            "pending",
        )
        return replace(expected, receipt_identity=self._receipt_identity(expected))

    @staticmethod
    def _receipt_identity(receipt: ManagementReadyReceipt) -> str:
        values = asdict(receipt)
        values.pop("receipt_identity")
        return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _validate_boundaries(self) -> None:
        root = self.config.version_root.expanduser().resolve()
        source = self.config.source_repository.expanduser().resolve()
        if not self.config.instance_id or not root.is_absolute():
            raise WorkerVersionError(PreparationFailure.UNSUPPORTED, "management instance and absolute version root are required")
        protected = [path.expanduser().resolve() for path in (self.config.active_environment, self.config.rollback_environment) if path is not None]
        if root == source or root in source.parents or source in root.parents or any(root == path or root in path.parents or path in root.parents for path in protected):
            raise WorkerVersionError(PreparationFailure.CONFLICT, "management staging overlaps a protected runtime or repository")
