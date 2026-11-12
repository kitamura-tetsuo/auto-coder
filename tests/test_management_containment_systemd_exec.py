"""Real source-file substitutions with native systemd protocol snapshots.

The command facts below are measured Ubuntu 255.4-1ubuntu8.18 readback. The
systemd transport is a component fixture; full-host acceptance remains separate.
"""

from __future__ import annotations

import hashlib
import os
import pwd
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from auto_coder.management import containment_route_manifest as routes
from auto_coder.management import containment_systemd_exec as resolution
from auto_coder.management.containment_route_sources import CommandSourceUnavailable

pytestmark = pytest.mark.usefixtures("_use_real_commands")

# Measured effective values from the native stock systemd-journal-flush unit.
_NATIVE_CONTEXT = """RootDirectoryStartOnly=no
Environment=
PassEnvironment=
UnsetEnvironment=
WorkingDirectory=
RootDirectory=
RootImage=
RootEphemeral=no
ExtensionDirectories=
ExtensionImages=
MountImages=
User=
Group=
DynamicUser=no
PAMName=
ReadWritePaths=
ReadOnlyPaths=
InaccessiblePaths=
ExecPaths=
NoExecPaths=
ExecSearchPath=
PrivateTmp=no
PrivateDevices=no
PrivateUsers=no
PrivateMounts=no
ProtectHome=no
ProtectSystem=no
BindPaths=
BindReadOnlyPaths=
TemporaryFileSystem=
"""
_NATIVE_VERSION = "systemd 255 (255.4-1ubuntu8.18)\n+PAM +AUDIT default-hierarchy=unified\n"


def _row(name: str = "maintenance", arguments: str = "") -> str:
    return "{ path=" + name + " ; argv[]=" + name + (" " + arguments if arguments else "") + " ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"


def _write_helper(path: Path, output: str = "benign") -> None:
    path.write_text("#!/bin/sh\nexec /usr/bin/printf " + output + "\n")
    path.chmod(0o700)


class _SearchFixture:
    def __init__(self, base: Path, monkeypatch: pytest.MonkeyPatch):
        self.base = base
        self.directories = tuple(base / name for name in ("early-sbin", "early-bin", "late-sbin", "late-bin"))
        for directory in self.directories:
            directory.mkdir(mode=0o700)
        self.utility = base / "systemd-path"
        _write_helper(self.utility)
        self.helper = self.directories[-1] / "maintenance"
        _write_helper(self.helper)
        self.context = dict(line.split("=", 1) for line in _NATIVE_CONTEXT.splitlines())
        self.context.update(Id="maintenance.service", LoadState="loaded", UnitFileState="enabled", ActiveState="inactive", SubState="dead", MainPID="0", NeedDaemonReload="no", FragmentPath="/etc/systemd/system/maintenance.service")
        self.rows = (_row(),)
        self.environment_files: tuple[str, ...] = ()
        self.version = _NATIVE_VERSION
        self.manager_version = "255.4-1ubuntu8.18\n"
        self.search_output = ":".join(str(item) for item in self.directories) + "\n"
        self.calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(resolution, "_SYSTEMD_PATH", self.utility)
        monkeypatch.setattr(resolution, "_SUPPORTED_SEARCH_PATH", tuple(str(item) for item in self.directories))
        monkeypatch.setattr(routes, "_command", self.command)

    def command(self, *args: str, **kwargs: object) -> str:
        self.calls.append(args)
        if args == (str(self.utility), "--version"):
            return self.version
        if args == (str(self.utility), "search-binaries-default"):
            return self.search_output
        if args == ("/usr/bin/systemctl", "show", "--property=Version", "--value"):
            return self.manager_version
        if args[0:2] == ("systemctl", "show"):
            assert "--all" in args
            assert args[-2:] == ("--", self.context["Id"])
            return "".join(key + "=" + value + "\n" for key, value in self.context.items()) + "".join("ExecStart=" + row + "\n" for row in self.rows) + "".join("EnvironmentFiles=" + row + "\n" for row in self.environment_files)
        if args[0:2] == ("systemctl", "cat"):
            return "# /etc/systemd/system/maintenance.service\n[Service]\nExecStart=maintenance\n"
        raise AssertionError(args)

    def map(self, *, user: str | None = None) -> tuple[tuple[str, ...], tuple[resolution.SystemdExecSearchFact, ...]]:
        return resolution.resolve_direct_systemd_commands(self.rows, unit=self.context["Id"], shown=self.context, environment_files=self.environment_files, user=user, uid=os.geteuid(), read_command=self.command)

    def observe(self) -> routes.SystemdRouteFact:
        return routes._systemd_unit(self.context["Id"], trusted_uid=os.geteuid(), inspect_local_inputs=True, extra_cues=("/srv/original-state",))


