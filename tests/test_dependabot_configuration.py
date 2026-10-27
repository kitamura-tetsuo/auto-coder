"""Regression coverage for the repository's Dependabot manifest inventory."""

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEPENDABOT_CONFIG = REPOSITORY_ROOT / ".github" / "dependabot.yml"


def _nested_python_projects(repository_root: Path) -> set[str]:
    """Inventory nested Python projects independently of Dependabot's entries."""
    return {f"/{manifest.parent.relative_to(repository_root).as_posix()}" for manifest in repository_root.rglob("pyproject.toml") if manifest.parent != repository_root and ".git" not in manifest.parts and ".venv" not in manifest.parts}


def _load_updates() -> list[dict[str, Any]]:
    document = yaml.safe_load(DEPENDABOT_CONFIG.read_text(encoding="utf-8"))
    assert document["version"] == 2
    return document["updates"]


def _assert_manifest_coverage(updates: list[dict[str, Any]], repository_root: Path) -> None:
    required = {
        ("uv", "/", "pyproject.toml"),
        ("docker", "/", "Dockerfile"),
        *(("uv", directory, "pyproject.toml") for directory in _nested_python_projects(repository_root)),
    }
    declared: list[tuple[str, str]] = []

    for update in updates:
        ecosystem = update["package-ecosystem"]
        directory = update["directory"]
        assert directory.startswith("/")
        target_directory = repository_root / directory.removeprefix("/")
        assert target_directory.is_dir(), f"Dependabot directory does not exist: {directory}"
        declared.append((ecosystem, directory))

    assert len(declared) == len(set(declared)), "duplicate Dependabot entries conflict"
    for ecosystem, directory, manifest_name in required:
        assert (ecosystem, directory) in declared, f"{ecosystem} does not monitor required project {directory}"
        manifest = repository_root / directory.removeprefix("/") / manifest_name
        assert manifest.is_file(), f"{ecosystem} mapping does not monitor {manifest}"


def test_dependabot_covers_repository_dependency_manifests() -> None:
    updates = _load_updates()

    _assert_manifest_coverage(updates, REPOSITORY_ROOT)

    schedules = {(entry["package-ecosystem"], entry["directory"]): entry["schedule"]["interval"] for entry in updates}
    assert schedules[("uv", "/")] == "weekly"
    assert schedules[("github-actions", "/")] == "weekly"


def test_missing_nested_uv_project_is_rejected() -> None:
    updates = deepcopy(_load_updates())
    missing_directory = sorted(_nested_python_projects(REPOSITORY_ROOT))[0]
    incomplete = [entry for entry in updates if not (entry["package-ecosystem"] == "uv" and entry["directory"] == missing_directory)]
    assert any(entry["package-ecosystem"] == "uv" for entry in incomplete)

    with pytest.raises(AssertionError, match="does not monitor required project"):
        _assert_manifest_coverage(incomplete, REPOSITORY_ROOT)
