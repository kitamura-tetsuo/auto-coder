"""Non-authorizing read-only tests for one real installed systemd route."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from auto_coder.management import containment_observation as containment
from auto_coder.management import systemd_watcher_observation as watcher

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")


def _state(**overrides: str) -> dict[str, str]:
    result = {
        "Id": watcher.WATCHER_UNIT,
        "LoadState": "masked",
        "UnitFileState": "masked",
        "ActiveState": "inactive",
        "SubState": "dead",
        "MainPID": "0",
        "NeedDaemonReload": "no",
        "FragmentPath": str(watcher.WATCHER_UNIT_PATH),
    }
    result.update(overrides)
    return result


def _masked() -> watcher.WatcherSystemdObservation:
    return watcher.WatcherSystemdObservation(watcher.WATCHER_UNIT, str(watcher.WATCHER_UNIT_PATH), "masked", "masked", "inactive", "dead", 0, False, 1, 2, 3, 0, 0o777, "/dev/null", True)


def _scope() -> containment.ProposedScope:
    return containment.ProposedScope("unit-deployment", "unit-enrollment", "unit-authorization", "ordinary-v1", "a" * 64, "b" * 64)


def test_fixed_installed_unit_is_sampled_twice_before_mask_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    shows: list[str] = []
    links: list[Path] = []

    def show(unit: str) -> dict[str, str]:
        shows.append(unit)
        return _state()

    def link(path: Path) -> tuple[int, int, int, int, int, str]:
        links.append(path)
        return (1, 2, 3, 0, 0o777, "/dev/null")

    monkeypatch.setattr(watcher, "_show", show)
    monkeypatch.setattr(watcher, "_link", link)
    result = watcher.observe_watcher_unit()
    assert result == _masked()
    assert shows == [watcher.WATCHER_UNIT, watcher.WATCHER_UNIT]
    assert links == [watcher.WATCHER_UNIT_PATH, watcher.WATCHER_UNIT_PATH]
    assert result.inhibited


@pytest.mark.parametrize(
    "override",
    [
        {"UnitFileState": "enabled"},
        {"ActiveState": "active", "SubState": "running", "MainPID": "123"},
        {"NeedDaemonReload": "yes"},
        {"FragmentPath": "/tmp/untrusted-unit"},
    ],
)
def test_uninhibited_or_stale_state_is_never_positive(monkeypatch: pytest.MonkeyPatch, override: dict[str, str]) -> None:
    monkeypatch.setattr(watcher, "_show", lambda _unit: _state(**override))
    monkeypatch.setattr(watcher, "_link", lambda _path: (1, 2, 3, 0, 0o777, "/dev/null"))
    assert not watcher.observe_watcher_unit().inhibited


@pytest.mark.parametrize("target,owner", [("/etc/shadow", 0), ("/dev/null", 1000)])
def test_untrusted_mask_link_is_never_positive(monkeypatch: pytest.MonkeyPatch, target: str, owner: int) -> None:
    monkeypatch.setattr(watcher, "_show", lambda _unit: _state())
    monkeypatch.setattr(watcher, "_link", lambda _path: (1, 2, 3, owner, 0o777, target))
    assert not watcher.observe_watcher_unit().inhibited


def test_systemd_change_between_samples_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    states = iter((_state(), _state(NeedDaemonReload="yes")))
    monkeypatch.setattr(watcher, "_show", lambda _unit: next(states))
    monkeypatch.setattr(watcher, "_link", lambda _path: (1, 2, 3, 0, 0o777, "/dev/null"))
    with pytest.raises(ValueError, match="changed during observation"):
        watcher.observe_watcher_unit()


def test_unit_link_change_between_samples_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    links = iter(((1, 2, 3, 0, 0o777, "/dev/null"), (1, 9, 4, 0, 0o777, "/dev/null")))
    monkeypatch.setattr(watcher, "_show", lambda _unit: _state())
    monkeypatch.setattr(watcher, "_link", lambda _path: next(links))
    with pytest.raises(ValueError, match="changed during observation"):
        watcher.observe_watcher_unit()


def test_link_inspection_reads_actual_symlink_identity(tmp_path: Path) -> None:
    link = tmp_path / "fixture.service"
    link.symlink_to("/dev/null")
    info = link.lstat()
    assert watcher._link(link) == (info.st_dev, info.st_ino, info.st_ctime_ns, info.st_uid, 0o777, "/dev/null")
    assert watcher._link(tmp_path / "absent.service") == (0, 0, 0, 0, 0, "")


def test_systemctl_output_requires_exact_unambiguous_properties(monkeypatch: pytest.MonkeyPatch) -> None:
    command_seen: list[tuple[str, ...]] = []

    def systemctl(command: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command_seen.append(command)
        return subprocess.CompletedProcess(command, 0, "\n".join(key + "=" + value for key, value in _state().items()) + "\n", "")

    monkeypatch.setattr(watcher.subprocess, "run", systemctl)
    assert watcher._show(watcher.WATCHER_UNIT) == _state()
    assert command_seen == [("systemctl", "show", watcher.WATCHER_UNIT, "--property=" + ",".join(watcher._PROPERTIES))]

    def duplicated(command: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        output = "\n".join(key + "=" + value for key, value in _state().items()) + "\nMainPID=0\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(watcher.subprocess, "run", duplicated)
    with pytest.raises(ValueError, match="ambiguous"):
        watcher._show(watcher.WATCHER_UNIT)


def test_separate_consumer_independently_rechecks_watcher_and_refuses_reenabled_route(monkeypatch: pytest.MonkeyPatch) -> None:
    facts = [_masked(), replace(_masked(), unit_file_state="enabled", inhibited=False)]
    monkeypatch.setattr(containment, "observe_watcher_unit", lambda: facts.pop(0))
    monkeypatch.setattr(containment, "_docker", lambda _scope: ())
    saved = json.loads(json.dumps(asdict(containment.observe(_scope()))))
    assert saved["watcher_systemd"]["inhibited"] is True
    current = containment.verify(_scope(), saved)
    reasons = {item.authority: item.reason for item in current.unavailable}
    assert current.watcher_systemd is not None and not current.watcher_systemd.inhibited
    assert "watcher-systemd" in reasons
    assert "saved-report:watcher_systemd" in reasons
    assert current.status == "unavailable"