@pytest.fixture
def search_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[_SearchFixture]:
    # Approved-source ancestry excludes shared writable temporary directories.
    with tempfile.TemporaryDirectory(prefix="ac-systemd-search-", dir=pwd.getpwuid(os.geteuid()).pw_dir) as directory:
        yield _SearchFixture(Path(directory), monkeypatch)


def test_native_lookup_binds_every_missing_prefix_and_selected_bytes_without_observer_path(search_fixture: _SearchFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = search_fixture
    decoy = fixture.base / "observer-path"
    decoy.mkdir(mode=0o700)
    _write_helper(decoy / "maintenance", "wrong-path")
    monkeypatch.setenv("PATH", str(decoy))
    mapped, searches = fixture.map()
    assert mapped == (_row().replace("path=maintenance", "path=" + str(fixture.helper), 1),)
    assert "argv[]=maintenance ;" in mapped[0]
    assert len(searches) == 1
    assert [item.path for item in searches[0].candidates] == [str(path / "maintenance") for path in fixture.directories]
    assert all(item.entry is None and item.target is None and item.sha256 == "" for item in searches[0].candidates[:-1])
    chosen = searches[0].candidates[-1]
    assert chosen.sha256 == hashlib.sha256(fixture.helper.read_bytes()).hexdigest()
    assert chosen.target is not None and chosen.target.inode == fixture.helper.stat().st_ino
    assert searches[0].authority.search_path == tuple(str(path) for path in fixture.directories)
    assert searches[0].authority.manager_version == "255.4-1ubuntu8.18"
    assert subprocess.run([searches[0].selected_path], check=True, capture_output=True, text=True, timeout=5).stdout == "benign"
    resolution.reobserve_systemd_exec_searches(searches, uid=os.geteuid(), read_command=fixture.command)
    observed = fixture.observe()
    assert observed.systemd_exec_searches == searches
    assert next(item for item in observed.command_sources if item.path == str(fixture.helper)).sha256 == chosen.sha256
    assert not observed.management_candidate


def test_same_inode_helper_rewrite_is_detected_with_unchanged_directory_metadata(search_fixture: _SearchFixture) -> None:
    fixture = search_fixture
    first = fixture.observe()
    original_inode = fixture.helper.stat().st_ino
    directory_before = fixture.helper.parent.stat()
    _write_helper(fixture.helper, "/srv/original-state")
    directory_after = fixture.helper.parent.stat()
    assert fixture.helper.stat().st_ino == original_inode
    assert (directory_before.st_dev, directory_before.st_ino, directory_before.st_mtime_ns, directory_before.st_ctime_ns) == (directory_after.st_dev, directory_after.st_ino, directory_after.st_mtime_ns, directory_after.st_ctime_ns)
    assert subprocess.run([str(fixture.helper)], check=True, capture_output=True, text=True, timeout=5).stdout == "/srv/original-state"
    with pytest.raises(CommandSourceUnavailable, match="changed during observation"):
        resolution.reobserve_systemd_exec_searches(first.systemd_exec_searches, uid=os.geteuid(), read_command=fixture.command)
    second = fixture.observe()
    assert second.management_candidate
    assert second.sha256 != first.sha256
    assert second.systemd_exec_searches[0].candidates[-1].sha256 != first.systemd_exec_searches[0].candidates[-1].sha256


def test_new_earlier_executable_cannot_retain_the_original_route_fingerprint(search_fixture: _SearchFixture) -> None:
    fixture = search_fixture
    first = fixture.observe()
    earlier = fixture.directories[0] / "maintenance"
    _write_helper(earlier, "/srv/original-state")
    with pytest.raises(CommandSourceUnavailable, match="changed during observation"):
        resolution.reobserve_systemd_exec_searches(first.systemd_exec_searches, uid=os.geteuid(), read_command=fixture.command)
    second = fixture.observe()
    assert second.systemd_exec_searches[0].selected_path == str(earlier)
    assert second.management_candidate
    assert second.sha256 != first.sha256


def test_original_argv_and_runtime_metadata_remain_in_the_route_digest(search_fixture: _SearchFixture) -> None:
    fixture = search_fixture
    fixture.rows = (_row(arguments="--flush"),)
    first = fixture.observe()
    mapped, searches = fixture.map()
    assert "argv[]=maintenance --flush ;" in mapped[0]
    fixture.rows = (fixture.rows[0].replace("pid=0", "pid=1234"),)
    second = fixture.observe()
    assert second.systemd_exec_searches == searches
    assert second.sha256 != first.sha256
    fixture.rows = (_row(arguments="--smart-relinquish-var"),)
    third = fixture.observe()
    assert third.systemd_exec_searches == searches
    assert third.sha256 not in (first.sha256, second.sha256)


def test_every_repeated_direct_command_has_its_own_mapping_and_original_row(search_fixture: _SearchFixture) -> None:
    fixture = search_fixture
    other = fixture.directories[2] / "other-helper"
    _write_helper(other)
    fixture.rows = (_row(), _row("other-helper", "--literal"), _row(arguments="--another"))
    observed = fixture.observe()
    assert [item.executable for item in observed.systemd_exec_searches] == ["maintenance", "other-helper"]
    assert {item.path for item in observed.command_sources} == {str(fixture.helper), str(other)}
    fixture.rows = tuple(reversed(fixture.rows))
    assert fixture.observe().sha256 != observed.sha256


@pytest.mark.parametrize("property_name", resolution.SYSTEMD_EXEC_CONTEXT_PROPERTIES)
def test_missing_effective_context_never_inherits_a_default(search_fixture: _SearchFixture, property_name: str) -> None:
    del search_fixture.context[property_name]
    with pytest.raises(CommandSourceUnavailable, match="missing execution context"):
        search_fixture.map()
    assert search_fixture.calls == []


@pytest.mark.parametrize(
    ("property_name", "value"),
    (
        ("ExecSearchPath", "/opt/tools"),
        ("Environment", "PATH=/opt/tools"),
        ("PassEnvironment", "PATH"),
        ("UnsetEnvironment", "PATH"),
        ("WorkingDirectory", "/opt"),
        ("RootDirectory", "/srv/root"),
        ("RootImage", "/srv/root.img"),
        ("BindPaths", "/opt/tools:/usr/bin"),
        ("BindReadOnlyPaths", "/opt/tools:/usr/bin"),
        ("MountImages", "/opt/image:/usr"),
        ("ExtensionImages", "/opt/image"),
        ("ExtensionDirectories", "/opt/extensions"),
        ("TemporaryFileSystem", "/usr:ro"),
        ("InaccessiblePaths", "/usr/local"),
        ("NoExecPaths", "/usr/local"),
        ("ExecPaths", "/usr/bin"),
        ("ReadOnlyPaths", "/usr"),
        ("ReadWritePaths", "/usr"),
        ("User", "nobody"),
        ("Group", "nogroup"),
        ("DynamicUser", "yes"),
        ("PAMName", "login"),
        ("RootDirectoryStartOnly", "yes"),
        ("RootEphemeral", "yes"),
        ("PrivateUsers", "yes"),
        ("PrivateMounts", "yes"),
        ("PrivateTmp", "yes"),
        ("PrivateDevices", "yes"),
        ("ProtectHome", "yes"),
        ("ProtectSystem", "strict"),
    ),
)
def test_alternate_execution_context_is_outside_the_native_profile(search_fixture: _SearchFixture, property_name: str, value: str) -> None:
    search_fixture.context[property_name] = value
    with pytest.raises(CommandSourceUnavailable, match="execution context"):
        search_fixture.observe()


def test_environment_files_and_user_managers_cannot_authorize_bare_search(search_fixture: _SearchFixture) -> None:
    fixture = search_fixture
    fixture.environment_files = (str(fixture.base / "optional-environment") + " (ignore_errors=yes)",)
    with pytest.raises(CommandSourceUnavailable, match="execution context"):
        fixture.observe()
    fixture.environment_files = ()
    with pytest.raises(CommandSourceUnavailable, match="system-manager context"):
        fixture.map(user="operator")


@pytest.mark.parametrize("program", ("sh", "bash", "dash", "env", "python3", "node", "perl", "ruby", "nice", "nohup", "setsid", "timeout", "flock", "stdbuf", "ionice", "chrt", "taskset", "busybox"))
def test_bare_dispatchers_never_acquire_permission_from_an_absolute_mapping(search_fixture: _SearchFixture, program: str) -> None:
    search_fixture.rows = (_row(program, "/usr/bin/true"),)
    with pytest.raises(CommandSourceUnavailable, match="dispatcher"):
        search_fixture.map()
    assert search_fixture.calls == []


@pytest.mark.parametrize("arguments", ("$COMMAND", "${COMMAND}", "%i", "'literal'", '"literal"', "./../helper", "/usr/bin/true;", "/usr/bin/true | other", "argument\\ value"))
def test_unsupported_argv_expansion_or_shell_syntax_refuses(search_fixture: _SearchFixture, arguments: str) -> None:
    search_fixture.rows = (_row(arguments=arguments),)
    with pytest.raises(CommandSourceUnavailable, match="expansion"):
        search_fixture.map()


def test_overridden_argv_zero_is_not_synthesized_from_the_selected_basename(search_fixture: _SearchFixture) -> None:
    search_fixture.rows = (_row().replace("argv[]=maintenance", "argv[]=other-name"),)
    with pytest.raises(CommandSourceUnavailable, match="argv override"):
        search_fixture.map()


@pytest.mark.parametrize("mutation", ("non-executable", "directory", "group-writable", "missing", "dangling-link", "link-chain"))
def test_ambiguous_candidate_never_falls_through_to_a_later_executable(search_fixture: _SearchFixture, mutation: str) -> None:
    fixture = search_fixture
    path = fixture.directories[0] / "maintenance"
    if mutation == "directory":
        path.mkdir()
    elif mutation == "dangling-link":
        path.symlink_to(fixture.base / "absent")
    elif mutation == "link-chain":
        intermediate = fixture.base / "intermediate"
        intermediate.symlink_to(fixture.helper)
        path.symlink_to(intermediate)
    elif mutation == "missing":
        fixture.helper.unlink()
    else:
        _write_helper(path)
        path.chmod(0o600 if mutation == "non-executable" else 0o770)
    with pytest.raises(CommandSourceUnavailable):
        fixture.map()


def test_selected_symlink_target_and_utility_bytes_are_reobserved(search_fixture: _SearchFixture) -> None:
    fixture = search_fixture
    first_target = fixture.base / "first"
    second_target = fixture.base / "second"
    _write_helper(first_target)
    _write_helper(second_target)
    fixture.helper.unlink()
    fixture.helper.symlink_to(first_target)
    _, first = fixture.map()
    fixture.helper.unlink()
    fixture.helper.symlink_to(second_target)
    with pytest.raises(CommandSourceUnavailable, match="changed during observation"):
        resolution.reobserve_systemd_exec_searches(first, uid=os.geteuid(), read_command=fixture.command)
    _, second = fixture.map()
    _write_helper(fixture.utility, "different-utility")
    with pytest.raises(CommandSourceUnavailable, match="changed during observation"):
        resolution.reobserve_systemd_exec_searches(second, uid=os.geteuid(), read_command=fixture.command)


@pytest.mark.parametrize("mutation", ("manager-version", "utility-version", "search-order"))
def test_search_authority_is_independent_and_version_bound(search_fixture: _SearchFixture, mutation: str) -> None:
    fixture = search_fixture
    if mutation == "manager-version":
        fixture.manager_version = "255.4-1ubuntu8.17\n"
    elif mutation == "utility-version":
        fixture.version = "systemd 256 (256.1)\n"
    else:
        fixture.search_output = ":".join(str(path) for path in reversed(fixture.directories)) + "\n"
    with pytest.raises(CommandSourceUnavailable, match="search authority"):
        fixture.map()


def test_absolute_command_keeps_existing_source_rules_without_native_search(search_fixture: _SearchFixture) -> None:
    fixture = search_fixture
    fixture.rows = (_row().replace("path=maintenance", "path=/usr/bin/true").replace("argv[]=maintenance", "argv[]=/usr/bin/true"),)
    fixture.context.clear()
    fixture.context["Id"] = "absolute.service"
    mapped, searches = fixture.map()
    assert mapped == fixture.rows and searches == ()
    assert fixture.calls == []


def test_change_between_mapping_and_source_inspection_refuses_the_unit(search_fixture: _SearchFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = search_fixture
    original = routes.observe_command_sources

    def inspect(*args: object, **kwargs: object) -> object:
        result = original(*args, **kwargs)
        _write_helper(fixture.helper, "/srv/original-state")
        return result

    monkeypatch.setattr(routes, "observe_command_sources", inspect)
    with pytest.raises(CommandSourceUnavailable, match="changed during observation"):
        fixture.observe()


def test_recorded_missing_prefix_cannot_be_dropped_from_reobservation(search_fixture: _SearchFixture) -> None:
    fixture = search_fixture
    _, searches = fixture.map()
    truncated = (replace(searches[0], candidates=searches[0].candidates[1:]),)
    with pytest.raises(CommandSourceUnavailable, match="changed during observation"):
        resolution.reobserve_systemd_exec_searches(truncated, uid=os.geteuid(), read_command=fixture.command)


def test_symlink_to_a_dispatcher_cannot_gain_permission_under_another_name(search_fixture: _SearchFixture) -> None:
    fixture = search_fixture
    dispatcher = fixture.base / "sh"
    _write_helper(dispatcher)
    fixture.helper.unlink()
    fixture.helper.symlink_to(dispatcher)
    with pytest.raises(CommandSourceUnavailable, match="dispatcher target"):
        fixture.map()


@pytest.mark.parametrize("mutation", ("file-mode", "parent-mode"))
def test_previously_bound_executable_permission_change_refuses(search_fixture: _SearchFixture, mutation: str) -> None:
    fixture = search_fixture
    _, searches = fixture.map()
    if mutation == "file-mode":
        fixture.helper.chmod(0o600)
    else:
        fixture.helper.parent.chmod(0o770)
    with pytest.raises(CommandSourceUnavailable):
        resolution.reobserve_systemd_exec_searches(searches, uid=os.geteuid(), read_command=fixture.command)


def test_native_exited_row_preserves_single_numeric_status(search_fixture: _SearchFixture) -> None:
    # Exact raw ExecStart readback from the native Ubuntu grub-common unit.
    row = "{ path=grub-editenv ; argv[]=grub-editenv /boot/grub/grubenv unset recordfail ; ignore_errors=no ; start_time=[Thu 2026-11-12 09:08:57 UTC] ; stop_time=[Thu 2026-11-12 09:08:57 UTC] ; pid=565 ; code=exited ; status=0 }"
    executable = search_fixture.directories[-1] / "grub-editenv"
    _write_helper(executable)
    search_fixture.rows = (row,)
    mapped, searches = search_fixture.map()
    assert mapped == (row.replace("path=grub-editenv", "path=" + str(executable), 1),)
    assert len(searches) == 1
    assert searches[0].executable == "grub-editenv"
    assert searches[0].selected_path == str(executable)
