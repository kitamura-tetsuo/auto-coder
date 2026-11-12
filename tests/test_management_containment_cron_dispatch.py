"""Real cron source reads and helper execution, without a live cron daemon.

Private path-role injection is used only for these protected temporary inputs.
These component regressions do not replace full native host acceptance.
"""

from __future__ import annotations

import os
import pwd
import subprocess
import tempfile
from pathlib import Path

import pytest

from auto_coder.management import containment_route_manifest as routes
from auto_coder.management import containment_route_sources as sources

pytestmark = pytest.mark.usefixtures("_use_real_commands")


def _identity(path: Path) -> tuple[int, ...]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _scheduled(kind: str, command: str) -> str:
    if kind == "system-crontab":
        return "* * * * * root " + command + "\n"
    if kind == "user-crontab":
        return "* * * * * " + command + "\n"
    if kind == "anacrontab":
        return "1 5 fixture-job " + command + "\n"
    return "#!/bin/sh\n" + command + "\n"


def _catalog(path: Path, kind: str) -> tuple[routes.CronEntry, ...]:
    return routes._cron_catalog(roots=(path,), trusted_uid=os.geteuid(), inspect_local_inputs=True, extra_cues=("/srv/original-state",), source_formats=((path, kind),))


@pytest.mark.parametrize("kind", ("system-crontab", "user-crontab", "anacrontab", "shell"))
def test_existing_cron_path_helper_rewrite_cannot_retain_an_unrelated_catalog(tmp_path: Path, kind: str) -> None:
    tools = tmp_path / "tools"
    tools.mkdir(mode=0o700)
    helper = tools / "maintenance"
    helper.write_text("#!/bin/sh\nprintf '%s\\n' benign\n")
    helper.chmod(0o700)
    crontab = tmp_path / "installed-source"
    crontab.write_text("PATH=" + str(tools) + "\n" + _scheduled(kind, "maintenance"))
    crontab.chmod(0o600)
    directory, cron_identity, helper_inode = _identity(tools), _identity(crontab), helper.stat().st_ino

    def execute() -> str:
        return subprocess.run(("/bin/sh", "-c", "maintenance"), env={"PATH": str(tools)}, capture_output=True, text=True, check=True, timeout=5).stdout.strip()

    assert execute() == "benign"
    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        _catalog(crontab, kind)
    helper.write_text("#!/bin/sh\nprintf '%s\\n' /srv/original-state\n")
    assert _identity(tools) == directory
    assert _identity(crontab) == cron_identity
    assert helper.stat().st_ino == helper_inode
    assert execute() == "/srv/original-state"
    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        _catalog(crontab, kind)


@pytest.mark.parametrize("kind", ("system-crontab", "user-crontab", "anacrontab", "shell"))
def test_scheduled_absolute_packaged_command_keeps_its_bound_source_positive(tmp_path: Path, kind: str) -> None:
    crontab = tmp_path / "installed-source"
    crontab.write_text(_scheduled(kind, "/usr/bin/true"))
    crontab.chmod(0o600)
    first = _catalog(crontab, kind)
    assert len(first) == 1
    assert first[0].path == str(crontab)
    assert first[0].kind == "regular"
    assert first[0].command_sources == ()
    assert first[0].management_candidate is False
    assert _catalog(crontab, kind) == first
    assert subprocess.run(("/bin/sh", "-c", "/usr/bin/true"), check=False, timeout=5).returncode == 0
    crontab.write_text("# /srv/original-state\n" + crontab.read_text())
    changed = _catalog(crontab, kind)
    assert changed[0].sha256 != first[0].sha256
    assert changed[0].management_candidate is True


@pytest.mark.parametrize(
    "path,kind",
    (
        ("/etc/crontab", "system-crontab"),
        ("/etc/cron.d/root", "system-crontab"),
        ("/etc/anacrontab", "anacrontab"),
        ("/var/spool/cron/crontabs/root", "user-crontab"),
        ("/var/spool/cron/maintenance", "user-crontab"),
        ("/etc/cron.daily/fixture-job", "shell"),
        ("/var/spool/at/fixture-job", "shell"),
        ("/var/spool/atjobs/fixture-job", "shell"),
    ),
)
def test_installed_controller_paths_determine_cron_formats(path: str, kind: str) -> None:
    assert routes._cron_source_format(Path(path), routes._CRON_SOURCE_FORMATS) == kind


