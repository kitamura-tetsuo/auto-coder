"""Bounded ordinary genesis predecessor and stock Caddy capture profiles."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from auto_coder.management import fresh_host_observation as observation
from auto_coder.management.exclusion_host import inherited_support
from auto_coder.management.original_genesis import HISTORY_MOUNT
from tests.test_management_original_genesis import captured, root_authorization  # noqa: F401


def test_original_genesis_predecessor_retains_its_exact_readonly_history(captured) -> None:  # noqa: F811
    anchor, _ = captured
    mounts = [{"Type": "bind", "Source": item.host_path, "Destination": item.container_path, "RW": item.role in {"state", "journal", "oauth"}} for item in anchor.roots]
    assert inherited_support(mounts) == ((), {str(HISTORY_MOUNT)})
    mounts[-1]["RW"] = True
    with pytest.raises(ValueError):
        inherited_support(mounts)


def test_original_genesis_predecessor_cannot_replace_only_history_mount(captured, tmp_path: Path) -> None:  # noqa: F811
    anchor, _ = captured
    alternate = tmp_path / "alternate"
    alternate.mkdir(mode=0o700)
    mounts = [{"Type": "bind", "Source": item.host_path if item.role != "history" else str(alternate), "Destination": item.container_path, "RW": item.role in {"state", "journal", "oauth"}} for item in anchor.roots]
    with pytest.raises(ValueError, match="mounted root differs"):
        inherited_support(mounts)


@pytest.fixture
def caddy_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = tmp_path / "Caddyfile"
    config.write_bytes(b"http://127.0.0.1:8080 {}")
    paths = [config]
    for name in ("data", "config"):
        path = tmp_path / name
        path.mkdir(mode=0o700)
        paths.append(path)
    mounts = [{"Type": "bind", "Source": str(path), "Destination": destination, "RW": index > 0} for index, (path, destination) in enumerate(zip(paths, ("/etc/caddy/Caddyfile", "/data", "/config")))]
    identifier, image = "a" * 64, "sha256:" + "b" * 64
    values = [identifier, image, True, os.getpid(), "running", False, "", None, None, "container:" + "c" * 64, None, mounts, "unused", "started", 0, ["caddy"], ["run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]]
    marker = object()
    monkeypatch.setattr(observation, "_engine_id", lambda: "component-engine")
    monkeypatch.setattr(observation, "_inspect", lambda _: values)
    monkeypatch.setattr(observation, "_capture_prior_kernel", lambda *_args, **_kwargs: marker)
    return identifier, image, values, marker


def test_stock_caddy_explicit_private_data_binds_are_captured(caddy_profile) -> None:
    identifier, image, values, marker = caddy_profile
    expected = tuple((item["Source"], item["Destination"], item["RW"]) for item in values[11])
    assert observation.capture_prior_container(identifier, image, expected_mounts=expected) is marker


@pytest.mark.parametrize("mutation", ["partial", "writable-policy", "arbitrary-command", "unprotected-root"])
def test_caddy_data_profile_cannot_admit_other_mounts(caddy_profile, mutation: str) -> None:
    identifier, image, values, _ = caddy_profile
    if mutation == "partial":
        values[11].pop()
    elif mutation == "writable-policy":
        values[11][-1]["RW"] = False
    elif mutation == "arbitrary-command":
        values[15] = ["python"]
    else:
        Path(values[11][-1]["Source"]).chmod(0o755)
    expected = tuple((item["Source"], item["Destination"], item["RW"]) for item in values[11])
    with pytest.raises(observation.HostObservationUnavailable):
        observation.capture_prior_container(identifier, image, expected_mounts=expected)
