"""Actual executable aliases cannot inherit an unrelated data-file classification."""

from __future__ import annotations

import hashlib
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
REFUSAL = "absolute executable alias or dispatcher path is outside the supported profile"
ALIAS_REFUSAL = "resolved executable alias uses an unsupported dispatcher profile"


def _identity(path: Path) -> tuple[int, ...]:
    info = path.lstat()
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _helper(path: Path, value: str) -> None:
    path.write_text("#!/bin/sh\nprintf '%s\\n' '" + value + "'\n")
    path.chmod(0o700)


def _alias_case(root: Path, *, copied: bool = True) -> tuple[Path, Path, Path, Path]:
    tools, package = root / "tools", root / "package"
    tools.mkdir(mode=0o700)
    package.mkdir(mode=0o700)
    helper = tools / "maintenance"
    _helper(helper, "benign-before")
    target = package / "sh" if copied else Path("/bin/sh").resolve(strict=True)
    if copied:
        shutil.copyfile(Path("/bin/sh").resolve(strict=True), target)
        target.chmod(0o700)
    alias = root / "launcher"
    alias.symlink_to(target)
    return alias, target, tools, helper


def _exec_row(executable: str, arguments: str) -> str:
    return "{ path=" + executable + " ; argv[]=" + arguments + " ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"


def _show(executable: str, arguments: str, *, extra: str = "") -> str:
    return "Id=alias-probe.service\nLoadState=loaded\nUnitFileState=enabled\n" "ActiveState=inactive\nSubState=dead\nMainPID=0\nNeedDaemonReload=no\n" "FragmentPath=/etc/systemd/system/alias-probe.service\nExecStart=" + _exec_row(executable, arguments) + "\n" + extra


def _unit(monkeypatch: pytest.MonkeyPatch, executable: str, arguments: str, *, extra: str = "") -> routes.SystemdRouteFact:
    shown = _show(executable, arguments, extra=extra)
    unit_source = "[Service]\nExecStart=" + arguments + "\n"
    monkeypatch.setattr(routes, "_command", lambda *args, **kwargs: shown if "show" in args else unit_source)
    return routes._systemd_unit("alias-probe.service", trusted_uid=os.geteuid(), inspect_local_inputs=True)


def _execute(alias: Path, tools: Path) -> str:
    return subprocess.run((str(alias), "-c", "maintenance"), env={"PATH": str(tools)}, check=True, capture_output=True, text=True, timeout=5).stdout.strip()


@pytest.mark.parametrize("copied", (False, True))
def test_real_absolute_shell_alias_refuses_both_unchanged_unit_phases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, copied: bool) -> None:
    alias, target, tools, helper = _alias_case(tmp_path, copied=copied)
    unchanged = {path: _identity(path) for path in (alias, target, tools, alias.parent, target.parent)}
    target_digest = hashlib.sha256(target.read_bytes()).hexdigest()
    helper_inode = helper.stat().st_ino
    arguments, extra = str(alias) + " -c maintenance", "Environment=PATH=" + str(tools) + "\n"
    shown = _show(str(alias), arguments, extra=extra)

    assert _execute(alias, tools) == "benign-before"
    for expected in ("benign-before", "/srv/original-state"):
        if expected != "benign-before":
            _helper(helper, expected)
        assert _execute(alias, tools) == expected
        assert {path: _identity(path) for path in unchanged} == unchanged
        assert hashlib.sha256(target.read_bytes()).hexdigest() == target_digest
        assert helper.stat().st_ino == helper_inode
        assert _show(str(alias), arguments, extra=extra) == shown
        # This uses the actual systemctl value form, without an ExecStart=
        # prefix inside the source reader's effective command payload.
        with pytest.raises(sources.CommandSourceUnavailable, match=ALIAS_REFUSAL):
            _unit(monkeypatch, str(alias), arguments, extra=extra)


