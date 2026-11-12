"""Component checks execute the real guest setup and systemd fact parser."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_coder.management import containment_route_manifest as routes

GUEST = Path(__file__).resolve().parents[1] / "scripts/recovery_vm/guest.sh"
MAINTENANCE_COMMAND = ".venv/bin/python - \"$output/maintenance-profile.json\" <<'PY'\n"
TIMER = "systemd-tmpfiles-clean.timer"
SERVICE = "systemd-tmpfiles-clean.service"
UNITS = (TIMER, SERVICE)


class SystemdFixture:
    """Supply systemctl readback while retaining real parser and mask objects."""

    def __init__(self, root: Path, fault: str | None = None) -> None:
        self.root = root
        self.fault = fault
        self.calls: list[tuple[str, ...]] = []
        self.stopped: set[str] = set()
        self.masked = False
        self.reads = {unit: 0 for unit in UNITS}
        self.timer_rows: list[dict[str, str]] = []

    def command(self, *arguments: str) -> str:
        assert arguments[0] == "systemctl" and arguments[-1] in UNITS
        unit = arguments[-1]
        if arguments[1] == "cat":
            return "# Installed maintenance unit\n[Service]\nExecStart=/usr/bin/systemd-tmpfiles --clean\n"
        assert arguments[1] == "show"
        state = {
            "Id": unit,
            "LoadState": "masked" if self.masked else "loaded",
            "UnitFileState": "masked" if self.masked else "static",
            "ActiveState": "inactive" if unit in self.stopped else "active",
            "SubState": "dead" if unit in self.stopped else "running" if unit == SERVICE else "waiting",
            "NeedDaemonReload": "no",
            "FragmentPath": str(self.root / unit) if self.masked else "/usr/lib/systemd/system/" + unit,
        }
        if unit == SERVICE:
            state["MainPID"] = "0" if unit in self.stopped else "123"
        if not self.masked and self.fault == "missing" and unit == SERVICE:
            state.update(LoadState="not-found", UnitFileState="", ActiveState="inactive", FragmentPath="")
        if not self.masked and unit in self.stopped and self.fault == "still-running" and unit == SERVICE:
            state.update(ActiveState="active", SubState="running", MainPID="123")
        if self.masked:
            self.reads[unit] += 1
            if unit == SERVICE and self.fault and self.fault.startswith("property:"):
                _, key, value = self.fault.split(":", 2)
                if value == "missing":
                    state.pop(key)
                else:
                    state[key] = value
            if unit == TIMER and self.fault == "changed-mask" and self.reads[unit] == 2:
                path = self.root / unit
                path.rename(self.root / "retained-original-mask")
                path.symlink_to("/dev/null")
        if unit == TIMER:
            self.timer_rows.append(state.copy())
        return "\n".join(key + "=" + value for key, value in state.items())

    def run(self, command: tuple[str, ...], *, check: bool, timeout: int) -> subprocess.CompletedProcess[str]:
        assert check is True and timeout == 30
        self.calls.append(command)
        if command[:3] == ("systemctl", "stop", "--"):
            unit = command[3]
            assert unit in UNITS
            if self.fault == "stop-failed" and unit == SERVICE:
                raise subprocess.CalledProcessError(1, command)
            self.stopped.add(unit)
        elif command == ("systemctl", "mask", "--", *UNITS):
            assert self.stopped == set(UNITS)
            for unit in UNITS:
                path = self.root / unit
                if self.fault == "regular-mask" and unit == SERVICE:
                    path.write_text("not a mask")
                else:
                    path.symlink_to("/dev/zero" if self.fault == "wrong-mask" and unit == SERVICE else "/dev/null")
            self.masked = True
        else:
            assert command == ("systemctl", "daemon-reload") and self.masked
        return subprocess.CompletedProcess(command, 0, "", "")


@pytest.fixture
def maintenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    text = GUEST.read_text()
    assert text.count(MAINTENANCE_COMMAND) == 1
    body = text.split(MAINTENANCE_COMMAND, 1)[1].split("\nPY\n", 1)[0]
    namespace = {"__name__": "recovery_vm_maintenance_component"}
    exec(compile(body, str(GUEST), "exec"), namespace)
    mask_root = tmp_path / "masks"
    mask_root.mkdir()
    namespace["MASK_ROOT"] = mask_root
    actual_reader = routes._systemd_unit
    namespace["_systemd_unit"] = lambda unit: actual_reader(unit, trusted_uid=os.geteuid())

    def prepare(fault: str | None = None) -> SystemdFixture:
        fixture = SystemdFixture(mask_root, fault)
        monkeypatch.setattr(routes, "_command", fixture.command)
        namespace["subprocess"] = SimpleNamespace(run=fixture.run)
        return fixture

    return namespace["inhibit_maintenance"], prepare, tmp_path / "maintenance-profile.json", mask_root


def test_guest_inhibits_only_exact_pair_before_originals_and_control_copy() -> None:
    text = GUEST.read_text()
    setup = text.index(MAINTENANCE_COMMAND)
    assert text.index("verify_installed_watcher\n") < setup
    assert setup < text.index("driver_module=scripts.test_management_recovery_epoch_host")
    assert setup < text.index("scripts.recovery_epoch_mutation_control prepare")
    assert setup < text.index('fd = os.open("/etc/auto-coder-disposable-containment-host.json"')
    assert setup < text.index('.venv/bin/python -m "$driver_module"')
    subprocess.run(("bash", "-n", str(GUEST)), check=True, timeout=10)


def test_real_guest_block_stops_timer_first_and_retains_actual_masks(maintenance) -> None:
    inhibit, prepare, output, masks = maintenance
    systemd = prepare()
    inhibit(output)
    assert systemd.calls == [
        ("systemctl", "stop", "--", TIMER),
        ("systemctl", "stop", "--", SERVICE),
        ("systemctl", "mask", "--", *UNITS),
        ("systemctl", "daemon-reload"),
    ]
    evidence = json.loads(output.read_text())
    assert evidence["schema"] == "auto-coder-recovery-vm-maintenance-profile-v1" and evidence["status"] == "inhibited"
    assert [fact["unit"] for fact in evidence["observed"]] == list(UNITS)
    assert evidence["initial"][1]["main_pid"] == 123
    assert all(fact["active_state"] == "inactive" and fact["main_pid"] == 0 for fact in evidence["stopped"])
    assert all(fact["inhibited"] and fact["unit_file_state"] == "masked" and fact["main_pid"] == 0 and not fact["needs_daemon_reload"] for fact in evidence["observed"])
    assert all("MainPID" not in row for row in systemd.timer_rows)
    assert systemd.reads == {TIMER: 2, SERVICE: 2}
    for record in evidence["masks"]:
        path = masks / record["unit"]
        info = path.lstat()
        assert record == {"unit": path.name, "path": str(path), "device": info.st_dev, "inode": info.st_ino, "ctime_ns": info.st_ctime_ns, "mode": info.st_mode, "uid": info.st_uid, "target": "/dev/null"}
        assert os.readlink(path) == "/dev/null"


def test_existing_mask_is_preserved_without_stopping_or_resetting(maintenance) -> None:
    inhibit, prepare, output, masks = maintenance
    path = masks / TIMER
    path.symlink_to("/dev/null")
    before = path.lstat()
    systemd = prepare()
    with pytest.raises(ValueError, match="mask paths are not empty"):
        inhibit(output)
    assert path.lstat() == before and os.readlink(path) == "/dev/null"
    assert systemd.calls == []
    assert json.loads(output.read_text())["status"] == "failed"


@pytest.mark.parametrize("fault", ("missing", "stop-failed", "still-running"))
def test_missing_or_unstopped_service_never_installs_a_mask(maintenance, fault: str) -> None:
    inhibit, prepare, output, masks = maintenance
    systemd = prepare(fault)
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        inhibit(output)
    assert not tuple(masks.iterdir())
    assert all(command[1] == "stop" for command in systemd.calls)
    if fault == "missing":
        assert systemd.calls == []
    evidence = json.loads(output.read_text())
    assert evidence["status"] == "failed" and evidence["failure_type"]
    assert "observed" not in evidence


@pytest.mark.parametrize(
    "fault",
    (
        "property:LoadState:loaded",
        "property:UnitFileState:static",
        "property:ActiveState:active",
        "property:SubState:running",
        "property:MainPID:123",
        "property:MainPID:missing",
        "property:NeedDaemonReload:yes",
        "regular-mask",
        "wrong-mask",
        "changed-mask",
    ),
)
def test_guest_refuses_incomplete_or_changed_actual_inhibition(maintenance, fault: str) -> None:
    inhibit, prepare, output, masks = maintenance
    systemd = prepare(fault)
    with pytest.raises(ValueError):
        inhibit(output)
    evidence = json.loads(output.read_text())
    assert evidence["status"] == "failed" and evidence["failure_type"]
    assert systemd.calls[-1] == ("systemctl", "daemon-reload")
    assert len(systemd.calls) == 4
    assert (masks / TIMER).is_symlink()
    assert all("reset-failed" not in command and "unmask" not in command for command in systemd.calls)
