from __future__ import annotations

import concurrent.futures
import errno
import hashlib
from pathlib import Path

import pytest

from auto_coder.management.version_staging import ManagementPrepareIntent, ManagementRuntimeConfig, ManagementVersionStager
from auto_coder.tested_source_refs import SCHEMA_VERSION, TEST_PROFILE, WORKFLOW_PATH
from auto_coder.tested_source_refs import TestedSourceReadback as SourceReadback
from auto_coder.tested_source_refs import TestedSourceRecord as SourceRecord
from auto_coder.worker_versions import PreparationFailure, WorkerVersionError


def _proof(sha: str, lock: str, version: str = "1.2.3") -> SourceReadback:
    record = SourceRecord(SCHEMA_VERSION, "owner/repo", sha, version, lock, "3.12", 91, 2, 17, WORKFLOW_PATH, TEST_PROFILE, "success")
    return SourceReadback(f"refs/tags/auto-coder-tested-{sha}", "b" * 40, sha, record)


class Authority:
    def __init__(self, proof: SourceReadback | None, *, fail_verify: bool = False):
        self.proof = proof
        self.fail_verify = fail_verify
        self.verified = False

    def read(self, _sha: str) -> SourceReadback | None:
        return self.proof

    def verify_run(self, _record: SourceRecord, *, recorded_attempt: bool = False) -> None:
        assert recorded_attempt
        self.verified = True
        if self.fail_verify:
            from auto_coder.tested_source_refs import TestedSourceError

            raise TestedSourceError("invented run")


class TransportFailureAuthority(Authority):
    def read(self, _sha: str) -> SourceReadback | None:
        raise RuntimeError("GitHub Git-data request unavailable")


def _config(tmp_path: Path, **changes: object) -> ManagementRuntimeConfig:
    config_file = tmp_path / "management.yaml"
    config_file.write_text("fixture")
    values: dict[str, object] = {
        "instance_id": "manager-a",
        "trusted_repository": "owner/repo",
        "source_repository": tmp_path / "source",
        "version_root": tmp_path / "management-versions",
        "python_executable": Path("/python"),
        "uv_executable": Path("/uv"),
        "uv_version": "0.7.22",
        "management_config": config_file,
    }
    values.update(changes)
    return ManagementRuntimeConfig(**values)  # type: ignore[arg-type]


def test_same_sha_fabricated_run_is_authenticated_before_install(tmp_path: Path) -> None:
    sha = "a" * 40
    lock = hashlib.sha256(b"lock").hexdigest()
    authority = Authority(_proof(sha, lock), fail_verify=True)
    config = _config(tmp_path, source_authority=authority)
    config.source_repository.mkdir()
    stager = ManagementVersionStager(config)
    stager._preparer._run_bytes = lambda _args: b"lock"  # type: ignore[method-assign]
    stager._preparer.prepare = lambda *_args: pytest.fail("installer must not run")  # type: ignore[method-assign]

    with pytest.raises(WorkerVersionError) as caught:
        stager.prepare(ManagementPrepareIntent(sha, "attempt"))

    assert caught.value.failure is PreparationFailure.UNAVAILABLE
    assert authority.verified


def test_source_authority_transport_failure_is_typed_before_install(tmp_path: Path) -> None:
    config = _config(tmp_path, source_authority=TransportFailureAuthority(None))
    config.source_repository.mkdir()
    stager = ManagementVersionStager(config)
    stager._preparer.prepare = lambda *_args: pytest.fail("installer must not run")  # type: ignore[method-assign]

    with pytest.raises(WorkerVersionError) as caught:
        stager.prepare(ManagementPrepareIntent("a" * 40, "attempt"))

    assert caught.value.failure is PreparationFailure.UNAVAILABLE


@pytest.mark.parametrize("field", ["active_environment", "rollback_environment"])
def test_management_root_must_not_overlap_protected_runtime(tmp_path: Path, field: str) -> None:
    root = tmp_path / "versions"
    stager = ManagementVersionStager(_config(tmp_path, version_root=root, **{field: root / "runtime"}))
    with pytest.raises(WorkerVersionError) as caught:
        stager._validate_boundaries()
    assert caught.value.failure is PreparationFailure.CONFLICT


def test_relative_management_root_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(WorkerVersionError) as caught:
        ManagementVersionStager(_config(tmp_path, version_root=Path("relative")))._validate_boundaries()
    assert caught.value.failure is PreparationFailure.UNSUPPORTED


def test_existing_worker_root_is_not_adopted(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.version_root.mkdir()
    (config.version_root / "revisions").mkdir()
    stager = ManagementVersionStager(config)
    stager._validate_boundaries()
    with pytest.raises(WorkerVersionError) as caught:
        stager._reserve_root()
    assert caught.value.failure is PreparationFailure.CONFLICT


def test_concurrent_root_reservations_publish_one_complete_marker(tmp_path: Path) -> None:
    config = _config(tmp_path)

    def reserve(_: int) -> None:
        stager = ManagementVersionStager(config)
        stager._validate_boundaries()
        stager._reserve_root()

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(reserve, range(32)))

    assert (config.version_root / ".management-root.json").read_text() == '{"instance_id":"manager-a","schema_version":1}\n'


def test_adapter_publication_space_error_is_typed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    error = OSError(errno.ENOSPC, "no space")
    stager = ManagementVersionStager(_config(tmp_path))
    monkeypatch.setattr(stager, "_validate_boundaries", lambda: (_ for _ in ()).throw(error))
    with pytest.raises(WorkerVersionError) as caught:
        stager.prepare(ManagementPrepareIntent("a" * 40, "attempt"))
    assert caught.value.failure is PreparationFailure.INSUFFICIENT_SPACE
