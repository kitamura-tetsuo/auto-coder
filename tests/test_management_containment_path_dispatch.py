"""Real PATH substitution and component-protocol refusal regressions.

Only the helper executions are real OS tests. The systemd and complete-consumer
tests supply component contracts and do not replace full-host acceptance.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from auto_coder.management import containment_observation as observation
from auto_coder.management import containment_route_manifest as routes
from auto_coder.management import containment_route_sources as sources
from tests.test_management_containment_composition import closed_components as closed_components

pytestmark = pytest.mark.usefixtures("_use_real_commands")


def _write_helper(path: Path, outcome: str) -> None:
    path.write_text("#!/bin/sh\nprintf '%s\\n' '" + outcome + "'\n")
    path.chmod(0o700)


def _directory_identity(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns


@pytest.mark.parametrize("launcher", ("shell", "combined-options", "env", "nice", "shell-file"))
def test_existing_path_helper_rewrite_changes_real_execution_but_never_proves_unchanged_sources(tmp_path: Path, launcher: str) -> None:
    tools = tmp_path / "tools"
    tools.mkdir(mode=0o700)
    helper = tools / "maintenance"
    _write_helper(helper, "benign")
    wrapper = tmp_path / "wrapper"
    wrapper.write_text("maintenance\n")
    commands = {
        "shell": ("/bin/sh", "-c", "maintenance"),
        "combined-options": ("/bin/sh", "-ec", "maintenance"),
        "env": ("/usr/bin/env", "maintenance"),
        "nice": ("/usr/bin/nice", "maintenance"),
        "shell-file": ("/bin/sh", str(wrapper)),
    }
    command = commands[launcher]
    text = " ".join(command) + "\nPATH=" + str(tools)

    def execute() -> str:
        return subprocess.run(command, env={"PATH": str(tools)}, capture_output=True, text=True, check=True, timeout=5).stdout.strip()

    assert execute() == "benign"
    directory = _directory_identity(tools)
    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        sources.observe_command_sources(text, uid=os.geteuid(), cues=("/srv/original-state",))

    _write_helper(helper, "/srv/original-state")
    assert _directory_identity(tools) == directory
    assert execute() == "/srv/original-state"
    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        sources.observe_command_sources(text, uid=os.geteuid(), cues=("/srv/original-state",))


@pytest.mark.parametrize(
    "command",
    (
        "/bin/sh -c maintenance",
        "/bin/sh -ec maintenance",
        "/bin/bash --noprofile -c maintenance",
        "/bin/bash -o errexit -c maintenance",
        '/bin/sh "-c" maintenance',
        "'/bin/sh' -c maintenance",
        "/usr/bin/env maintenance",
        "/usr/bin/env -i PATH=/opt/tools maintenance",
        "/usr/bin/env -S maintenance",
        "#!/usr/bin/env sh\nmaintenance\n",
    ),
)
def test_unresolved_shell_and_env_dispatch_have_no_unrelated_source_profile(command: str) -> None:
    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        sources.observe_command_sources(command, uid=os.geteuid(), cues=())


@pytest.mark.parametrize("line", ("maintenance", "exec maintenance", "exec printf", "command maintenance", "eval maintenance", "true && maintenance", "/usr/bin/true; maintenance"))
def test_local_shell_wrapper_cannot_hide_bare_dispatch(tmp_path: Path, line: str) -> None:
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text("#!/bin/sh\n" + line + "\n")
    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        sources.observe_command_sources(str(wrapper), uid=os.geteuid(), cues=())


def test_literal_shell_wrappers_keep_bound_sources_and_trusted_distro_programs(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.write_text("exec /usr/bin/true\n")
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text("#!/bin/sh\n. " + str(nested) + "\n")
    facts, candidate = sources.observe_command_sources(str(wrapper), uid=os.geteuid(), cues=())
    assert {item.path for item in facts} == {str(wrapper), str(nested)}
    assert candidate is False
    assert sources.observe_command_sources("/usr/bin/true", uid=os.geteuid(), cues=()) == ((), False)
    nested.write_text("maintenance\n")
    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        sources.observe_command_sources(str(wrapper), uid=os.geteuid(), cues=())


def _unit_properties(tools: Path) -> str:
    return (
        "Id=maintenance.service\nLoadState=loaded\nUnitFileState=enabled\n"
        "ActiveState=inactive\nSubState=dead\nMainPID=0\nNeedDaemonReload=no\n"
        "FragmentPath=/etc/systemd/system/maintenance.service\n"
        "ExecStart={ path=/bin/sh ; argv[]=/bin/sh -c maintenance ; ignore_errors=no ; "
        "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }\n"
        "Environment=PATH=" + str(tools) + "\n"
    )


@pytest.mark.parametrize("rewritten", (False, True))
def test_actual_route_refusal_reaches_producer_and_saved_success_consumer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, closed_components: observation.ProposedScope, rewritten: bool) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    helper = tools / "maintenance"
    _write_helper(helper, "benign")
    saved = json.loads(json.dumps(asdict(observation.observe(closed_components))))
    assert saved["status"] == observation.OBSERVED
    directory = _directory_identity(tools)
    if rewritten:
        _write_helper(helper, "/srv/original-state")
        assert _directory_identity(tools) == directory
    shown = _unit_properties(tools)
    unit_source = "[Service]\nExecStart=/bin/sh -c maintenance\nEnvironment=PATH=" + str(tools) + "\n"
    monkeypatch.setattr(routes, "_command", lambda *args, **kwargs: shown if "show" in args else unit_source)
    complete_route = observation.observe_approved_route_manifest
    calls = []

    def inspect_route(*args: object) -> routes.RouteManifestObservation:
        calls.append("real-source-inspection")
        routes._systemd_unit("maintenance.service", trusted_uid=tools.stat().st_uid, inspect_local_inputs=True)
        return complete_route(*args)

    monkeypatch.setattr(observation, "observe_approved_route_manifest", inspect_route)
    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        routes._systemd_unit("maintenance.service", trusted_uid=tools.stat().st_uid, inspect_local_inputs=True)
    for report in (observation.observe(closed_components), observation.verify(closed_components, saved)):
        assert report.status == "unavailable"
        assert report.route_manifest is None
        assert report.installed_bootstrap_source is not None
        assert "approved-route-manifest" in {item.authority for item in report.unavailable}
        assert "ordinary-route-closure" in {item.authority for item in report.unavailable}
    assert len(calls) == 2


def test_data_reference_later_used_as_shell_input_cannot_bypass_dispatch_validation(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    helper = tools / "maintenance"
    _write_helper(helper, "benign")
    wrapper, nested = tmp_path / "aa-wrapper", tmp_path / "zz-nested"
    wrapper.write_text(". " + str(nested) + "\n")
    nested.write_text("maintenance\n")
    text = "/bin/sh " + str(wrapper) + "\nEnvironmentFiles=" + str(nested) + " (ignore_errors=yes)\nPATH=" + str(tools)
    # The later lexical name is initially encountered only as data. The
    # earlier wrapper subsequently gives the same file a shell execution role.
    assert subprocess.run(("/bin/sh", str(wrapper)), env={"PATH": str(tools)}, capture_output=True, text=True, check=True, timeout=5).stdout.strip() == "benign"
    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        sources.observe_command_sources(text, uid=os.geteuid(), cues=("/srv/original-state",))
    directory = _directory_identity(tools)
    _write_helper(helper, "/srv/original-state")
    assert _directory_identity(tools) == directory
    assert subprocess.run(("/bin/sh", str(wrapper)), env={"PATH": str(tools)}, capture_output=True, text=True, check=True, timeout=5).stdout.strip() == "/srv/original-state"
    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        sources.observe_command_sources(text, uid=os.geteuid(), cues=("/srv/original-state",))


def test_late_shell_role_rechecks_the_original_cached_file_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wrapper, nested = tmp_path / "aa-wrapper", tmp_path / "zz-nested"
    wrapper.write_text(". " + str(nested) + "\n")
    nested.write_text("printf benign\n")
    text = "/bin/sh " + str(wrapper) + "\nEnvironmentFiles=" + str(nested) + " (ignore_errors=yes)"
    read = sources._protected_source
    paths = []

    def mutate_after_wrapper_read(path: Path, *, owners: frozenset[int]) -> tuple[bytes, os.stat_result]:
        paths.append(path)
        result = read(path, owners=owners)
        if path == wrapper:
            nested.write_text("exec /usr/bin/true\n")
        return result

    monkeypatch.setattr(sources, "_protected_source", mutate_after_wrapper_read)
    with pytest.raises(sources.CommandSourceUnavailable, match="changed during observation"):
        sources.observe_command_sources(text, uid=os.geteuid(), cues=())
    assert paths == [nested, wrapper, nested]


@pytest.mark.parametrize(
    "command",
    (
        "/usr/bin/nice maintenance",
        "/usr/bin/nice -n 5 maintenance",
        "/usr/bin/nice --adjustment=5 -- maintenance",
        "/usr/bin/nohup maintenance",
        "/usr/bin/setsid maintenance",
        "/usr/bin/timeout 5 maintenance",
        "/usr/bin/flock /tmp/fixture-lock maintenance",
        "/usr/bin/ionice maintenance",
        "/usr/bin/chrt 0 maintenance",
        "/usr/bin/taskset 1 maintenance",
        "/usr/bin/stdbuf -oL maintenance",
    ),
)
def test_common_launchers_cannot_delegate_to_unresolved_commands(command: str) -> None:
    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        sources.observe_command_sources(command, uid=os.geteuid(), cues=())


@pytest.mark.parametrize("command", ("/usr/bin/nice -n 5 /usr/bin/true", "/usr/bin/nohup /usr/bin/true", "/usr/bin/setsid /usr/bin/true", "/usr/bin/timeout 5 /usr/bin/true"))
def test_bounded_launcher_with_literal_packaged_target_preserves_trust_boundary(command: str) -> None:
    assert sources.observe_command_sources(command, uid=os.geteuid(), cues=()) == ((), False)


@pytest.mark.parametrize("command", ("/bin/sh -c '/usr/bin/true'", "/bin/sh -c /usr/bin/true", '/bin/dash -c "/usr/bin/true"'))
def test_inline_sh_supports_exactly_one_literal_absolute_target(command: str) -> None:
    assert sources.observe_command_sources(command, uid=os.geteuid(), cues=()) == ((), False)


def _exec_row(executable: str, arguments: str) -> str:
    return "{ path=" + executable + " ; argv[]=" + arguments + " ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"


def test_exec_readback_keeps_shell_body_separators_distinct_from_metadata() -> None:
    safe = _exec_row("/bin/sh", "/bin/sh -c /usr/bin/true")
    assert sources.observe_command_sources(safe, uid=os.geteuid(), cues=()) == ((), False)
    for body in ("/usr/bin/true; maintenance", "/usr/bin/true ; maintenance"):
        with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
            sources.observe_command_sources(_exec_row("/bin/sh", "/bin/sh -c " + body), uid=os.geteuid(), cues=())


@pytest.mark.parametrize("mode", ("-i", "-l", "-el", "--login", "-s"))
def test_shell_startup_or_stdin_modes_do_not_inherit_literal_script_support(tmp_path: Path, mode: str) -> None:
    wrapper = tmp_path / "wrapper"
    wrapper.write_text("exec /usr/bin/true\n")
    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        sources.observe_command_sources("/bin/sh " + mode + " " + str(wrapper), uid=os.geteuid(), cues=())


@pytest.mark.parametrize("argv0", ("-sh", "sh", "/bin/bash"))
def test_systemd_shell_argv0_override_is_not_a_literal_nonlogin_profile(tmp_path: Path, argv0: str) -> None:
    wrapper = tmp_path / "wrapper"
    wrapper.write_text("exec /usr/bin/true\n")
    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        sources.observe_command_sources(_exec_row("/bin/sh", argv0 + " " + str(wrapper)), uid=os.geteuid(), cues=())


@pytest.mark.parametrize("dispatcher", ("nice", "python"))
def test_effective_dispatcher_cannot_hide_behind_overridden_argv0(tmp_path: Path, dispatcher: str) -> None:
    helper = tmp_path / "maintenance"
    _write_helper(helper, "benign")
    wrapper = tmp_path / "extensionless-wrapper"
    wrapper.write_text("print('benign')\n")
    executable, command = ("/usr/bin/nice", ("renamed-launcher", "maintenance")) if dispatcher == "nice" else (sys.executable, ("renamed-interpreter", "-B", str(wrapper)))
    # systemd's @ prefix changes argv[0], not the actual executed program.
    result = subprocess.run(command, executable=executable, env={"PATH": str(tmp_path)}, capture_output=True, text=True, check=True, timeout=5)
    assert result.stdout.strip() == "benign"
    with pytest.raises(sources.CommandSourceUnavailable, match="overridden dispatcher"):
        sources.observe_command_sources(_exec_row(executable, " ".join(command)), uid=os.geteuid(), cues=())


def test_bash_ambient_startup_cannot_make_a_builtin_hide_a_path_helper(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    helper = tools / "true"
    _write_helper(helper, "benign")
    wrapper, startup = tmp_path / "wrapper", tmp_path / "bash-init"
    wrapper.write_text("true\n")
    startup.write_text("enable -n true\n")
    command = ("/bin/bash", str(wrapper))

    def execute() -> str:
        return subprocess.run(command, env={"PATH": str(tools), "BASH_ENV": str(startup)}, capture_output=True, text=True, check=True, timeout=5).stdout.strip()

    # BASH_ENV may be inherited, so it is deliberately absent from the
    # selected per-unit input text in this regression.
    selected = " ".join(command)
    assert execute() == "benign"
    directory = _directory_identity(tools)
    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        sources.observe_command_sources(selected, uid=os.geteuid(), cues=())
    _write_helper(helper, "/srv/original-state")
    assert _directory_identity(tools) == directory
    assert execute() == "/srv/original-state"
    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        sources.observe_command_sources(selected, uid=os.geteuid(), cues=())


def test_actual_environment_file_startup_assignment_is_not_plain_unchanging_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    helper = tools / "true"
    _write_helper(helper, "benign")
    wrapper, startup, environment = tmp_path / "wrapper", tmp_path / "bash-init", tmp_path / "service-environment"
    wrapper.write_text("true\n")
    startup.write_text("enable -n true\n")
    environment.write_text("BASH_ENV=" + str(startup) + "\nPATH=" + str(tools) + "\n")
    unchanged = environment.read_bytes(), environment.stat()
    # These literal assignments need no environment-file escape processing.
    actual_environment = dict(line.split("=", 1) for line in environment.read_text().splitlines())

    def execute() -> str:
        return subprocess.run(("/bin/bash", str(wrapper)), env=actual_environment, capture_output=True, text=True, check=True, timeout=5).stdout.strip()

    # Use a safe sh command so refusal must come from the environment file,
    # rather than being hidden by the separate unsupported-Bash predicate.
    shown = _unit_properties(tools).replace("/bin/sh -c maintenance", "/bin/sh -c /usr/bin/true")
    shown += "EnvironmentFiles=" + str(environment) + " (ignore_errors=yes)\n"
    monkeypatch.setattr(routes, "_command", lambda *args, **kwargs: shown if "show" in args else "[Service]\nExecStart=/bin/sh -c '/usr/bin/true'\n")
    assert execute() == "benign"
    directory = _directory_identity(tools)
    with pytest.raises(sources.CommandSourceUnavailable, match="Bash startup environment"):
        routes._systemd_unit("maintenance.service", trusted_uid=os.geteuid(), inspect_local_inputs=True)
    _write_helper(helper, "/srv/original-state")
    assert _directory_identity(tools) == directory
    assert (environment.read_bytes(), environment.stat()) == unchanged
    assert execute() == "/srv/original-state"
    with pytest.raises(sources.CommandSourceUnavailable, match="Bash startup environment"):
        routes._systemd_unit("maintenance.service", trusted_uid=os.geteuid(), inspect_local_inputs=True)


def test_real_local_python_sibling_import_is_not_an_unchanged_source_profile(tmp_path: Path) -> None:
    wrapper, helper = tmp_path / "wrapper.py", tmp_path / "helper.py"
    wrapper.write_text("import helper\nprint(helper.VALUE)\n")
    helper.write_text("VALUE = 'benign'\n")
    command = (sys.executable, "-B", str(wrapper))

    def execute() -> str:
        return subprocess.run(command, capture_output=True, text=True, check=True, timeout=5).stdout.strip()

    assert execute() == "benign"
    directory, helper_inode, wrapper_bytes = _directory_identity(tmp_path), helper.stat().st_ino, wrapper.read_bytes()
    with pytest.raises(sources.CommandSourceUnavailable, match="unprofiled local interpreter"):
        sources.observe_command_sources(" ".join(command), uid=os.geteuid(), cues=())
    helper.write_text("VALUE = '/srv/original-state'\n")
    assert _directory_identity(tmp_path) == directory
    assert helper.stat().st_ino == helper_inode
    assert wrapper.read_bytes() == wrapper_bytes
    assert execute() == "/srv/original-state"
    assert not (tmp_path / "__pycache__").exists()
    with pytest.raises(sources.CommandSourceUnavailable, match="unprofiled local interpreter"):
        sources.observe_command_sources(" ".join(command), uid=os.geteuid(), cues=())


@pytest.mark.parametrize("interpreter,suffix", (("/usr/bin/python3", ".py"), ("/usr/bin/node", ".js"), ("/usr/bin/perl", ".pl"), ("/usr/bin/ruby", ".rb")))
def test_unprofiled_local_interpreter_operands_are_not_dependency_graph_proofs(tmp_path: Path, interpreter: str, suffix: str) -> None:
    wrapper = tmp_path / ("wrapper" + suffix)
    wrapper.write_text("# Local source with an unbound dependency graph.\n")
    with pytest.raises(sources.CommandSourceUnavailable, match="unprofiled local interpreter"):
        sources.observe_command_sources(interpreter + " " + str(wrapper), uid=os.geteuid(), cues=())
    with pytest.raises(sources.CommandSourceUnavailable, match="unprofiled local interpreter"):
        sources.observe_command_sources(str(wrapper), uid=os.geteuid(), cues=())


@pytest.mark.parametrize("command", ("/usr/bin/python3 -c pass", "/usr/bin/node -e process.exit()", "/usr/bin/perl -e exit", "/usr/bin/ruby -e exit"))
def test_unprofiled_interpreter_inline_eval_is_unavailable(command: str) -> None:
    with pytest.raises(sources.CommandSourceUnavailable, match="unprofiled local interpreter"):
        sources.observe_command_sources(command, uid=os.geteuid(), cues=())


def test_trusted_distro_script_operand_stays_within_its_deliberate_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    packaged = tmp_path / "packaged.py"
    packaged.write_text("import installed_distro_dependency\n")
    trusted = sources._trusted_program
    monkeypatch.setattr(sources, "_trusted_program", lambda path: path == packaged or trusted(path))
    assert sources.observe_command_sources("/usr/bin/python3 -B " + str(packaged), uid=os.geteuid(), cues=()) == ((), False)


@pytest.mark.parametrize("packaged_target", (False, True))
def test_absolute_executable_alias_to_shell_refuses_before_and_after_helper_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closed_components: observation.ProposedScope,
    packaged_target: bool,
) -> None:
    """A renamed, resolved shell must not hide unbound PATH dispatch."""
    tools = tmp_path / "tools"
    tools.mkdir(mode=0o700)
    helper = tools / "maintenance"
    _write_helper(helper, "benign")
    if packaged_target:
        actual_shell = Path("/bin/sh").resolve(strict=True)
    else:
        actual_shell = tmp_path / "sh"
        shutil.copyfile("/bin/sh", actual_shell)
        actual_shell.chmod(0o700)
    launcher = tmp_path / "launcher"
    launcher.symlink_to(actual_shell)
    command = (str(launcher), "-c", "maintenance")
    shown = _unit_properties(tools).replace("path=/bin/sh", "path=" + str(launcher)).replace("argv[]=/bin/sh", "argv[]=" + str(launcher))
    unit_source = "[Service]\nExecStart=" + " ".join(command) + "\nEnvironment=PATH=" + str(tools) + "\n"
    monkeypatch.setattr(routes, "_command", lambda *args, **kwargs: shown if "show" in args else unit_source)
    saved = json.loads(json.dumps(asdict(observation.observe(closed_components))))
    assert saved["status"] == observation.OBSERVED
    inspect_route = observation.observe_approved_route_manifest

    def current_route(*args: object) -> routes.RouteManifestObservation:
        routes._systemd_unit("maintenance.service", trusted_uid=tools.stat().st_uid, inspect_local_inputs=True)
        return inspect_route(*args)

    monkeypatch.setattr(observation, "observe_approved_route_manifest", current_route)
    directory = _directory_identity(tools)
    for expected in ("benign", "/srv/original-state"):
        assert subprocess.run(command, env={"PATH": str(tools)}, capture_output=True, text=True, check=True, timeout=5).stdout.strip() == expected
        with pytest.raises(sources.CommandSourceUnavailable, match="resolved executable alias.*dispatch"):
            sources.observe_command_sources(
                _exec_row(str(launcher), " ".join(command)),
                uid=tools.stat().st_uid,
                cues=("/srv/original-state",),
            )
        with pytest.raises(sources.CommandSourceUnavailable, match="resolved executable alias.*dispatch"):
            routes._systemd_unit("maintenance.service", trusted_uid=tools.stat().st_uid, inspect_local_inputs=True)
        for report in (observation.observe(closed_components), observation.verify(closed_components, saved)):
            assert report.status == "unavailable"
            assert report.route_manifest is None
            assert "approved-route-manifest" in {item.authority for item in report.unavailable}
        if expected == "benign":
            _write_helper(helper, "/srv/original-state")
            assert _directory_identity(tools) == directory
    assert sources.observe_command_sources(_exec_row("/usr/bin/true", "/usr/bin/true"), uid=tools.stat().st_uid, cues=()) == ((), False)