@pytest.mark.parametrize("rewritten", (False, True))
def test_real_unit_alias_refusal_reaches_producer_and_saved_success_verifier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, closed_components: observation.ProposedScope, capsys: pytest.CaptureFixture[str], rewritten: bool) -> None:
    alias, _, tools, helper = _alias_case(tmp_path)
    scope_path, previous_path = tmp_path / "scope.json", tmp_path / "previous.json"
    scope_path.write_text(json.dumps(asdict(closed_components)))

    def invoke(command: str):
        arguments = ["containment-observation", command, "--scope", str(scope_path)]
        if command == "verify":
            arguments += ["--previous", str(previous_path)]
        monkeypatch.setattr(sys, "argv", arguments)
        code = observation.main()
        output = capsys.readouterr()
        assert output.err == ""
        return code, json.loads(output.out)

    code, saved = invoke("observe")
    assert code == 0 and saved["status"] == observation.OBSERVED
    previous_path.write_text(json.dumps(saved))
    code, baseline_verifier = invoke("verify")
    assert code == 0 and baseline_verifier["status"] == observation.OBSERVED
    directory = _identity(tools)
    if rewritten:
        _helper(helper, "/srv/original-state")
    assert _identity(tools) == directory
    assert _execute(alias, tools) == ("/srv/original-state" if rewritten else "benign-before")
    shown = _show(str(alias), str(alias) + " -c maintenance", extra="Environment=PATH=" + str(tools) + "\n")
    monkeypatch.setattr(routes, "_command", lambda *args, **kwargs: shown if "show" in args else "[Service]\nExecStart=" + str(alias) + " -c maintenance\n")
    complete_route = observation.observe_approved_route_manifest
    calls = []

    def inspect_route(*args: object) -> routes.RouteManifestObservation:
        calls.append("actual-source-boundary")
        routes._systemd_unit("alias-probe.service", trusted_uid=os.geteuid(), inspect_local_inputs=True)
        return complete_route(*args)

    monkeypatch.setattr(observation, "observe_approved_route_manifest", inspect_route)
    for command in ("observe", "verify"):
        code, report = invoke(command)
        assert code == 2 and report["status"] == "unavailable"
        assert report["route_manifest"] is None
        assert report["installed_bootstrap_source"] is not None
        assert {"approved-route-manifest", "ordinary-route-closure"} <= {item["authority"] for item in report["unavailable"]}
    assert calls == ["actual-source-boundary", "actual-source-boundary"]


@pytest.mark.parametrize("argv", (("/usr/bin/true",), ("/bin/sh", "-c", "/usr/bin/true")))
def test_real_direct_packaged_controls_remain_supported(monkeypatch: pytest.MonkeyPatch, argv: tuple[str, ...]) -> None:
    assert subprocess.run(argv, check=True, capture_output=True, timeout=5).returncode == 0
    fact = _unit(monkeypatch, argv[0], " ".join(argv))
    assert fact.command_sources == ()
    assert not fact.management_candidate


@pytest.mark.parametrize("target", ("/bin/sh", "/bin/bash", "/usr/bin/nice", sys.executable))
def test_absolute_alias_cannot_hide_resolved_dispatcher_family(tmp_path: Path, target: str) -> None:
    alias = tmp_path / "launcher"
    alias.symlink_to(Path(target).resolve(strict=True))
    with pytest.raises(sources.CommandSourceUnavailable, match=ALIAS_REFUSAL):
        sources.observe_command_sources(_exec_row(str(alias), str(alias) + " -c maintenance"), uid=os.geteuid(), cues=())


def test_noncanonical_direct_shell_path_and_opaque_local_elf_refuse(tmp_path: Path) -> None:
    for name in ("sh", "opaque-launcher"):
        path = tmp_path / name
        shutil.copyfile(Path("/bin/sh").resolve(strict=True), path)
        path.chmod(0o700)
        assert subprocess.run((str(path), "-c", "/usr/bin/true"), check=True, timeout=5).returncode == 0
        with pytest.raises(sources.CommandSourceUnavailable, match=REFUSAL):
            sources.observe_command_sources(_exec_row(str(path), str(path) + " -c /usr/bin/true"), uid=os.geteuid(), cues=())


