from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from auto_coder.worker_versions import PreparationFailure, PrepareIntent, WorkerVersionConfig, WorkerVersionError, WorkerVersionPreparer

REPO_ROOT = Path(__file__).resolve().parents[1]


def _command(arguments: list[str], cwd: Path | None = None) -> str:
    return subprocess.run(arguments, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _command(["git", "add", "."], repo)
    _command(["git", "commit", "-m", message], repo)
    return _command(["git", "rev-parse", "HEAD"], repo)


def _create_production_fixture(tmp_path: Path) -> tuple[Path, str, Path, str]:
    """Create a disposable Git source using the production backend/provenance code."""

    uv = Path(shutil.which("uv") or pytest.fail("uv is required for the worker-version integration boundary"))
    repo = tmp_path / "source-repository"
    (repo / "build_backend").mkdir(parents=True)
    (repo / "src" / "auto_coder").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "build_backend" / "auto_coder_build_backend.py", repo / "build_backend")
    for name in ("build_identity.py", "build_provenance.py"):
        shutil.copy2(REPO_ROOT / "src" / "auto_coder" / name, repo / "src" / "auto_coder")
    (repo / "src" / "auto_coder" / "__init__.py").write_text("")
    (repo / "README.md").write_text("fixture\n")
    (repo / "pyproject.toml").write_text(
        """[build-system]
requires = ["setuptools>=84.2.1", "wheel"]
build-backend = "auto_coder_build_backend"
backend-path = ["build_backend"]

[project]
name = "auto-coder"
dynamic = ["version"]
requires-python = ">=3.12"
dependencies = []

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-dir]
"" = "src"

[tool.setuptools.package-data]
auto_coder = ["*.json"]

[tool.setuptools.dynamic]
version = {attr = "auto_coder_build_backend.get_build_version"}
"""
    )
    _command(["git", "init", "-q"], repo)
    _command(["git", "config", "user.email", "worker-version-test@example.invalid"], repo)
    _command(["git", "config", "user.name", "Worker Version Test"], repo)
    _command([str(uv), "lock", "--python", sys.executable], repo)
    sha = _commit(repo, "fixture revision")
    uv_version = _command([str(uv), "--version"]).removeprefix("uv ")
    return repo, sha, uv, uv_version


def _prepare_in_process(repo: str, root: str, uv: str, uv_version: str, sha: str, attempt: str) -> tuple[str, bool, str]:
    config = WorkerVersionConfig(Path(repo), Path(root), Path(sys.executable), Path(uv), uv_version)
    prepared = WorkerVersionPreparer(config).prepare(PrepareIntent(sha, attempt))
    return prepared.receipt.installed_runtime_id, prepared.reused, prepared.receipt.interpreter_path


def test_real_uv_install_provenance_reconstruction_wrong_source_and_running_sentinel(tmp_path: Path, _use_real_commands: None) -> None:
    repo, sha, uv, uv_version = _create_production_fixture(tmp_path)
    root = tmp_path / "versions"
    sentinel = tmp_path / "previous-worker"
    _command([str(uv), "venv", "--python", sys.executable, "--no-project", str(sentinel)])
    sentinel_process = subprocess.Popen(
        [str(sentinel / "bin" / "python"), "-c", "import os,time; print(os.getpid(), flush=True); time.sleep(120)"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert sentinel_process.stdout is not None
    assert int(sentinel_process.stdout.readline()) == sentinel_process.pid
    sentinel_python = (sentinel / "bin" / "python").lstat()
    config = WorkerVersionConfig(repo, root, Path(sys.executable), uv, uv_version)
    try:
        prepared = WorkerVersionPreparer(config).prepare(PrepareIntent(sha, "real-install"))
        receipt = prepared.receipt
        interpreter = Path(receipt.interpreter_path)
        assert interpreter == Path(receipt.environment_path) / "bin" / "python"
        assert _command([str(interpreter), "-c", "import sys; print(sys.prefix)"]) == receipt.environment_path
        observation = json.loads(_command([str(interpreter), "-m", "auto_coder.build_provenance"]))
        assert observation["source_revision"]["value"] == sha
        assert observation["distribution_version"]["value"] == receipt.distribution_version
        assert sentinel_process.poll() is None
        assert (sentinel / "bin" / "python").lstat() == sentinel_python
        assert _command([str(interpreter), "-c", "from pathlib import Path; print(Path('/var/run/docker.sock').exists())"]) == "False"

        reconstructed = WorkerVersionPreparer(config).read_ready(root / "revisions" / next((root / "revisions").iterdir()).name / "real-install" / "READY.json")
        assert reconstructed.installed_runtime_id == receipt.installed_runtime_id
        assert reconstructed.content_identity == receipt.content_identity

        wrong_sha = "f" * 40 if sha != "f" * 40 else "e" * 40
        subprocess.run([str(interpreter), "-m", "auto_coder.build_provenance", "embed"], check=True, env={**os.environ, "AUTO_CODER_SOURCE_REVISION": wrong_sha})
        with pytest.raises(WorkerVersionError) as mismatch:
            WorkerVersionPreparer(config).read_ready(next(root.glob("revisions/*/real-install/READY.json")))
        assert mismatch.value.failure is PreparationFailure.VERIFICATION_MISMATCH
        assert sentinel_process.poll() is None
    finally:
        sentinel_process.terminate()
        sentinel_process.wait(timeout=10)


def test_independent_concurrent_preparers_reuse_one_real_install(tmp_path: Path, _use_real_commands: None) -> None:
    repo, sha, uv, uv_version = _create_production_fixture(tmp_path)
    root = tmp_path / "concurrent-versions"
    with concurrent.futures.ProcessPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_prepare_in_process, str(repo), str(root), str(uv), uv_version, sha, f"attempt-{index}") for index in range(2)]
        results = [future.result(timeout=120) for future in futures]

    assert len({runtime_id for runtime_id, _, _ in results}) == 1
    assert sorted(reused for _, reused, _ in results) == [False, True]
    assert len(list(root.glob("revisions/*/*/READY.json"))) == 1


