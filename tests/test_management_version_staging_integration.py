from __future__ import annotations

import concurrent.futures
import errno
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from auto_coder.management.version_staging import ManagementPrepareIntent, ManagementRuntimeConfig, ManagementVersionStager
from auto_coder.tested_source_refs import SCHEMA_VERSION, TEST_PROFILE, WORKFLOW_PATH, TestedSourceReadback, TestedSourceRecord
from auto_coder.worker_versions import PreparationFailure, WorkerVersionError, WorkerVersionPreparer


def _run(args: list[str], cwd: Path | None = None) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Authority:
    def __init__(self, proof: TestedSourceReadback):
        self.proof = proof
        self.verifications = 0

    def read(self, sha: str) -> TestedSourceReadback | None:
        return self.proof if self.proof.direct_commit_sha == sha else None

    def verify_run(self, record: TestedSourceRecord, *, recorded_attempt: bool = False) -> None:
        assert recorded_attempt and record == self.proof.record
        self.verifications += 1


def _fixture(tmp_path: Path, *, serve: bool = True) -> tuple[ManagementRuntimeConfig, str, Authority]:
    uv = Path(shutil.which("uv") or pytest.fail("uv required"))
    repo = tmp_path / "repo"
    package = repo / "src" / "auto_coder"
    management = package / "management"
    management.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "lazy_feature.py").write_text("VALUE = 7\n")
    (management / "__init__.py").write_text("")
    (package / "build_provenance.py").write_text(
        "import json,os,sys\nfrom pathlib import Path\np=Path(__file__).with_name('_identity.json')\n"
        "if len(sys.argv)>1 and sys.argv[1]=='embed': p.write_text(json.dumps({'source_revision':os.environ['AUTO_CODER_SOURCE_REVISION'],'distribution_version':'1.2.3'}))\n"
        "else:\n d=json.loads(p.read_text()); print(json.dumps({'source_revision':{'available':True,'value':d['source_revision']},'distribution_version':{'available':True,'value':d['distribution_version']}}))\n"
    )
    serve_case = "if a==['management','serve','--help']: return" if serve else ""
    (package / "cli.py").write_text("import sys\ndef main():\n a=sys.argv[1:]\n if a==['management','--help']: return\n " + serve_case + "\n raise SystemExit(2)\n")
    (management / "oauth_config.py").write_text("from dataclasses import dataclass\nfrom pathlib import Path\n@dataclass(frozen=True)\nclass ManagementSettings:\n value:str\ndef load_management_settings(path:Path):\n return ManagementSettings(path.read_text())\n")
    (management / "app.py").write_text("def create_management_app(settings): return settings\n")
    (repo / "pyproject.toml").write_text(
        """[build-system]
requires=['setuptools>=84.2.1']
build-backend='setuptools.build_meta'
[project]
name='auto-coder'
version='1.2.3'
requires-python='>=3.12'
dependencies=[]
[project.scripts]
auto-coder='auto_coder.cli:main'
[tool.setuptools.packages.find]
where=['src']
[tool.setuptools.package-dir]
''='src'
"""
    )
    _run(["git", "init", "-q"], repo)
    _run(["git", "config", "user.email", "test@example.invalid"], repo)
    _run(["git", "config", "user.name", "Test"], repo)
    _run([str(uv), "lock", "--python", sys.executable], repo)
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-qm", "fixture"], repo)
    sha = _run(["git", "rev-parse", "HEAD"], repo)
    lock = hashlib.sha256((repo / "uv.lock").read_bytes()).hexdigest()
    record = TestedSourceRecord(SCHEMA_VERSION, "owner/repo", sha, "1.2.3", lock, "3.12", 11, 1, 12, WORKFLOW_PATH, TEST_PROFILE, "success")
    authority = Authority(TestedSourceReadback(f"refs/tags/auto-coder-tested-{sha}", "b" * 40, sha, record))
    config_file = tmp_path / "management.yaml"
    config_file.write_text("compatible")
    config = ManagementRuntimeConfig("manager-a", "owner/repo", repo, tmp_path / "management-versions", Path(sys.executable), uv, _run([str(uv), "--version"]).removeprefix("uv "), management_config=config_file, source_authority=authority)  # type: ignore[arg-type]
    return config, sha, authority


def test_real_prepare_lookup_reuse_and_installed_content_tampering(tmp_path: Path, _use_real_commands: None) -> None:
    config, sha, authority = _fixture(tmp_path)
    active = tmp_path / "active"
    active.mkdir()
    marker = active / "credentials.sqlite3"
    marker.write_bytes(b"unchanged")
    config = ManagementRuntimeConfig(**{**config.__dict__, "active_environment": active})
    stager = ManagementVersionStager(config)
    prepared = stager.prepare(ManagementPrepareIntent(sha, "first"))
    ready = Path(prepared.receipt.environment_path).parent / "MANAGEMENT_READY.json"
    assert stager.lookup(ready) == prepared.receipt
    assert stager.prepare(ManagementPrepareIntent(sha, "lost-ack-retry")).reused
    assert marker.read_bytes() == b"unchanged"
    assert authority.verifications >= 4

    site = _run([prepared.receipt.interpreter_path, "-c", "import auto_coder.cli; print(auto_coder.cli.__file__)"]).strip()
    Path(site).write_text(Path(site).read_text() + "\n# tampered\n")
    with pytest.raises(WorkerVersionError) as caught:
        stager.lookup(ready)
    assert caught.value.failure is PreparationFailure.VERIFICATION_MISMATCH


