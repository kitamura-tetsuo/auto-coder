"""Regression coverage for immutable, consistently versioned CI dependency setup."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
UV_VERSION = "0.7.22"
SETUP_UV_ACTION = "astral-sh/setup-uv@v7"
PROJECT_INSTALL = re.compile(r"\buv sync\b|\buv pip install\b|\bpip install\b[^\n]*(?:-e\s+\.|\s\.)")


def _run_text(step: object) -> str:
    if not isinstance(step, dict):
        return ""
    command = step.get("run", "")
    return command if isinstance(command, str) else ""


def _sync_commands(workflow_path: Path) -> list[str]:
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    commands: list[str] = []
    for job in workflow.get("jobs", {}).values():
        for step in job.get("steps", []):
            for line in _run_text(step).splitlines():
                command = line.strip()
                if command.startswith("uv sync"):
                    commands.append(command)
    return commands


def test_every_active_project_installing_workflow_uses_one_pinned_locked_uv_setup() -> None:
    """Discover future workflows instead of maintaining an allowlist."""
    discovered: set[str] = set()

    workflow_paths = sorted((*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")))
    for workflow_path in workflow_paths:
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        for job_name, job in workflow.get("jobs", {}).items():
            steps = job.get("steps", [])
            install_steps = [step for step in steps if PROJECT_INSTALL.search(_run_text(step))]
            if not install_steps:
                continue

            discovered.add(f"{workflow_path.name}:{job_name}")
            setup_steps = [step for step in steps if step.get("uses") == SETUP_UV_ACTION]
            assert len(setup_steps) == 1, f"{workflow_path}:{job_name} must use {SETUP_UV_ACTION} exactly once"
            assert setup_steps[0].get("with", {}).get("version") == UV_VERSION
            sync_steps = [step for step in install_steps if "uv sync" in _run_text(step)]
            assert sync_steps, f"{workflow_path}:{job_name} must install dependencies with uv sync"
            assert all("--locked" in _run_text(step) for step in sync_steps)

            job_commands = "\n".join(_run_text(step) for step in steps)
            assert "astral.sh/uv/install.sh" not in job_commands
            assert "uv pip install -e ." not in job_commands

    assert discovered, "expected at least one active project-installing Python workflow"


def _write_lock_fixture(project: Path) -> None:
    dependency = project / "dependency"
    (dependency / "local_dependency").mkdir(parents=True)
    (dependency / "local_dependency" / "__init__.py").write_text("", encoding="utf-8")
    (dependency / "pyproject.toml").write_text(
        """[project]
name = "local-dependency"
version = "1.0.0"

[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"
""",
        encoding="utf-8",
    )
    (project / "pyproject.toml").write_text(
        """[project]
name = "lock-admission-fixture"
version = "1.0.0"
dependencies = ["local-dependency"]

[tool.uv.sources]
local-dependency = { path = "dependency" }

[project.optional-dependencies]
test = []

[dependency-groups]
dev = []
""",
        encoding="utf-8",
    )
    subprocess.run(["uv", "lock"], cwd=project, check=True, capture_output=True, text=True)


def _assert_setup_fails_without_lock_change(project: Path, command: str) -> None:
    lock = project / "uv.lock"
    before = lock.read_bytes()
    downstream = project / "downstream-ran"
    environment = {**os.environ, "UV_CACHE_DIR": str(project / ".uv-cache")}
    result = subprocess.run(
        ["bash", "-c", f"{command} && touch {shlex.quote(str(downstream))}"],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert not downstream.exists()
    assert lock.read_bytes() == before


@pytest.mark.parametrize(
    "workflow_name",
    ["test-source-for-runtime.yml", "publish-beta.yml"],
)
def test_production_setup_rejects_stale_lock_before_tests_or_publication(tmp_path: Path, workflow_name: str, _use_real_commands: object) -> None:
    command = _sync_commands(WORKFLOWS / workflow_name)[0]
    _write_lock_fixture(tmp_path)
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(
        pyproject_path.read_text(encoding="utf-8").replace(
            'name = "lock-admission-fixture"\nversion = "1.0.0"',
            'name = "lock-admission-fixture"\nversion = "2.0.0"',
        ),
        encoding="utf-8",
    )

    _assert_setup_fails_without_lock_change(tmp_path, command)


@pytest.mark.parametrize(
    "workflow_name",
    ["test-source-for-runtime.yml", "publish-beta.yml"],
)
def test_production_setup_rejects_unavailable_locked_package_before_tests_or_publication(tmp_path: Path, workflow_name: str, _use_real_commands: object) -> None:
    command = _sync_commands(WORKFLOWS / workflow_name)[0]
    _write_lock_fixture(tmp_path)
    shutil.rmtree(tmp_path / "dependency")

    _assert_setup_fails_without_lock_change(tmp_path, command)
