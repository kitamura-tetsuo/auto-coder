"""Protocol tests for the native fsck block-data role, not host acceptance.

The command and vendor fragment are the captured native profile. Filesystem
metadata is controlled here; the disposable VM provides actual device evidence.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_coder.management import containment_route_manifest as routes
from auto_coder.management import containment_route_sources as sources

UNIT = r"systemd-fsck@dev-disk-by\x2dlabel-BOOT.service"
OPERAND = "/dev/disk/by-label/BOOT"
TARGET = "/dev/vda16"
EXECUTABLE = "/usr/lib/systemd/systemd-fsck"
FRAGMENT = "/usr/lib/systemd/system/systemd-fsck@.service"
ROW = "{ path=/usr/lib/systemd/systemd-fsck ; argv[]=/usr/lib/systemd/systemd-fsck /dev/disk/by-label/BOOT ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"


def _node(mode: int, inode: int, *, gid: int = 0, rdev: int = 0) -> SimpleNamespace:
    return SimpleNamespace(st_dev=9, st_ino=inode, st_mode=mode, st_uid=0, st_gid=gid, st_rdev=rdev, st_size=0, st_mtime_ns=100, st_ctime_ns=200)


class NativeFsck:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.fragment = Path(__file__).with_name("fixtures").joinpath("management-systemd-fsck.service").read_bytes()
        assert hashlib.sha256(self.fragment).hexdigest() == sources._FSCK_FRAGMENT_SHA256
        self.cat = "# " + FRAGMENT + "\n" + self.fragment.decode()
        self.properties = {
            "Id": UNIT,
            "LoadState": "loaded",
            "UnitFileState": "static",
            "ActiveState": "active",
            "SubState": "exited",
            "MainPID": "0",
            "NeedDaemonReload": "no",
            "FragmentPath": FRAGMENT,
            "SourcePath": "",
            "DropInPaths": "",
            "Environment": "",
            "WorkingDirectory": "",
            "RootDirectory": "",
            "RootImage": "",
        }
        self.command = ROW
        self.extra_rows: list[str] = []
        self.target = TARGET
        self.executable_target = EXECUTABLE
        self.nodes = {
            OPERAND: _node(stat.S_IFLNK | 0o777, 101),
            TARGET: _node(stat.S_IFBLK | 0o660, 102, gid=6, rdev=os.makedev(259, 0)),
            EXECUTABLE: _node(stat.S_IFREG | 0o755, 103),
            FRAGMENT: _node(stat.S_IFREG | 0o644, 104),
        }
        self.fragment_reads: list[str] = []
        self.operand_reads = 0
        self.change_during_readback = False
        self.change_fragment_readback = False
        original_lstat, original_stat, original_resolve = Path.lstat, Path.stat, Path.resolve

        def lstat(path: Path, *args: object, **kwargs: object) -> object:
            name = str(path)
            if name == OPERAND:
                self.operand_reads += 1
                if self.change_during_readback and self.operand_reads == 2:
                    self.nodes[TARGET].st_rdev = os.makedev(259, 1)
            return self.nodes[name] if name in self.nodes else original_lstat(path, *args, **kwargs)

        def node_stat(path: Path, *args: object, **kwargs: object) -> object:
            name = str(path)
            if name == OPERAND:
                return self.nodes[self.target]
            return self.nodes[name] if name in self.nodes else original_stat(path, *args, **kwargs)

        def resolve(path: Path, *args: object, **kwargs: object) -> Path:
            if str(path) == OPERAND:
                return Path(self.target)
            if str(path) == EXECUTABLE:
                return Path(self.executable_target)
            if str(path) in self.nodes:
                return path
            return original_resolve(path, *args, **kwargs)

        def protected(path: Path, *, owners: frozenset[int]) -> tuple[bytes, object]:
            # Device content is never a permitted file read in this protocol.
            assert str(path) == FRAGMENT
            assert owners == frozenset((0,))
            self.fragment_reads.append(str(path))
            if self.change_fragment_readback and len(self.fragment_reads) == 2:
                self.nodes[FRAGMENT].st_ctime_ns += 1
            return self.fragment, self.nodes[FRAGMENT]

        def command(*args: str, **kwargs: object) -> str:
            if "show" in args:
                return "\n".join([*(key + "=" + value for key, value in self.properties.items()), "ExecStart=" + self.command, *self.extra_rows]) + "\n"
            assert "cat" in args
            return self.cat

        monkeypatch.setattr(Path, "lstat", lstat)
        monkeypatch.setattr(Path, "stat", node_stat)
        monkeypatch.setattr(Path, "resolve", resolve)
        monkeypatch.setattr(sources, "_protected_source", protected)
        monkeypatch.setattr(routes, "_command", command)

    def observe(self) -> routes.SystemdRouteFact:
        return routes._systemd_unit(UNIT, inspect_local_inputs=True)


def test_native_fsck_block_argument_binds_real_shape_without_reading_device(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = NativeFsck(monkeypatch)
    fact = fixture.observe()
    assert not fact.inhibited
    assert not fact.management_candidate
    assert len(fact.fsck_block_operands) == 1
    block = fact.fsck_block_operands[0]
    assert (block.path, block.resolved) == (OPERAND, TARGET)
    assert block.entry.mode == stat.S_IFLNK | 0o777
    assert block.target.mode == stat.S_IFBLK | 0o660
    assert block.target.gid == 6
    assert block.target.rdev == os.makedev(259, 0)
    assert fixture.fragment_reads == [FRAGMENT, FRAGMENT]
    assert [item.path for item in fact.command_sources] == [FRAGMENT]
    assert sources.fsck_block_operand(ROW) == (OPERAND, ROW.replace(OPERAND, "", 1))
    assert EXECUTABLE in sources.fsck_block_operand(ROW)[1]


def test_original_fsck_exec_row_remains_in_the_route_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = NativeFsck(monkeypatch)
    first = fixture.observe()
    fixture.command = ROW.replace("pid=0", "pid=123")
    second = fixture.observe()
    assert second.fsck_block_operands == first.fsck_block_operands
    assert second.sha256 != first.sha256


@pytest.mark.parametrize("mutation", ("argv-extra", "executable", "root-image", "root-directory", "working-directory", "environment", "environment-file", "other-exec", "drop-in", "missing-context", "changed-fragment", "extra-fragment", "executable-link"))
def test_block_data_role_cannot_hide_execution_or_source_inputs(monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    fixture = NativeFsck(monkeypatch)
    if mutation == "argv-extra":
        fixture.command = ROW.replace(OPERAND + " ;", OPERAND + " --repair ;")
    elif mutation == "executable":
        fixture.command = ROW.replace("path=" + EXECUTABLE, "path=/usr/bin/python3")
    elif mutation == "root-image":
        fixture.properties["RootImage"] = OPERAND
    elif mutation == "root-directory":
        fixture.properties["RootDirectory"] = "/alternate-root"
    elif mutation == "working-directory":
        fixture.properties["WorkingDirectory"] = "/alternate-work"
    elif mutation == "environment":
        fixture.properties["Environment"] = "INPUT=" + OPERAND
    elif mutation == "environment-file":
        fixture.extra_rows.append("EnvironmentFiles=" + OPERAND + " (ignore_errors=yes)")
    elif mutation == "other-exec":
        fixture.extra_rows.append("ExecStartPre=/usr/bin/python3 " + OPERAND)
    elif mutation == "drop-in":
        fixture.properties["DropInPaths"] = "/etc/systemd/system/fsck.service.d/override.conf"
    elif mutation == "missing-context":
        del fixture.properties["RootImage"]
    elif mutation == "changed-fragment":
        fixture.fragment += b"BindPaths=/opt/other-fsck:/usr/lib/systemd/systemd-fsck\n"
        fixture.cat = "# " + FRAGMENT + "\n" + fixture.fragment.decode()
    elif mutation == "extra-fragment":
        fixture.cat += "\n# /etc/systemd/system/fsck.service.d/override.conf\n[Service]\nBindPaths=/opt/other-fsck:/usr/lib/systemd/systemd-fsck\n"
    else:
        fixture.nodes[EXECUTABLE].st_mode = stat.S_IFLNK | 0o777
        fixture.executable_target = "/usr/bin/python3"
    with pytest.raises(sources.CommandSourceUnavailable):
        fixture.observe()


@pytest.mark.parametrize("field,value", (("st_rdev", os.makedev(259, 2)), ("st_ino", 999), ("st_gid", 7), ("st_mode", stat.S_IFBLK | 0o600), ("st_ctime_ns", 201)))
def test_block_target_identity_changes_change_the_route_digest(monkeypatch: pytest.MonkeyPatch, field: str, value: int) -> None:
    fixture = NativeFsck(monkeypatch)
    first = fixture.observe()
    setattr(fixture.nodes[TARGET], field, value)
    second = fixture.observe()
    assert second.fsck_block_operands != first.fsck_block_operands
    assert second.sha256 != first.sha256


def test_block_symlink_retarget_changes_bound_target(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = NativeFsck(monkeypatch)
    first = fixture.observe()
    fixture.target = "/dev/vda17"
    fixture.nodes[fixture.target] = _node(stat.S_IFBLK | 0o660, 999, gid=6, rdev=os.makedev(259, 1))
    fixture.nodes[OPERAND].st_ino += 1
    second = fixture.observe()
    assert second.fsck_block_operands[0].resolved == fixture.target
    assert second.sha256 != first.sha256


@pytest.mark.parametrize("mutation", ("target-type", "target-owner", "entry-owner", "readback-race", "fragment-race"))
def test_unsupported_or_racing_block_endpoint_refuses(monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    fixture = NativeFsck(monkeypatch)
    if mutation == "target-type":
        fixture.nodes[TARGET].st_mode = stat.S_IFREG | 0o660
    elif mutation == "target-owner":
        fixture.nodes[TARGET].st_uid = 1000
    elif mutation == "entry-owner":
        fixture.nodes[OPERAND].st_uid = 1000
    elif mutation == "fragment-race":
        fixture.change_fragment_readback = True
    else:
        fixture.change_during_readback = True
    with pytest.raises(sources.CommandSourceUnavailable):
        fixture.observe()


def test_generic_block_source_is_still_refused_even_without_group_write(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = NativeFsck(monkeypatch)
    fixture.nodes[TARGET].st_mode = stat.S_IFBLK | 0o600
    with pytest.raises(sources.CommandSourceUnavailable, match="unsupported filesystem object"):
        sources.observe_command_sources(OPERAND, uid=0, cues=())