@pytest.mark.parametrize("prefix", ("/usr/bin/nice ", "/usr/bin/timeout 5 /usr/bin/nice ", "/bin/sh -c "))
def test_supported_literal_delegation_retains_the_target_executable_role(tmp_path: Path, prefix: str) -> None:
    alias = tmp_path / "launcher"
    alias.symlink_to(Path("/bin/sh").resolve(strict=True))
    command = prefix + str(alias)
    with pytest.raises(sources.CommandSourceUnavailable, match=ALIAS_REFUSAL):
        sources.observe_command_sources(command, uid=os.geteuid(), cues=())


@pytest.mark.parametrize("prefix", ("exec ", "exec\t", "exec \t "))
@pytest.mark.parametrize("copied", (False, True))
def test_cached_native_data_alias_is_rechecked_when_wrapper_executes_it(tmp_path: Path, copied: bool, prefix: str) -> None:
    package = tmp_path / "package"
    package.mkdir()
    target = package / "sh" if copied else Path("/bin/sh").resolve(strict=True)
    if copied:
        shutil.copyfile(Path("/bin/sh").resolve(strict=True), target)
        target.chmod(0o700)
    alias, wrapper = tmp_path / "zz-alias", tmp_path / "aa-wrapper"
    alias.symlink_to(target)
    wrapper.write_text("#!/bin/sh\n" + prefix + str(alias) + " -c maintenance\n")
    # The later lexical path is observed as native data before the earlier
    # wrapper gives the same cached file/trusted-link an executable role.
    payload = _exec_row("/bin/sh", "/bin/sh " + str(wrapper)) + "\n" + str(alias)
    with pytest.raises(sources.CommandSourceUnavailable, match=ALIAS_REFUSAL):
        sources.observe_command_sources(payload, uid=os.geteuid(), cues=(), systemd_readback=True)


def test_shebang_alias_has_no_supported_local_executable_profile(tmp_path: Path) -> None:
    alias = tmp_path / "interpreter"
    alias.symlink_to(Path("/bin/sh").resolve(strict=True))
    script = tmp_path / "job"
    script.write_text("#!" + str(alias) + "\nmaintenance\n")
    script.chmod(0o700)
    with pytest.raises(sources.CommandSourceUnavailable, match=REFUSAL):
        sources.observe_command_sources(str(script), uid=os.geteuid(), cues=())


def test_native_data_properties_do_not_manufacture_executable_roles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    alias = tmp_path / "data-alias"
    alias.symlink_to(Path("/bin/sh").resolve(strict=True))
    image = tmp_path / "data.img"
    image.write_bytes(b"\x7fELF\x00protected data")
    extra = "Environment=EXAMPLE=/bin/sh -c " + str(alias) + "\nWorkingDirectory=" + str(tmp_path) + "\nRootImage=" + str(image) + "\n"
    fact = _unit(monkeypatch, "/usr/bin/true", "/usr/bin/true", extra=extra)
    by_path = {item.path: item for item in fact.command_sources}
    assert by_path[str(alias)].kind == "trusted-link"
    assert by_path[str(image)].sha256 == hashlib.sha256(image.read_bytes()).hexdigest()
    assert by_path[str(tmp_path)].kind == "directory"


@pytest.mark.parametrize("canonical,target", (("/bin/sh", "/bin/bash"), ("/usr/bin/python3", "/usr/bin/true"), ("/usr/bin/nice", "/usr/bin/true")))
def test_canonical_spelling_does_not_hide_incompatible_resolved_family(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, canonical: str, target: str) -> None:
    resolve = Path.resolve
    actual_target = Path(target).resolve(strict=True)
    monkeypatch.setattr(Path, "resolve", lambda self, *args, **kwargs: actual_target if self == Path(canonical) else resolve(self, *args, **kwargs))
    module = tmp_path / "reviewed.py"
    module.write_text("VALUE = 1\n")
    arguments = canonical + (" -I -B -m reviewed.module" if "python" in canonical else " -c /usr/bin/true" if canonical.endswith("/sh") else " /usr/bin/true")
    with pytest.raises(sources.CommandSourceUnavailable, match=REFUSAL):
        sources.observe_command_sources(_exec_row(canonical, arguments), uid=os.geteuid(), cues=(), known_modules=(("reviewed.module", str(module)),))