def test_custom_root_name_does_not_silently_select_a_system_crontab_role(tmp_path: Path) -> None:
    root = tmp_path / "cron.d"
    root.mkdir()
    crontab = root / "fixture-job"
    crontab.write_text("* * * * * root /usr/bin/true\n")
    with pytest.raises(routes.RouteManifestUnavailable, match="installed path format"):
        routes._cron_catalog(roots=(root,), trusted_uid=os.geteuid(), inspect_local_inputs=True)
    assert _catalog(crontab, "system-crontab")[0].command_sources == ()


def test_user_command_is_not_mistaken_for_system_cron_account_field(tmp_path: Path) -> None:
    crontab = tmp_path / "installed-source"
    crontab.write_text("* * * * * maintenance /usr/bin/true\n")
    # In a real system crontab this is the username field; in a user spool it
    # is the executable. Its spelling cannot select the source's format.
    assert _catalog(crontab, "system-crontab")[0].command_sources == ()
    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        _catalog(crontab, "user-crontab")


@pytest.mark.parametrize("kind,body", (("system-crontab", "@daily root /usr/bin/true\n"), ("user-crontab", "@daily /usr/bin/true\n"), ("anacrontab", "@monthly 5 fixture-job /usr/bin/true\n")))
def test_supported_macros_keep_exact_command_boundaries(tmp_path: Path, kind: str, body: str) -> None:
    crontab = tmp_path / "installed-source"
    crontab.write_text(body)
    assert _catalog(crontab, kind)[0].command_sources == ()
    crontab.write_text(body.replace("/usr/bin/true", "maintenance"))
    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        _catalog(crontab, kind)


@pytest.mark.parametrize("command", ("maintenance", "/usr/bin/true; maintenance", "/usr/bin/true && maintenance", "/usr/bin/nice maintenance", "/usr/bin/true%maintenance"))
def test_scheduled_commands_use_the_existing_bounded_shell_profile(tmp_path: Path, command: str) -> None:
    crontab = tmp_path / "installed-source"
    crontab.write_text(_scheduled("system-crontab", command))
    with pytest.raises((routes.RouteManifestUnavailable, sources.CommandSourceUnavailable), match="dispatch|outside"):
        _catalog(crontab, "system-crontab")


@pytest.mark.parametrize("shell", ("/bin/bash", "bash", "/usr/local/bin/custom-shell", "/bin/sh -l"))
def test_cron_shell_assignment_cannot_change_the_execution_context(tmp_path: Path, shell: str) -> None:
    crontab = tmp_path / "installed-source"
    crontab.write_text('SHELL="' + shell + '"\n' + _scheduled("system-crontab", "/usr/bin/true"))
    with pytest.raises(routes.RouteManifestUnavailable, match="outside"):
        _catalog(crontab, "system-crontab")


def test_literal_cron_environment_keeps_startup_assignment_refusal(tmp_path: Path) -> None:
    crontab = tmp_path / "installed-source"
    crontab.write_text('SHELL = "/bin/sh"\nMAILTO=""\n' + _scheduled("system-crontab", "/usr/bin/true"))
    assert _catalog(crontab, "system-crontab")[0].command_sources == ()
    crontab.write_text("BASH_ENV=/tmp/opaque-init\n" + crontab.read_text())
    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        _catalog(crontab, "system-crontab")


def test_absolute_local_cron_wrapper_is_bound_and_bare_nested_dispatch_refuses(tmp_path: Path) -> None:
    wrapper = tmp_path / "wrapper"
    wrapper.write_text("#!/bin/sh\nexec /usr/bin/true\n")
    crontab = tmp_path / "installed-source"
    crontab.write_text(_scheduled("system-crontab", str(wrapper)))
    first = _catalog(crontab, "system-crontab")
    assert {item.path for item in first[0].command_sources} == {str(wrapper)}
    wrapper.write_text("#!/bin/sh\nmaintenance\n")
    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        _catalog(crontab, "system-crontab")


def test_cron_symlink_uses_controller_path_role_and_retains_target_identity() -> None:
    with tempfile.TemporaryDirectory(prefix="ac-cron-dispatch-", dir=pwd.getpwuid(os.geteuid()).pw_dir) as directory:
        root = Path(directory)
        target = root / "target.sh"
        target.write_text("* * * * * root /usr/bin/true\n")
        target.chmod(0o600)
        link = root / "system-job"
        link.symlink_to(target)
        first = _catalog(link, "system-crontab")
        assert first[0].kind == "symlink"
        assert first[0].target_inode == target.stat().st_ino
        assert first[0].command_sources == ()
        target.write_text("* * * * * root maintenance\n")
        with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
            _catalog(link, "system-crontab")
