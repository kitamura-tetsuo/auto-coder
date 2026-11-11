"""Fast checks of installed acceptance oracles; no simulated host acceptance."""

from __future__ import annotations

import copy
import itertools
import json
from types import SimpleNamespace

import pytest

from tests import test_release_worker_acceptance_container as acceptance


def _facts() -> dict[str, object]:
    return {
        "Id": "container-original",
        "State": {"Pid": 123, "StartedAt": "2026-11-11T06:00:00Z"},
        "Mounts": [
            {"Type": "bind", "Source": "/host/source", "Destination": "/source", "Mode": "ro", "RW": False, "Propagation": "rprivate"},
            {"Type": "bind", "Source": "/host/runtime", "Destination": "/runtime", "Mode": "", "RW": True, "Propagation": "rprivate"},
            {"Type": "bind", "Source": "/host/routing", "Destination": "/routing", "Mode": "", "RW": True, "Propagation": "rprivate"},
        ],
    }


def _identity(monkeypatch, facts):
    def inspect(*arguments):
        assert arguments == ("inspect", "disposable-container")
        return SimpleNamespace(stdout=json.dumps([facts]))

    monkeypatch.setattr(acceptance, "_docker", inspect)
    return acceptance._container_identity("disposable-container")


def test_container_identity_ignores_only_mount_order(monkeypatch) -> None:
    facts = _facts()
    original = copy.deepcopy(facts)
    expected = _identity(monkeypatch, facts)
    for permutation in itertools.permutations(facts["Mounts"]):
        changed = {**facts, "Mounts": list(permutation)}
        assert _identity(monkeypatch, changed) == expected
    assert facts == original
    assert expected["mounts"] == sorted(facts["Mounts"], key=lambda item: json.dumps(item, sort_keys=True))


@pytest.mark.parametrize("field,value", [("Mode", "rw"), ("RW", True), ("Source", "/host/other"), ("Destination", "/other"), ("Propagation", "rshared"), ("Type", "volume"), ("FutureField", "different")])
def test_container_identity_retains_every_mount_field(monkeypatch, field: str, value: object) -> None:
    facts = _facts()
    expected = _identity(monkeypatch, facts)
    changed = copy.deepcopy(facts)
    changed["Mounts"][0][field] = value
    assert _identity(monkeypatch, changed) != expected


@pytest.mark.parametrize("change", ["missing", "duplicate", "replace-with-duplicate"])
def test_container_identity_retains_mount_multiplicity(monkeypatch, change: str) -> None:
    facts = _facts()
    expected = _identity(monkeypatch, facts)
    changed = copy.deepcopy(facts)
    if change == "missing":
        changed["Mounts"].pop()
    elif change == "duplicate":
        changed["Mounts"].append(copy.deepcopy(changed["Mounts"][0]))
    else:
        changed["Mounts"][1] = copy.deepcopy(changed["Mounts"][0])
    assert _identity(monkeypatch, changed) != expected


@pytest.mark.parametrize("field,value", [("Id", "container-other"), ("Pid", 456), ("StartedAt", "2026-11-11T07:00:00Z")])
def test_container_identity_retains_process_identity(monkeypatch, field: str, value: object) -> None:
    facts = _facts()
    expected = _identity(monkeypatch, facts)
    changed = copy.deepcopy(facts)
    if field == "Id":
        changed[field] = value
    else:
        changed["State"][field] = value
    assert _identity(monkeypatch, changed) != expected
