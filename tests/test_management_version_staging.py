from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from auto_coder.management.version_staging import ManagementPrepareIntent, ManagementRuntimeConfig, ManagementVersionStager
from auto_coder.tested_source_refs import SCHEMA_VERSION, TEST_PROFILE, WORKFLOW_PATH
from auto_coder.tested_source_refs import TestedSourceReadback as SourceReadback
from auto_coder.tested_source_refs import TestedSourceRecord as SourceRecord
from auto_coder.worker_versions import PreparationFailure, WorkerVersionError


def _proof(sha: str, lock: str) -> SourceReadback:
    record = SourceRecord(SCHEMA_VERSION, "owner/repo", sha, "1.2.3", lock, "3.12", 91, 2, 17, WORKFLOW_PATH, TEST_PROFILE, "success")
    return SourceReadback(f"refs/tags/auto-coder-tested-{sha}", "b" * 40, sha, record)


def _config(tmp_path: Path, **changes: object) -> ManagementRuntimeConfig:
    values: dict[str, object] = {
        "instance_id": "manager-a",
        "trusted_repository": "owner/repo",
        "source_repository": tmp_path / "source",
        "version_root": tmp_path / "management-versions",
        "python_executable": Path("/python"),
        "uv_executable": Path("/uv"),
        "uv_version": "0.7.22",
    }
    values.update(changes)
    return ManagementRuntimeConfig(**values)  # type: ignore[arg-type]


def test_prepare_rejects_test_evidence_for_another_commit_before_install(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.source_repository.mkdir()
    stager = ManagementVersionStager(config)
    called = False

    def forbidden(*_args: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("installer must not run")

    stager._preparer.prepare = forbidden  # type: ignore[method-assign]
    sha = "a" * 40
    with pytest.raises(WorkerVersionError) as caught:
        stager.prepare(ManagementPrepareIntent(sha, "attempt", _proof("c" * 40, "d" * 64)))
    assert caught.value.failure is PreparationFailure.VERIFICATION_MISMATCH
    assert not called


def test_management_root_must_not_overlap_active_or_rollback_runtime(tmp_path: Path) -> None:
    root = tmp_path / "versions"
    config = _config(tmp_path, version_root=root, active_environment=root / "active")
    stager = ManagementVersionStager(config)
    with pytest.raises(WorkerVersionError) as caught:
        stager._validate_boundaries()
    assert caught.value.failure is PreparationFailure.CONFLICT


def test_management_receipt_identity_detects_source_evidence_tampering(tmp_path: Path) -> None:
    config = _config(tmp_path)
    stager = ManagementVersionStager(config)
    from auto_coder.worker_versions import InstalledProvenance, ReadyReceipt

    worker = ReadyReceipt(1, "a" * 40, "a" * 40, "/v", "/v/bin/python", "1.2.3", "d" * 64, "attempt", config.runtime_profile, "e" * 32, "f" * 64, InstalledProvenance("a" * 40, "1.2.3"), "now")
    stager._preparer._run = lambda *_args, **_kwargs: "1" * 40  # type: ignore[method-assign]
    receipt = stager._management_receipt(worker, _proof("a" * 40, "d" * 64))
    assert len(receipt.receipt_identity) == 64
    payload = asdict(receipt)
    payload["test_run_id"] = 92
    tampered = type(receipt)(**payload)
    assert stager._receipt_identity(tampered) != tampered.receipt_identity


def test_lock_mismatch_is_rejected_before_environment_creation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.source_repository.mkdir()
    stager = ManagementVersionStager(config)
    stager._preparer._run_bytes = lambda _args: b"actual lock"  # type: ignore[method-assign]
    sha = "a" * 40
    proof = _proof(sha, hashlib.sha256(b"different lock").hexdigest())
    with pytest.raises(WorkerVersionError) as caught:
        stager.prepare(ManagementPrepareIntent(sha, "attempt", proof))
    assert caught.value.failure is PreparationFailure.VERIFICATION_MISMATCH
    assert not config.version_root.exists()