def test_partial_attempt_frozen_lock_failure_and_cross_runtime_receipt_are_rejected(tmp_path: Path, _use_real_commands: None) -> None:
    repo, sha, uv, uv_version = _create_production_fixture(tmp_path)
    config = WorkerVersionConfig(repo, tmp_path / "versions-a", Path(sys.executable), uv, uv_version)
    lock_bytes = subprocess.run(["git", "show", f"{sha}:uv.lock"], cwd=repo, check=True, capture_output=True).stdout
    lock_hash = hashlib.sha256(lock_bytes).hexdigest()
    key = hashlib.sha256(f"{sha}\0{lock_hash}\0python-3.12".encode()).hexdigest()
    partial = config.version_root / "revisions" / key / "crashed-attempt"
    partial.mkdir(parents=True)
    (partial / "partial-install").write_text("not ready")

    prepared = WorkerVersionPreparer(config).prepare(PrepareIntent(sha, "retry-attempt"))
    assert not (partial / "READY.json").exists()
    assert (partial / "partial-install").read_text() == "not ready"

    other_config = replace(config, version_root=tmp_path / "versions-b")
    other = WorkerVersionPreparer(other_config).prepare(PrepareIntent(sha, "other-runtime"))
    ready = next(config.version_root.glob("revisions/*/retry-attempt/READY.json"))
    target_payload = json.loads(ready.read_text())
    # Model a receipt from a different real runtime while retaining paths that
    # pass structural confinement. Readback must still execute this runtime and
    # reject the substituted installed version rather than trusting JSON.
    target_payload["distribution_version"] = "9999.0"
    target_payload["installed_provenance"]["distribution_version"] = "9999.0"
    bindings = {
        "attempt_id": target_payload["attempt_id"],
        "runtime_profile": target_payload["runtime_profile"],
        "environment_path": target_payload["environment_path"],
        "interpreter_path": target_payload["interpreter_path"],
        "source_sha": target_payload["source_sha"],
        "distribution_version": target_payload["distribution_version"],
        "lock_sha256": target_payload["lock_sha256"],
        "installed_runtime_id": other.receipt.installed_runtime_id,
    }
    target_payload["installed_runtime_id"] = other.receipt.installed_runtime_id
    target_payload["content_identity"] = hashlib.sha256(json.dumps(bindings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    ready.write_text(json.dumps(target_payload))
    with pytest.raises(WorkerVersionError) as substituted:
        WorkerVersionPreparer(config).read_ready(ready)
    assert substituted.value.failure is PreparationFailure.VERIFICATION_MISMATCH

    (repo / "uv.lock").write_text("not a uv lock\n")
    bad_sha = _commit(repo, "broken frozen lock")
    with pytest.raises(WorkerVersionError) as frozen:
        WorkerVersionPreparer(replace(config, version_root=tmp_path / "failed-versions")).prepare(PrepareIntent(bad_sha, "failed-lock"))
    assert frozen.value.failure in {PreparationFailure.INCOMPATIBLE_DEPENDENCIES, PreparationFailure.UNAVAILABLE}
    assert not list((tmp_path / "failed-versions").glob("revisions/*/*/READY.json"))
    assert prepared.receipt.installed_runtime_id
