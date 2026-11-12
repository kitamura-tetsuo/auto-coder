"""Only effective mount collection order is presentation, never its contents."""

from __future__ import annotations

import copy

import pytest

from auto_coder.management.docker_inspection import canonical_inspection, canonical_legacy_inspection, canonical_mounts
from auto_coder.management.installed_bootstrap_mount_profile import _mounts


def _inspection() -> dict[str, object]:
    return {
        "Config": {"Cmd": ["run", "--config", "/etc/caddy/Caddyfile"], "Env": ["A=1", "B=2"]},
        "State": {"Pid": 123, "StartedAt": "original"},
        "Mounts": [
            {"Type": "bind", "Source": "/private/second", "Destination": "/second", "RW": True, "Propagation": "rprivate", "FutureField": {"flags": ["first", "second"]}},
            {"Type": "bind", "Source": "/private/first", "Destination": "/first", "RW": False, "Propagation": "rprivate"},
        ],
    }


def test_full_inspection_normalizes_only_mount_collection_order_without_mutation() -> None:
    first = _inspection()
    original = copy.deepcopy(first)
    reordered = copy.deepcopy(first)
    reordered["Mounts"].reverse()
    assert canonical_inspection(first) == canonical_inspection(reordered)
    assert first == original
    assert canonical_inspection(first)["Config"] == first["Config"]
    assert canonical_inspection(first)["State"] == first["State"]


@pytest.mark.parametrize("field,value", [("RW", False), ("Propagation", "rshared"), ("Source", "/other/source"), ("Destination", "/other"), ("FutureField", {"flags": ["second", "first"]})])
def test_normalization_preserves_all_mount_fields(field: str, value: object) -> None:
    first = _inspection()
    changed = copy.deepcopy(first)
    changed["Mounts"][0][field] = value
    assert canonical_inspection(first) != canonical_inspection(changed)


def test_duplicate_mount_records_are_preserved_and_selected_profile_refuses_them() -> None:
    first = _inspection()
    duplicate = copy.deepcopy(first)
    duplicate["Mounts"].append(copy.deepcopy(duplicate["Mounts"][0]))
    normalized = canonical_inspection(duplicate)
    assert len(normalized["Mounts"]) == 3
    assert normalized != canonical_inspection(first)
    with pytest.raises(ValueError, match="mount destination is ambiguous"):
        _mounts(normalized)


def test_command_environment_and_lifetime_are_never_sorted_or_removed() -> None:
    first = _inspection()
    for field in ("Cmd", "Env"):
        changed = copy.deepcopy(first)
        changed["Config"][field].reverse()
        assert canonical_inspection(first) != canonical_inspection(changed)
    changed = copy.deepcopy(first)
    changed["State"]["Pid"] = 456
    assert canonical_inspection(first) != canonical_inspection(changed)


def test_legacy_normalization_keeps_the_other_eighteen_fields_exact() -> None:
    mounts = _inspection()["Mounts"]
    first: list[object] = [*range(11), mounts, *range(12, 19)]
    reordered = copy.deepcopy(first)
    reordered[11].reverse()
    assert canonical_legacy_inspection(first) == canonical_legacy_inspection(reordered)
    for index in (*range(11), *range(12, 19)):
        changed = copy.deepcopy(reordered)
        changed[index] = "changed"
        assert canonical_legacy_inspection(first) != canonical_legacy_inspection(changed)
    with pytest.raises(ValueError, match="fields are incomplete"):
        canonical_legacy_inspection(first[:-1])


@pytest.mark.parametrize("value", [None, {}, "mounts", ["not-a-record"]])
def test_malformed_mount_collection_refuses(value: object) -> None:
    with pytest.raises(ValueError, match="mount inspection is malformed"):
        canonical_mounts(value)