def test_lookup_hashes_before_candidate_execution_and_ignores_runtime_bytecode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands: None) -> None:
    config, sha, _authority = _fixture(tmp_path)
    stager = ManagementVersionStager(config)
    prepared = stager.prepare(ManagementPrepareIntent(sha, "first"))
    ready = Path(prepared.receipt.environment_path).parent / "MANAGEMENT_READY.json"
    interpreter = prepared.receipt.interpreter_path

    _run([interpreter, "-c", "import auto_coder.lazy_feature"])
    assert stager.lookup(ready) == prepared.receipt

    sentinel = tmp_path / "candidate-executed"
    provenance = Path(_run([interpreter, "-I", "-c", "import auto_coder.build_provenance as p; print(p.__file__)"]).splitlines()[-1])
    provenance.write_text(provenance.read_text() + f"\nopen({str(sentinel)!r}, 'w').write('executed')\n")
    with pytest.raises(WorkerVersionError) as caught:
        stager.lookup(ready)
    assert caught.value.failure is PreparationFailure.VERIFICATION_MISMATCH
    assert not sentinel.exists()


def test_lookup_isolates_candidate_imports_from_controller_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands: None) -> None:
    config, sha, _authority = _fixture(tmp_path)
    prepared = ManagementVersionStager(config).prepare(ManagementPrepareIntent(sha, "first"))
    ready = Path(prepared.receipt.environment_path).parent / "MANAGEMENT_READY.json"
    foreign = tmp_path / "foreign-cwd" / "auto_coder"
    foreign.mkdir(parents=True)
    sentinel = tmp_path / "foreign-imported"
    (foreign / "__init__.py").write_text(f"open({str(sentinel)!r}, 'w').write('imported')\n")
    monkeypatch.chdir(foreign.parent)

    assert ManagementVersionStager(config).lookup(ready) == prepared.receipt
    assert not sentinel.exists()


def test_real_prepare_rejects_missing_serving_capability(tmp_path: Path, _use_real_commands: None) -> None:
    config, sha, _authority = _fixture(tmp_path, serve=False)
    with pytest.raises(WorkerVersionError) as caught:
        ManagementVersionStager(config).prepare(ManagementPrepareIntent(sha, "missing-serve"))
    assert caught.value.failure is PreparationFailure.MISSING_CAPABILITY
    assert not list(config.version_root.glob("revisions/*/*/MANAGEMENT_READY.json"))


def test_real_lookup_rejects_changed_profile_and_configuration(tmp_path: Path, _use_real_commands: None) -> None:
    config, sha, _authority = _fixture(tmp_path)
    prepared = ManagementVersionStager(config).prepare(ManagementPrepareIntent(sha, "first"))
    ready = Path(prepared.receipt.environment_path).parent / "MANAGEMENT_READY.json"
    incompatible = ManagementRuntimeConfig(**{**config.__dict__, "runtime_profile": "unsupported-other-abi"})
    with pytest.raises(WorkerVersionError) as profile:
        ManagementVersionStager(incompatible).lookup(ready)
    assert profile.value.failure is PreparationFailure.UNSUPPORTED
    assert config.management_config is not None
    config.management_config.write_text("changed")
    with pytest.raises(WorkerVersionError) as changed:
        ManagementVersionStager(config).lookup(ready)
    assert changed.value.failure is PreparationFailure.VERIFICATION_MISMATCH


def test_concurrent_same_source_requests_share_real_management_runtime(tmp_path: Path, _use_real_commands: None) -> None:
    config, sha, _authority = _fixture(tmp_path)

    def prepare(attempt: str) -> str:
        return ManagementVersionStager(config).prepare(ManagementPrepareIntent(sha, attempt)).receipt.management_runtime_id

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        identities = list(executor.map(prepare, ("concurrent-a", "concurrent-b")))

    assert len(set(identities)) == 1
    assert len(list(config.version_root.glob("revisions/*/*/MANAGEMENT_READY.json"))) == 1


def test_lost_management_publication_is_typed_and_restart_recovers_real_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands: None) -> None:
    config, sha, _authority = _fixture(tmp_path)
    original = WorkerVersionPreparer._publish
    management_write = False

    def fail_management(path: Path, receipt: object) -> None:
        nonlocal management_write
        if path.name == "MANAGEMENT_READY.json":
            management_write = True
            raise OSError(errno.ENOSPC, "simulated full management volume")
        original(path, receipt)  # type: ignore[arg-type]

    monkeypatch.setattr(WorkerVersionPreparer, "_publish", staticmethod(fail_management))
    with pytest.raises(WorkerVersionError) as caught:
        ManagementVersionStager(config).prepare(ManagementPrepareIntent(sha, "first"))
    assert caught.value.failure is PreparationFailure.INSUFFICIENT_SPACE
    assert management_write
    assert list(config.version_root.glob("revisions/*/*/READY.json"))
    assert not list(config.version_root.glob("revisions/*/*/MANAGEMENT_READY.json"))

    monkeypatch.setattr(WorkerVersionPreparer, "_publish", staticmethod(original))
    recovered = ManagementVersionStager(config).prepare(ManagementPrepareIntent(sha, "restart"))
    assert recovered.reused
    assert Path(recovered.receipt.environment_path).parent.joinpath("MANAGEMENT_READY.json").is_file()