def test_reviewed_python_allowance_is_per_occurrence_including_cached_trusted_link(tmp_path: Path) -> None:
    directory = tmp_path / "zz-interpreter"
    directory.mkdir()
    python = directory / "python3"
    python.symlink_to(Path("/usr/bin/python3").resolve(strict=True))
    module = tmp_path / "reviewed.py"
    module.write_text("VALUE = 1\n")
    known = (("reviewed.module", str(module)),)
    safe = _exec_row(str(python), str(python) + " -I -B -m reviewed.module")
    facts, candidate = sources.observe_command_sources(safe, uid=os.geteuid(), cues=(), known_modules=known)
    assert not candidate
    assert {item.path for item in facts} == {str(python), str(module)}
    assert next(item for item in facts if item.path == str(python)).kind == "trusted-link"
    different = _exec_row(str(python), str(python) + " -B /usr/lib/os-release")
    for commands in (safe + "\n" + different, different + "\n" + safe):
        with pytest.raises(sources.CommandSourceUnavailable, match=REFUSAL):
            sources.observe_command_sources(commands, uid=os.geteuid(), cues=(), known_modules=known)
    wrapper = tmp_path / "aa-wrapper"
    wrapper.write_text("#!/bin/sh\nexec " + str(python) + " -B /usr/lib/os-release\n")
    with pytest.raises(sources.CommandSourceUnavailable, match=REFUSAL):
        sources.observe_command_sources(safe + "\n" + _exec_row("/bin/sh", "/bin/sh " + str(wrapper)), uid=os.geteuid(), cues=(), known_modules=known)


@pytest.mark.parametrize("content", ("shell-elf", "shell-source"))
def test_local_program_named_python_cannot_inherit_reviewed_module_semantics(tmp_path: Path, content: str) -> None:
    python = tmp_path / "python3"
    if content == "shell-elf":
        shutil.copyfile(Path("/bin/sh").resolve(strict=True), python)
    else:
        python.write_text("#!/bin/sh\nexec /usr/bin/true\n")
    python.chmod(0o700)
    module = tmp_path / "reviewed.py"
    module.write_text("VALUE = 1\n")
    command = _exec_row(str(python), str(python) + " -I -B -m reviewed.module")
    with pytest.raises(sources.CommandSourceUnavailable, match=REFUSAL):
        sources.observe_command_sources(command, uid=os.geteuid(), cues=(), known_modules=(("reviewed.module", str(module)),))


def test_real_observer_interpreter_link_proves_identity_without_waiving_source_limits(tmp_path: Path) -> None:
    python = tmp_path / "python3"
    python.symlink_to(Path(sys.executable).resolve(strict=True))
    target = python.resolve(strict=True)
    info = target.stat()
    # This is the real kernel-identity subpredicate. It is deliberately not
    # a claim that this host's runtime satisfies owner and source bounds.
    assert sources._matches_observer_interpreter(target, info)
    if info.st_mode & 0o022 or info.st_size > sources._MAX_SOURCE_BYTES:
        module = tmp_path / "reviewed.py"
        module.write_text("VALUE = 1\n")
        command = _exec_row(str(python), str(python) + " -I -B -m reviewed.module")
        with pytest.raises(sources.CommandSourceUnavailable):
            sources.observe_command_sources(command, uid=os.geteuid(), cues=(), known_modules=(("reviewed.module", str(module)),))


@pytest.mark.parametrize("failure", ("changed-kernel", "unreadable-kernel"))
def test_observer_interpreter_identity_requires_coherent_kernel_readback(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    from types import SimpleNamespace

    target = Path(sys.executable).resolve(strict=True)
    info = target.stat()
    original_stat = Path.stat
    count = 0

    def read_stat(path: Path, *args: object, **kwargs: object):
        nonlocal count
        if path == Path("/proc/self/exe"):
            count += 1
            if count == 2:
                if failure == "unreadable-kernel":
                    raise OSError("independent kernel readback unavailable")
                fields = {name: getattr(info, name) for name in ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_rdev", "st_size", "st_mtime_ns", "st_ctime_ns")}
                fields["st_ino"] += 1
                return SimpleNamespace(**fields)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", read_stat)
    assert not sources._matches_observer_interpreter(target, info)
    assert count == 2
