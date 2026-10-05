"""Regression coverage for the controller artifact observation (Issue #2432)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import pytest

from auto_coder import build_provenance as bp

pytestmark = pytest.mark.usefixtures("_use_real_commands")

REPO_ROOT = Path(__file__).parents[1]
SHA_A = "a" * 39 + "1"
SHA_ENV = "e" * 40


@dataclass
class Installed:
    site: Path
    package: Path


def _dockerfile_embed_command() -> str:
    """The production Dockerfile's embed RUN instruction, verbatim."""
    lines = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
    match = [m.group(1) for line in lines if (m := re.match(r"RUN (python -m auto_coder\.build_provenance embed)$", line))]
    assert len(match) == 1
    return match[0]


def _uv() -> str:
    uv = shutil.which("uv")
    assert uv, "uv is the project's dependency tool and is required"
    return uv


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the project wheel through its normal build configuration (as the Dockerfile does)."""
    out = tmp_path_factory.mktemp("wheel")
    src = tmp_path_factory.mktemp("src")
    for name in ("pyproject.toml", "README.md", "LICENSE", "MANIFEST.in"):
        shutil.copy(REPO_ROOT / name, src / name)
    shutil.copytree(REPO_ROOT / "src", src / "src", ignore=shutil.ignore_patterns("__pycache__", "build_provenance.json"))
    result = subprocess.run([_uv(), "build", "--wheel", "--out-dir", str(out), str(src)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return next(out.glob("auto_coder-*.whl"))


@pytest.fixture
def installed(tmp_path: Path, built_wheel: Path) -> Installed:
    """Install the built wheel into an isolated site directory, outside the source checkout."""
    site = tmp_path / "site"
    result = subprocess.run([_uv(), "pip", "install", "--python", sys.executable, "--no-deps", "--target", str(site), str(built_wheel)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return Installed(site=site, package=site / "auto_coder")


def _run(args: list[str], installed: Installed, cwd: Path, env_extra: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(installed.site), "HOME": str(cwd)}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, *args], cwd=cwd, env=env, capture_output=True, text=True, check=False)


def _embed(installed: Installed, cwd: Path, revision: Optional[str]) -> None:
    cmd = _dockerfile_embed_command().split()[1:]
    extra = {} if revision is None else {bp.SOURCE_REVISION_ENV: revision}
    result = _run(cmd, installed, cwd, extra)
    assert result.returncode == 0, result.stderr


def _observe(installed: Installed, cwd: Path, env_extra: Optional[Dict[str, str]] = None) -> dict:
    result = _run(["-m", "auto_coder.build_provenance"], installed, cwd, env_extra)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _target_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target"
    repo.mkdir()
    for cmd in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "b"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True, capture_output=True)
    return repo


def _head(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def test_embedded_revision_survives_target_checkout_and_launch_environment(tmp_path: Path, installed: Installed) -> None:
    """AS-001/AS-002: embedded revision A wins over target commit B, env and head changes."""
    repo = _target_repo(tmp_path)
    _embed(installed, tmp_path, SHA_A)

    first = _observe(installed, repo)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "c"], cwd=repo, check=True)
    second = _observe(installed, repo, {bp.SOURCE_REVISION_ENV: SHA_ENV, "GITHUB_SHA": SHA_ENV})

    assert _head(repo) != SHA_A
    for obs in (first, second):
        assert obs["schema_version"] == 1
        assert obs["source_revision"] == {"value": SHA_A, "origin": "build_embedded", "available": True, "reason": None}
        assert obs["distribution_version"]["origin"] == "installed_distribution_metadata"
        assert obs["distribution_version"]["value"] and obs["distribution_version"]["value"] != SHA_A
        assert obs["process_run_id"] == obs["process_run"]["value"]
        assert str(installed.site) not in json.dumps(obs)
    # A new process has its own process-run identity; earlier observation is unchanged.
    assert first["process_run_id"] != second["process_run_id"]


@pytest.mark.parametrize("unset_or_unknown", [None, "unknown", "abc123", ""])
def test_embed_of_invalid_build_input_is_null_not_guessed(tmp_path: Path, installed: Installed, unset_or_unknown: Optional[str]) -> None:
    _embed(installed, tmp_path, unset_or_unknown)
    obs = _observe(installed, _target_repo(tmp_path))
    assert obs["source_revision"]["value"] is None
    assert obs["source_revision"]["available"] is False
    assert obs["source_revision"]["reason"] == bp.REASON_REVISION_INVALID


@pytest.mark.parametrize(
    "content,reason",
    [
        (None, bp.REASON_MISSING),
        ("{not json", bp.REASON_MALFORMED),
        ("[]", bp.REASON_MALFORMED),
        (json.dumps({"schema_version": 99, "source_revision": SHA_A}), bp.REASON_UNSUPPORTED),
        (json.dumps({"schema_version": 1}), bp.REASON_REVISION_ABSENT),
        (json.dumps({"schema_version": 1, "source_revision": "main"}), bp.REASON_REVISION_INVALID),
    ],
)
def test_unavailable_metadata_is_unknown_and_target_sha_never_fills_gap(tmp_path: Path, installed: Installed, content: Optional[str], reason: str) -> None:
    repo = _target_repo(tmp_path)
    if content is not None:
        (installed.package / bp.RECORD_FILENAME).write_text(content, encoding="utf-8")
    obs = _observe(installed, repo, {bp.SOURCE_REVISION_ENV: _head(repo)})
    assert obs["source_revision"] == {"value": None, "origin": "none", "available": False, "reason": reason}
    assert obs["process_run_id"]


def test_oversized_integer_record_keeps_known_fields(tmp_path: Path, installed: Installed) -> None:
    (installed.package / bp.RECORD_FILENAME).write_text('{"schema_version": 1, "source_revision": ' + "9" * 5000 + "}", encoding="utf-8")
    obs = _observe(installed, tmp_path)
    assert obs["source_revision"]["reason"] == bp.REASON_MALFORMED
    assert obs["process_run_id"]
    assert obs["distribution_version"]["available"] is True


def test_unreadable_record_is_unavailable(tmp_path: Path, installed: Installed) -> None:
    (installed.package / bp.RECORD_FILENAME).write_bytes(b"\xff\xfe\x00")
    obs = _observe(installed, tmp_path)
    assert obs["source_revision"]["value"] is None
    assert obs["source_revision"]["reason"] == bp.REASON_MALFORMED


def test_one_field_failure_keeps_other_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(bp, "_read_source_revision", boom)
    obs = bp.observe_controller_artifact()
    assert obs.process_run_id and obs.source_revision.value is None


def test_read_failures_never_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(bp.importlib.metadata, "version", boom)
    monkeypatch.setattr(bp, "_read_source_revision", boom)
    obs = bp.observe_controller_artifact()
    assert obs.source_revision.value is None and obs.schema_version == 1


def test_observation_uses_existing_process_run_identity() -> None:
    from auto_coder.execution_trace import get_trace_collector

    assert bp.observe_controller_artifact().process_run_id == get_trace_collector().process_run_id


def test_read_does_not_touch_network_or_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def forbidden(*_a: object, **_k: object) -> None:
        raise AssertionError("diagnostic read must not do I/O side effects")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    bp.observe_controller_artifact()


def test_dockerfile_embeds_after_install_with_existing_build_arg_and_label() -> None:
    text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG AUTO_CODER_SOURCE_REVISION=unknown" in text
    assert "LABEL org.opencontainers.image.revision=$AUTO_CODER_SOURCE_REVISION" in text
    assert text.index("pip install --no-cache-dir /wheels/*.whl") < text.index(_dockerfile_embed_command())
    assert text.index("ARG AUTO_CODER_SOURCE_REVISION") < text.index(_dockerfile_embed_command())
