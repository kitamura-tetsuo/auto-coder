"""Fast checks of installed acceptance oracles; no simulated host acceptance."""

from __future__ import annotations

import copy
import itertools
import json
import os
import subprocess
import sys
from pathlib import Path
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


def _lifetime_observation(monkeypatch, observed: str, token: str = "12345", pid: int = 223) -> None:
    def read_stat(*arguments):
        assert arguments[:3] == ("exec", "disposable-container", "python")
        assert arguments[-1] == str(pid)
        return SimpleNamespace(stdout=observed)

    monkeypatch.setattr(acceptance, "_docker", read_stat)
    acceptance._assert_lifetime_exited("disposable-container", {"pid": pid, "start_token": token})


def _stat(state: str, token: str = "12345") -> str:
    fields = [state, *(["0"] * 18), token, *(["0"] * 32)]
    return "223 (worker (original)) " + " ".join(fields)


@pytest.mark.parametrize("state", ["R", "S", "D", "T", "t", "W", "K", "P", "I"])
def test_original_live_or_stopped_lifetime_is_never_exited(monkeypatch, state: str) -> None:
    with pytest.raises(AssertionError):
        _lifetime_observation(monkeypatch, _stat(state))


@pytest.mark.parametrize("observed", ["ABSENT", _stat("Z"), _stat("X"), _stat("S", "67890")])
def test_lifetime_exit_oracle_accepts_only_exit_or_replaced_lifetime(monkeypatch, observed: str) -> None:
    _lifetime_observation(monkeypatch, observed)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux kernel process-exit observation")
def test_actual_exited_child_is_proven_before_parent_reaps_it(monkeypatch, _use_real_commands: None) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"], stdin=subprocess.PIPE)
    try:
        path = Path(f"/proc/{child.pid}/stat")
        before = path.read_text()
        token = before.rsplit(") ", 1)[1].split()[19]
        with pytest.raises(AssertionError):
            _lifetime_observation(monkeypatch, before, token, child.pid)
        assert child.stdin is not None
        child.stdin.close()
        exited = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
        assert exited.si_pid == child.pid and exited.si_code == os.CLD_EXITED and exited.si_status == 0
        retained = path.read_text()
        fields = retained.rsplit(") ", 1)[1].split()
        assert fields[0] == "Z" and fields[19] == token
        _lifetime_observation(monkeypatch, retained, token, child.pid)
    finally:
        if child.stdin is not None:
            child.stdin.close()
        assert child.wait(timeout=10) == 0
    assert not path.exists()
