"""Real-file component checks; native systemd acceptance remains a VM gate."""

from __future__ import annotations

import hashlib
import os
import pwd
import sys
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import pytest

from auto_coder.management import recovery_watcher_source as watcher
from auto_coder.management.containment_route_manifest import SystemdRouteFact
from auto_coder.management.recovery_containment_model import RecoveryContainmentUnavailable, RecoveryFile, RecoveryLock, RecoveryWatcher

pytestmark = pytest.mark.usefixtures("_use_real_commands")


def _bound(path: Path) -> RecoveryFile:
    info = path.stat()
    return RecoveryFile(str(path), hashlib.sha256(path.read_bytes()).hexdigest(), info.st_dev, info.st_ino)


def _identity(path: Path) -> tuple[int, ...]:
    info = path.lstat()
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@dataclass
class UnitFixture:
    root: Path
    paths: tuple[Path, ...]
    mask: Path
    source: Path
    selected: RecoveryWatcher
    fact: SystemdRouteFact
    content: bytes
    commands: list[tuple[str, ...]]

    def observe(self):
        return watcher.observe_recovery_watcher_unit(self.selected, self.fact, expected_uid=os.geteuid())

    def select_source(self, content: bytes) -> None:
        self.source.write_bytes(content)
        self.selected = replace(self.selected, unit_source=_bound(self.source))


@pytest.fixture
def unit_fixture(monkeypatch: pytest.MonkeyPatch):
    with tempfile.TemporaryDirectory(prefix="ac-watcher-source-", dir=pwd.getpwuid(os.geteuid()).pw_dir) as directory:
        root = Path(directory)
        roots = tuple(root / name for name in ("etc-units", "run-units", "vendor-units"))
        for path in roots:
            path.mkdir(mode=0o700)
        paths = (*roots, root / "absent-parent" / "absent-units")
        config, module, lock = root / "watcher.json", root / "proxy_watch_cli.py", root / "proxy-watch.lock"
        for path in (config, module, lock):
            path.write_bytes(b"{}\n")
            path.chmod(0o600)
        unit = "ac-watch-proxy.service"
        source, mask = roots[1] / unit, roots[0] / unit
        command = sys.executable + " -I -B -m auto_coder.management.proxy_watch_cli --config " + str(config)
        content = ("[Unit]\nDescription=Retained original watcher\n[Service]\nType=simple\nExecStart=" + command + "\nRestart=no\nStandardOutput=append:" + str(root / "watcher-output.log") + "\nStandardError=journal\n").encode()
        source.write_bytes(content)
        source.chmod(0o600)
        mask.symlink_to("/dev/null")
        info = lock.stat()
        selected = RecoveryWatcher(unit, _bound(config), _bound(module), _bound(source), RecoveryLock(str(lock), info.st_dev, info.st_ino))
        fact = SystemdRouteFact(unit, "a" * 64, "masked", "inactive", 0, True, True)
        commands = []

        def command_read(*args, **kwargs):
            commands.append(args)
            assert args == ("systemctl", "show", "--property=UnitPath", "--value")
            return " ".join(str(path) for path in paths) + "\n"

        monkeypatch.setattr(watcher, "_command", command_read)
        yield UnitFixture(root, paths, mask, source, selected, fact, content, commands)


def test_masked_watcher_joins_real_retained_fragment_without_effective_commands(unit_fixture) -> None:
    fixture = unit_fixture
    assert fixture.fact.command_sources == ()
    source_before, mask_before = _identity(fixture.source), _identity(fixture.mask)
    result = fixture.observe()
    assert result.source == fixture.selected.unit_source
    assert result.interpreter == sys.executable
    assert result.load_paths == tuple(str(path) for path in fixture.paths)
    assert result.mask_path == str(fixture.mask)
    assert result.load_roots[-1][1:] == (0, 0, 0, 0, 0)
    assert fixture.source.read_bytes() == fixture.content
    assert _identity(fixture.source) == source_before and _identity(fixture.mask) == mask_before
    assert len(fixture.commands) == 2
    assert RecoveryWatcher.parse(asdict(fixture.selected)) == fixture.selected


