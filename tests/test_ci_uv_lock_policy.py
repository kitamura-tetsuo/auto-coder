"""Regression coverage for immutable, consistently versioned CI dependency setup."""

from __future__ import annotations

import os
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


def _run_text(step: object) -> str:
    if not isinstance(step, dict):
        return ""
    command = step.get("run", "")
    return command if isinstance(command, str) else ""


def _shell_commands(script: str) -> list[list[str]]:
    """Tokenize simple workflow run blocks while discarding shell comments."""
    commands: list[list[str]] = []
    for line in script.replace("\\\n", " ").splitlines():
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        command: list[str] = []
        for token in lexer:
            if token and all(character in ";&|" for character in token):
                if command:
                    commands.append(command)
                    command = []
            else:
                command.append(token)
        if command:
            commands.append(command)
    return commands


def _install_commands(steps: list[object]) -> list[list[str]]:
    commands = [command for step in steps for command in _shell_commands(_run_text(step))]
    return [command for command in commands if command[:2] == ["uv", "sync"] or command[:3] == ["uv", "pip", "install"] or command[:2] == ["pip", "install"]]


def _assert_workflow_policy(workflow_paths: list[Path]) -> set[str]:
    discovered: set[str] = set()
    for workflow_path in workflow_paths:
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        for job_name, job in workflow.get("jobs", {}).items():
            steps = job.get("steps", [])
            install_commands = _install_commands(steps)
            if not install_commands:
                continue

            identity = f"{workflow_path.name}:{job_name}"
            discovered.add(identity)
            setup_steps = [step for step in steps if step.get("uses") == SETUP_UV_ACTION]
            assert len(setup_steps) == 1, f"{identity} must use {SETUP_UV_ACTION} exactly once"
            assert setup_steps[0].get("with", {}).get("version") == UV_VERSION, f"{identity} must pin uv {UV_VERSION}"
            for command in install_commands:
                assert command[:2] == ["uv", "sync"], f"{identity} has an unlocked install: {shlex.join(command)}"
                assert "--locked" in command[2:], f"{identity} has an unlocked install: {shlex.join(command)}"

            job_commands = "\n".join(_run_text(step) for step in steps)
            assert "astral.sh/uv/install.sh" not in job_commands
    return discovered


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
    workflow_paths = sorted((*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")))
    discovered = _assert_workflow_policy(workflow_paths)
    assert discovered, "expected at least one active project-installing Python workflow"


@pytest.mark.parametrize(
    ("setup_version", "install_script", "valid"),
    [
        (UV_VERSION, "uv sync --locked", True),
        (UV_VERSION, "uv sync", False),
        (UV_VERSION, "uv sync # --locked", False),
        (UV_VERSION, "uv sync --locked\nuv pip install .", False),
        (None, "uv sync --locked", False),
        ("0.7.21", "uv sync --locked", False),
    ],
)
def test_future_workflow_policy_rejects_each_unlocked_or_unpinned_install(tmp_path: Path, setup_version: str | None, install_script: str, valid: bool) -> None:
    setup_with = "" if setup_version is None else f'\n        with:\n          version: "{setup_version}"'
    workflow_path = tmp_path / "future.yaml"
    workflow_path.write_text(
        f"""name: Future Python workflow
jobs:
  python:
    runs-on: ubuntu-latest
    steps:
      - uses: {SETUP_UV_ACTION}{setup_with}
      - run: |
          {install_script.replace(chr(10), chr(10) + "          ")}
""",
        encoding="utf-8",
    )

    if valid:
        assert _assert_workflow_policy([workflow_path]) == {"future.yaml:python"}
    else:
        with pytest.raises(AssertionError):
            _assert_workflow_policy([workflow_path])


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
