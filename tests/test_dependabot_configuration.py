"""Regression coverage for scheduled Dependabot update groups and manifests."""

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEPENDABOT_CONFIG = REPOSITORY_ROOT / ".github" / "dependabot.yml"
EXPECTED_GROUP_NAMES = {
    "uv": "python-dependencies",
    "docker": "docker-dependencies",
    "github-actions": "github-actions-dependencies",
}


def _nested_python_projects(repository_root: Path) -> set[str]:
    """Inventory nested Python projects independently of Dependabot's entries."""
    return {
        f"/{manifest.parent.relative_to(repository_root).as_posix()}"
        for manifest in repository_root.rglob("pyproject.toml")
        if manifest.parent != repository_root
        and ".git" not in manifest.parts
        and ".venv" not in manifest.parts
    }


def _load_document() -> dict[str, Any]:
    document = yaml.safe_load(DEPENDABOT_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    assert document["version"] == 2
    return document


def _load_updates() -> list[dict[str, Any]]:
    return _load_document()["updates"]


def _directories(update: dict[str, Any]) -> list[str]:
    assert ("directory" in update) != ("directories" in update), (
        "Dependabot entry must specify exactly one of directory and directories"
    )
    directories = (
        update["directories"] if "directories" in update else [update["directory"]]
    )
    assert isinstance(directories, list) and directories, (
        "Dependabot directories must be a nonempty list"
    )
    assert all(
        isinstance(directory, str)
        and directory.startswith("/")
        and (directory == "/" or not directory.endswith("/"))
        for directory in directories
    ), "Dependabot directories must be absolute, canonical paths"
    assert len(directories) == len(set(directories)), (
        "duplicate Dependabot directories in an entry"
    )
    return directories


def _assert_manifest_coverage(
    updates: list[dict[str, Any]], repository_root: Path
) -> None:
    required = {
        ("uv", "/"),
        ("docker", "/"),
        ("github-actions", "/"),
        *(
            ("uv", directory)
            for directory in _nested_python_projects(repository_root)
        ),
    }
    declared: list[tuple[str, str]] = []

    for update in updates:
        ecosystem = update["package-ecosystem"]
        for directory in _directories(update):
            target_directory = repository_root / directory.removeprefix("/")
            assert target_directory.is_dir(), (
                f"Dependabot directory does not exist: {directory}"
            )
            declared.append((ecosystem, directory))

    assert len(declared) == len(set(declared)), "duplicate Dependabot entries conflict"
    missing = required - set(declared)
    assert not missing, (
        f"Dependabot does not monitor required project {sorted(missing)}"
    )
    unexpected = set(declared) - required
    assert not unexpected, (
        f"Unexpected Dependabot monitoring entries: {sorted(unexpected)}"
    )

    for ecosystem, directory in required:
        target_directory = repository_root / directory.removeprefix("/")
        if ecosystem == "github-actions":
            assert (target_directory / ".github" / "workflows").is_dir()
        else:
            manifest_name = (
                "Dockerfile" if ecosystem == "docker" else "pyproject.toml"
            )
            assert (target_directory / manifest_name).is_file(), (
                f"{ecosystem} mapping does not monitor {directory}/{manifest_name}"
            )


def _assert_update_contract(updates: list[dict[str, Any]]) -> None:
    assert len(updates) == 3, "Expected exactly three Dependabot update entries"
    assert {entry["package-ecosystem"] for entry in updates} == set(
        EXPECTED_GROUP_NAMES
    )

    for update in updates:
        ecosystem = update["package-ecosystem"]
        assert update["schedule"]["interval"] == "weekly"
        assert set(update["schedule"]) == {"interval"}
        expected_group = EXPECTED_GROUP_NAMES[ecosystem]
        assert update.get("groups") == {
            expected_group: {
                "applies-to": "version-updates",
                "patterns": ["*"],
            }
        }, f"{ecosystem} must have one catch-all version-update group"
        if ecosystem == "uv":
            assert "directories" in update and "directory" not in update
            assert update.get("ignore") == [
                {"dependency-name": "fastcore", "versions": [">=2.0.0"]}
            ], "Preserve the fastcore >=2.0.0 compatibility exclusion"
        else:
            assert _directories(update) == ["/"]
            assert "ignore" not in update


def test_dependabot_covers_repository_dependency_manifests() -> None:
    document = _load_document()
    assert "multi-ecosystem-groups" not in document
    assert set(document) == {"version", "updates"}
    updates = document["updates"]

    _assert_manifest_coverage(updates, REPOSITORY_ROOT)
    _assert_update_contract(updates)


def test_missing_nested_uv_project_is_rejected() -> None:
    updates = deepcopy(_load_updates())
    missing_directory = sorted(_nested_python_projects(REPOSITORY_ROOT))[0]
    uv_entry = next(
        entry for entry in updates if entry["package-ecosystem"] == "uv"
    )
    uv_entry["directories"].remove(missing_directory)

    with pytest.raises(AssertionError, match="does not monitor required project"):
        _assert_manifest_coverage(updates, REPOSITORY_ROOT)


def test_new_nested_uv_project_is_rejected(tmp_path: Path) -> None:
    updates = deepcopy(_load_updates())
    for update in updates:
        for directory in _directories(update):
            target = tmp_path / directory.removeprefix("/")
            target.mkdir(parents=True, exist_ok=True)
            if update["package-ecosystem"] == "uv":
                (target / "pyproject.toml").write_text("", encoding="utf-8")
            elif update["package-ecosystem"] == "docker":
                (target / "Dockerfile").write_text("", encoding="utf-8")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    unmonitored = tmp_path / "unmonitored"
    unmonitored.mkdir()
    (unmonitored / "pyproject.toml").write_text("", encoding="utf-8")

    with pytest.raises(AssertionError, match="does not monitor required project"):
        _assert_manifest_coverage(updates, tmp_path)


@pytest.mark.parametrize("ecosystem", ["uv", "docker", "github-actions"])
@pytest.mark.parametrize("change", ["missing", "narrow", "security"])
def test_invalid_grouping_is_rejected(ecosystem: str, change: str) -> None:
    updates = deepcopy(_load_updates())
    entry = next(
        update for update in updates if update["package-ecosystem"] == ecosystem
    )
    name = EXPECTED_GROUP_NAMES[ecosystem]
    if change == "missing":
        entry.pop("groups")
    elif change == "narrow":
        entry["groups"][name]["patterns"] = ["pytest"]
    else:
        entry["groups"][name]["applies-to"] = "security-updates"

    with pytest.raises(AssertionError, match="catch-all version-update group"):
        _assert_update_contract(updates)


@pytest.mark.parametrize("change", ["both", "duplicate", "empty", "scalar"])
def test_invalid_directory_shapes_are_rejected(change: str) -> None:
    updates = deepcopy(_load_updates())
    uv_entry = next(
        entry for entry in updates if entry["package-ecosystem"] == "uv"
    )
    if change == "both":
        uv_entry["directory"] = "/"
    elif change == "duplicate":
        uv_entry["directories"].append("/")
    elif change == "empty":
        uv_entry["directories"] = []
    else:
        uv_entry["directories"] = "/"

    with pytest.raises(AssertionError, match="directory|directories|duplicate"):
        _assert_manifest_coverage(updates, REPOSITORY_ROOT)


def test_duplicate_directory_across_entries_is_rejected() -> None:
    updates = deepcopy(_load_updates())
    updates.append(deepcopy(updates[0]))
    with pytest.raises(AssertionError, match="duplicate Dependabot entries"):
        _assert_manifest_coverage(updates, REPOSITORY_ROOT)


def test_missing_fastcore_exclusion_is_rejected() -> None:
    updates = deepcopy(_load_updates())
    uv_entry = next(
        entry for entry in updates if entry["package-ecosystem"] == "uv"
    )
    uv_entry.pop("ignore")
    with pytest.raises(AssertionError, match="fastcore"):
        _assert_update_contract(updates)


def test_nonweekly_schedule_is_rejected() -> None:
    updates = deepcopy(_load_updates())
    updates[0]["schedule"]["interval"] = "monthly"
    with pytest.raises(AssertionError):
        _assert_update_contract(updates)