@pytest.mark.parametrize("field,value", (("inhibited", False), ("main_pid", 12), ("needs_daemon_reload", True), ("unit", "other.service")))
def test_retained_source_cannot_replace_actual_technical_inhibition(unit_fixture, field, value) -> None:
    unit_fixture.fact = replace(unit_fixture.fact, **{field: value})
    with pytest.raises(RecoveryContainmentUnavailable, match="technically inhibited"):
        unit_fixture.observe()
    assert not unit_fixture.commands


@pytest.mark.parametrize("change", ("missing-isolation", "missing-bytecode-flag", "module", "config", "extra-argument", "environment", "working-directory", "root-directory", "pre-command", "post-command", "duplicate-command", "install-alias", "section-repeat", "specifier", "control"))
def test_retained_unit_requires_the_literal_finite_command_profile(unit_fixture, change: str) -> None:
    content = unit_fixture.content.decode()
    if change == "missing-isolation":
        content = content.replace(" -I -B ", " -B ")
    elif change == "missing-bytecode-flag":
        content = content.replace(" -I -B ", " -I ")
    elif change == "module":
        content = content.replace("auto_coder.management.proxy_watch_cli", "other.module")
    elif change == "config":
        content = content.replace(unit_fixture.selected.configuration.path, str(unit_fixture.root / "different.json"))
    elif change == "extra-argument":
        content = content.replace("\nRestart=", " --other\nRestart=")
    elif change == "duplicate-command":
        content += "ExecStart=/usr/bin/true\n"
    elif change == "install-alias":
        content += "[Install]\nAlias=other.service\n"
    elif change == "section-repeat":
        content += "[Service]\n"
    elif change == "specifier":
        content = content.replace("Description=Retained", "Description=%n Retained")
    elif change == "control":
        content = content.replace("Type=simple", "Type=simple\r")
    else:
        directive = {"environment": "Environment=PYTHONPATH=/other", "working-directory": "WorkingDirectory=/other", "root-directory": "RootDirectory=/other", "pre-command": "ExecStartPre=/usr/bin/true", "post-command": "ExecStartPost=/usr/bin/true"}[change]
        content += directive + "\n"
    unit_fixture.select_source(content.encode())
    with pytest.raises(RecoveryContainmentUnavailable):
        unit_fixture.observe()


def test_same_python_binary_in_another_venv_is_not_the_same_interpreter_selection(unit_fixture) -> None:
    other = unit_fixture.root / "other-venv/bin/python"
    other.parent.mkdir(parents=True)
    other.symlink_to(sys.executable)
    assert other.samefile(sys.executable)
    unit_fixture.select_source(unit_fixture.content.replace(sys.executable.encode(), str(other).encode()))
    with pytest.raises(RecoveryContainmentUnavailable, match="interpreter/module/configuration"):
        unit_fixture.observe()


@pytest.mark.parametrize("change", ("bytes", "inode", "writable", "hardlink"))
def test_retained_source_must_be_the_protected_original_object(unit_fixture, change: str) -> None:
    fixture = unit_fixture
    if change == "bytes":
        fixture.source.write_bytes(fixture.content + b"# changed\n")
    elif change == "inode":
        fixture.source.rename(fixture.root / "retained-old-fragment")
        fixture.source.write_bytes(fixture.content)
        fixture.source.chmod(0o600)
    elif change == "writable":
        fixture.source.chmod(0o666)
    else:
        os.link(fixture.source, fixture.root / "other-link")
    with pytest.raises(RecoveryContainmentUnavailable):
        fixture.observe()


