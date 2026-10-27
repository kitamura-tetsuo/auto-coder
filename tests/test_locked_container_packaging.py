"""Production-container lock installation regressions for Issue #2633."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from auto_coder.build_identity import BuildIdentity, deserialize_build_identity, serialize_build_identity

REPO_ROOT = Path(__file__).parents[1]


def _prepared_image() -> str:
    image = os.environ.get("AUTOCODER_OPENCODE_IMAGE") or os.environ.get("AUTO_CODER_OPENCODE_IMAGE")
    assert image, "the live workflow must provide the image built from this checkout"
    return image


def _build_identity_arg() -> str:
    revision = "a" * 40
    payload = serialize_build_identity(
        BuildIdentity(
            state="clean",
            version=f"1970.1.1.1+g{revision}",
            source_revision=revision,
            source_date_epoch=1,
            revision_count=1,
            reason=None,
        )
    )
    return base64.b64encode(payload.encode()).decode()


def test_dockerfile_uses_the_committed_lock_for_a_production_install() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY pyproject.toml uv.lock README.md LICENSE MANIFEST.in ./" in dockerfile
    assert "uv sync --locked --no-dev --no-editable --no-cache" in dockerfile
    assert "pip wheel" not in dockerfile
    assert "pip install" not in dockerfile
    assert "COPY --from=python-environment /opt/auto-coder/.venv /opt/auto-coder/.venv" in dockerfile
    assert 'ENV PATH="/opt/auto-coder/.venv/bin:/home/node/.opencode/bin:${PATH}"' in dockerfile


def test_stale_lock_build_argument_is_a_valid_build_identity() -> None:
    record = deserialize_build_identity(base64.b64decode(_build_identity_arg()).decode())

    assert record.state == "clean"
    assert record.source_revision == "a" * 40


@pytest.mark.opencode_live
def test_built_image_runs_locked_distributions_and_provenance() -> None:
    image = _prepared_image()
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = {package["name"]: package["version"] for package in lock["package"] if "version" in package}
    script = """
import importlib.metadata
import json
import auto_coder
from auto_coder.build_provenance import observe_controller_artifact
names = ('requests', 'pydantic', 'click')
print(json.dumps({name: importlib.metadata.version(name) for name in names}))
observation = observe_controller_artifact()
assert observation.distribution_version.value == importlib.metadata.version('auto-coder')
"""
    result = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "python", image, "-c", script],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    installed = json.loads(result.stdout.splitlines()[0])
    assert installed == {name: locked[name] for name in installed}


@pytest.mark.opencode_live
def test_actual_dependency_stage_rejects_a_stale_lock_without_rewriting_it(tmp_path: Path) -> None:
    context = tmp_path / "context"
    shutil.copytree(
        REPO_ROOT,
        context,
        ignore=shutil.ignore_patterns(".git", ".venv", ".agent-tmp", "__pycache__", ".pytest_cache"),
    )
    pyproject = context / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace("dependencies = [", 'dependencies = [\n    "boltons==25.0.0",', 1),
        encoding="utf-8",
    )
    original_lock = (context / "uv.lock").read_bytes()
    identity = _build_identity_arg()

    result = subprocess.run(
        [
            "docker",
            "build",
            "--target",
            "python-environment",
            "--build-arg",
            f"AUTO_CODER_BUILD_IDENTITY_B64={identity}",
            str(context),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "lockfile" in output.lower()
    assert "needs to be updated" in output.lower()
    assert "--locked was provided" in output.lower()
    assert (context / "uv.lock").read_bytes() == original_lock
