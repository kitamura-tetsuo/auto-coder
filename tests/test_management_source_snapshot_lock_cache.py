"""Small unit checks for isolated source fixture lock reuse, not host acceptance."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests import test_management_self_update_integration as helper


def _project(root: Path, dependencies: str) -> None:
    root.mkdir()
    (root / "pyproject.toml").write_text(dependencies, encoding="utf-8")


def test_identical_resolver_inputs_reuse_verified_lock_without_new_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(helper, "_SNAPSHOT_LOCK_CACHE", {})
    calls: list[Path] = []

    def resolve(arguments: list[str], cwd: Path, *, env: dict[str, str]) -> str:
        assert arguments == ["uv", "lock", "--python", helper.sys.executable]
        assert env == {"UV_INDEX_URL": "https://fixture.invalid/simple"}
        calls.append(cwd)
        (cwd / "uv.lock").write_text("verified:" + (cwd / "pyproject.toml").read_text(), encoding="utf-8")
        return ""

    monkeypatch.setattr(helper, "_run", resolve)
    first, second, changed = (tmp_path / name for name in ("first", "second", "changed"))
    _project(first, "original-dependencies")
    _project(second, "original-dependencies")
    _project(changed, "changed-dependencies")
    for root in (first, second, changed):
        helper._ensure_snapshot_lock(root, "uv", {"UV_INDEX_URL": "https://fixture.invalid/simple"})

    assert calls == [first, changed]
    assert (first / "uv.lock").read_bytes() == (second / "uv.lock").read_bytes()
    assert (changed / "uv.lock").read_bytes() != (second / "uv.lock").read_bytes()


def test_failed_resolution_never_populates_or_reuses_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(helper, "_SNAPSHOT_LOCK_CACHE", {})
    calls = 0

    def resolve(arguments: list[str], cwd: Path, *, env: dict[str, str]) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            (cwd / "uv.lock").write_text("partial", encoding="utf-8")
            raise subprocess.TimeoutExpired(arguments, 90)
        (cwd / "uv.lock").write_text("verified", encoding="utf-8")
        return ""

    monkeypatch.setattr(helper, "_run", resolve)
    first, second = tmp_path / "first", tmp_path / "second"
    _project(first, "same-dependencies")
    _project(second, "same-dependencies")
    with pytest.raises(subprocess.TimeoutExpired):
        helper._ensure_snapshot_lock(first, "uv", {})
    helper._ensure_snapshot_lock(second, "uv", {})
    assert calls == 2
    assert (second / "uv.lock").read_text() == "verified"