@pytest.mark.parametrize("name", ("ac-watch-proxy.service.d", "ac-watch-.service.d", "ac-.service.d", "service.d"))
def test_unit_dash_prefix_and_type_wide_drop_ins_refuse(unit_fixture, name: str) -> None:
    (unit_fixture.paths[2] / name).mkdir()
    with pytest.raises(RecoveryContainmentUnavailable, match="drop-ins"):
        unit_fixture.observe()


@pytest.mark.parametrize("kind", ("competing-fragment", "extra-mask", "missing-mask", "source-above-mask", "source-outside-load-path"))
def test_real_unit_load_path_selection_cannot_be_ambiguous(unit_fixture, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    fixture = unit_fixture
    if kind == "competing-fragment":
        (fixture.paths[2] / fixture.selected.unit).write_bytes(fixture.content)
    elif kind == "extra-mask":
        (fixture.paths[2] / fixture.selected.unit).symlink_to("/dev/null")
    elif kind == "missing-mask":
        fixture.mask.unlink()
    elif kind == "source-above-mask":
        monkeypatch.setattr(watcher, "_command", lambda *args, **kwargs: " ".join(str(path) for path in (fixture.paths[1], fixture.paths[0], *fixture.paths[2:])))
    else:
        monkeypatch.setattr(watcher, "_command", lambda *args, **kwargs: " ".join(str(path) for path in (fixture.paths[0], *fixture.paths[2:])))
    with pytest.raises(RecoveryContainmentUnavailable):
        fixture.observe()


@pytest.mark.parametrize("kind", ("direct", "missing-vendor-target", "name-chain", "relative"))
def test_aliases_follow_systemd_unit_names_even_when_literal_target_is_missing(unit_fixture, kind: str) -> None:
    fixture = unit_fixture
    alias = fixture.paths[0] / "generic.service"
    if kind == "direct":
        alias.symlink_to(fixture.source)
    elif kind == "missing-vendor-target":
        target = fixture.paths[2] / fixture.selected.unit
        assert not target.exists()
        alias.symlink_to(target)
    elif kind == "name-chain":
        alias.symlink_to(fixture.paths[2] / "middle.service")
        (fixture.paths[1] / "middle.service").symlink_to(fixture.paths[2] / fixture.selected.unit)
    else:
        (fixture.paths[1] / "generic.service").symlink_to(fixture.selected.unit)
    with pytest.raises(RecoveryContainmentUnavailable, match="alias"):
        fixture.observe()


@pytest.mark.parametrize("change", ("mask", "alias", "source"))
def test_retained_unit_readback_repeats_source_and_load_path_authority(unit_fixture, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    fixture = unit_fixture
    real_read = watcher.read_bound_file
    calls = 0

    def changing_read(*args, **kwargs):
        nonlocal calls
        value = real_read(*args, **kwargs)
        calls += 1
        if calls == 1:
            if change == "mask":
                fixture.mask.rename(fixture.root / "old-mask")
                fixture.mask.symlink_to("/dev/null")
            elif change == "alias":
                (fixture.paths[0] / "new-alias.service").symlink_to(fixture.paths[2] / fixture.selected.unit)
            else:
                fixture.source.write_bytes(fixture.content + b"# changed after first read\n")
        return value

    monkeypatch.setattr(watcher, "read_bound_file", changing_read)
    with pytest.raises(RecoveryContainmentUnavailable):
        fixture.observe()


@pytest.mark.parametrize("change", ("missing-source", "wrong-name", "instance"))
def test_enrollment_requires_an_unambiguous_retained_watcher_fragment(unit_fixture, change: str) -> None:
    raw = asdict(unit_fixture.selected)
    if change == "missing-source":
        raw.pop("unit_source")
    elif change == "wrong-name":
        raw["unit_source"]["path"] = str(unit_fixture.source.parent / "other.service")
    else:
        raw["unit"] = "ac-watch@one.service"
    with pytest.raises(RecoveryContainmentUnavailable):
        RecoveryWatcher.parse(raw)
