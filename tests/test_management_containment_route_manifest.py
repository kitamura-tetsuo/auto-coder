"""Protocol and hostile interleaving tests for protected ordinary-route approval.

These do not stand in for the Issue #2827 real full-profile OS/Docker fixture.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import subprocess
import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_coder.management import containment_route_manifest as routes
from auto_coder.management.containment_enrollment import EnrollmentUnavailable


def test_systemd_unit_names_allow_safe_systemd_hex_escapes_without_path_selection() -> None:
    assert routes._UNIT_NAME.fullmatch(r"systemd-fsck@dev-disk-by\x2duuid-BC0B\x2dD22A.service")
    assert routes._UNIT_NAME.fullmatch("apport-autoreport.path")
    assert routes._UNIT_NAME.fullmatch("multi-user.target")
    assert routes._UNIT_NAME.fullmatch("proc-sys-fs-binfmt_misc.automount")
    assert not routes._UNIT_NAME.fullmatch("../../untrusted.service")
    assert not routes._UNIT_NAME.fullmatch("untrusted.service;systemctl start manager")


def test_systemd_timer_without_service_main_pid_is_fingerprinted(monkeypatch: pytest.MonkeyPatch) -> None:
    shown = "Id=apt-daily.timer\nLoadState=loaded\nUnitFileState=enabled\nActiveState=active\nSubState=waiting\nFragmentPath=/usr/lib/systemd/system/apt-daily.timer\nNeedDaemonReload=no\n"
    monkeypatch.setattr(routes, "_command", lambda *args, **kwargs: shown if args[1] == "show" else "[Timer]\nOnCalendar=daily\n")
    fact = routes._systemd_unit("apt-daily.timer")
    assert fact.main_pid == 0
    assert fact.unit_file_state == "enabled"
    assert fact.inhibited is False


@pytest.mark.parametrize("unit", ("apport-autoreport.path", "multi-user.target", "etc-fstab.mount", "proc-sys-fs-binfmt_misc.automount", "dev-zram0.swap"))
def test_other_service_activators_are_fingerprinted_without_main_pid(monkeypatch: pytest.MonkeyPatch, unit: str) -> None:
    shown = "Id=" + unit + "\nLoadState=loaded\nUnitFileState=enabled\nActiveState=active\nSubState=running\nFragmentPath=/etc/systemd/system/" + unit + "\nNeedDaemonReload=no\n"
    monkeypatch.setattr(routes, "_command", lambda *args, **kwargs: shown if args[1] == "show" else "[Unit]\nWants=management.service\n")
    fact = routes._systemd_unit(unit)
    assert fact.unit == unit
    assert fact.main_pid == 0
    assert fact.inhibited is False


def test_root_mount_unit_name_is_never_misread_as_a_systemctl_option(monkeypatch: pytest.MonkeyPatch) -> None:
    name = "-.mount"
    shown = "Id=-.mount\nLoadState=loaded\nUnitFileState=generated\nActiveState=active\nSubState=mounted\nFragmentPath=/run/systemd/generator/-.mount\nNeedDaemonReload=no\n"
    commands: list[tuple[str, ...]] = []

    def command(*args: str, **kwargs: object) -> str:
        commands.append(args)
        return shown if args[1] == "show" else "[Mount]\nWhere=/\n"

    monkeypatch.setattr(routes, "_command", command)
    assert routes._systemd_unit(name).unit == name
    assert all(args[-2:] == ("--", name) for args in commands)


def test_systemd_catalog_includes_indirect_service_activation_types(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[tuple[str, ...]] = []

    def command(*args: str, **kwargs: object) -> str:
        commands.append(args)
        field = "unit_file" if args[1] == "list-unit-files" else "unit"
        return json.dumps([{field: "apport-autoreport.path"}, {field: "multi-user.target"}, {field: "management.service"}])

    monkeypatch.setattr(routes, "_command", command)
    assert routes._systemd_catalog() == ("apport-autoreport.path", "management.service", "multi-user.target")
    assert len(commands) == 2
    for args in commands:
        assert set(args[3:11]) == {"--type=" + unit_type for unit_type in routes._ACTIVATION_UNIT_TYPES}


def test_systemd_template_file_is_read_without_pretending_to_observe_an_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "_command", lambda *args, **kwargs: "[Service]\nExecStart=/bin/true\n")
    monkeypatch.setattr(routes.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "static\n", ""))
    fact = routes._systemd_unit("alsa-card-wait@.service")
    assert fact.unit_file_state == "static"
    assert fact.active_state == "inactive"
    assert fact.inhibited is False


def test_systemd_alias_is_bound_to_resolved_unit_and_enablement(monkeypatch: pytest.MonkeyPatch) -> None:
    shown = "Id=chrony.service\nLoadState=loaded\nUnitFileState=enabled\nActiveState=active\nSubState=running\nFragmentPath=/usr/lib/systemd/system/chrony.service\nNeedDaemonReload=no\nMainPID=1234\n"
    monkeypatch.setattr(routes, "_command", lambda *args, **kwargs: shown if args[1] == "show" else "[Service]\nExecStart=/usr/sbin/chronyd\n")
    monkeypatch.setattr(routes, "_unit_enablement", lambda unit: "alias")
    fact = routes._systemd_unit("chronyd.service")
    assert fact.main_pid == 1234
    assert fact.inhibited is False
    monkeypatch.setattr(routes, "_unit_enablement", lambda unit: "enabled")
    with pytest.raises(routes.RouteManifestUnavailable, match="redirected"):
        routes._systemd_unit("chronyd.service")


def test_systemd_multi_exec_start_post_preserves_all_command_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    shown = "Id=dmesg.service\nLoadState=loaded\nUnitFileState=enabled\nActiveState=inactive\nSubState=dead\nFragmentPath=/usr/lib/systemd/system/dmesg.service\nNeedDaemonReload=no\nMainPID=0\nExecStartPost=/usr/bin/chgrp\nExecStartPost=/usr/bin/chmod\n"
    monkeypatch.setattr(routes, "_command", lambda *args, **kwargs: shown if args[1] == "show" else "[Service]\nType=oneshot\n")
    first = routes._systemd_unit("dmesg.service")
    monkeypatch.setattr(routes, "_command", lambda *args, **kwargs: shown.replace("/usr/bin/chmod", "/usr/bin/false") if args[1] == "show" else "[Service]\nType=oneshot\n")
    assert routes._systemd_unit("dmesg.service").sha256 != first.sha256


def test_unordered_systemd_dependency_sets_are_canonical_without_losing_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    shown = (
        "Id=bolt.service\nLoadState=loaded\nUnitFileState=enabled\nActiveState=inactive\n"
        "SubState=dead\nFragmentPath=/usr/lib/systemd/system/bolt.service\n"
        "NeedDaemonReload=no\nMainPID=0\n"
        "Requires=sysinit.target system.slice dbus.socket -.mount\n"
        "Wants=network.target dbus.socket\n"
        "TriggeredBy=bolt.socket dbus.socket\n"
    )
    current = [shown]
    monkeypatch.setattr(routes, "_command", lambda *args, **kwargs: current[0] if args[1] == "show" else "[Service]\nExecStart=/usr/bin/boltd\n")
    first = routes._systemd_unit("bolt.service")
    current[0] = shown.replace("sysinit.target system.slice dbus.socket -.mount", "system.slice dbus.socket -.mount sysinit.target").replace("network.target dbus.socket", "dbus.socket network.target").replace("bolt.socket dbus.socket", "dbus.socket bolt.socket")
    assert routes._systemd_unit("bolt.service").sha256 == first.sha256
    current[0] = current[0].replace("dbus.socket -.mount sysinit.target", "other.socket -.mount sysinit.target")
    assert routes._systemd_unit("bolt.service").sha256 != first.sha256


def test_removed_loaded_systemd_unit_is_only_an_unrelated_current_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    shown = "Id=auto-cpufreq.service\nLoadState=not-found\nUnitFileState=\nActiveState=inactive\nSubState=dead\nFragmentPath=\nNeedDaemonReload=no\nMainPID=0\n"

    def command(*args: str, **kwargs: object) -> str:
        if args[1] == "show":
            return shown
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(routes, "_command", command)
    fact = routes._systemd_unit("auto-cpufreq.service")
    assert fact.unit_file_state == ""
    assert fact.inhibited is False


class _closed_routes:
    def __init__(self, monkeypatch: pytest.MonkeyPatch):
        self.monkeypatch = monkeypatch
        self.directory = tempfile.TemporaryDirectory(prefix="ac-route-manifest-", dir=pwd.getpwuid(os.geteuid()).pw_dir)
        self.base = Path(self.directory.name)
        self.root = self.base / "approved"
        self.root.mkdir(mode=0o700)
        self.cron_roots = (self.base / "cron.d", self.base / "at")
        self.cron_roots[0].mkdir()
        self.cron_roots[1].mkdir()
        (self.cron_roots[0] / "system-cron").write_text("# unrelated fixture\n")
        self.script = self.base / "management-recreate.sh"
        self.script.write_text("#!/bin/sh\n# Inhibited fixture only\n")
        self.script.chmod(0o755)
        self.script_identity = self.script.stat()
        self.ids = ("a" * 64, "b" * 64)
        self.docker = tuple(SimpleNamespace(container_id=item) for item in self.ids)
        self.watcher = "auto-coder-management-proxy-watch.service"
        self.unrelated = "dbus.service"
        self.facts = {
            self.watcher: routes.SystemdRouteFact(self.watcher, "c" * 64, "masked", "inactive", 0, True),
            self.unrelated: routes.SystemdRouteFact(self.unrelated, "d" * 64, "enabled", "active", 201, False),
        }
        self.catalog = tuple(sorted(self.facts))
        monkeypatch.setattr(routes, "_systemd_catalog", lambda: self.catalog)
        monkeypatch.setattr(routes, "_systemd_unit", lambda unit, **kwargs: self.facts[unit])
        self.approval = SimpleNamespace(
            enrollment=SimpleNamespace(
                deployment_id="fixture-deployment",
                enrollment_id="fixture-enrollment",
                authorization_id="fixture-authorization",
                profile="ordinary-v1",
            ),
            enrollment_sha256="e" * 64,
        )
        self.manifest = routes.ApprovedRouteManifest(
            "approved-management-ordinary-routes-v1",
            self.approval.enrollment.deployment_id,
            self.approval.enrollment.enrollment_id,
            self.approval.enrollment.authorization_id,
            self.approval.enrollment.profile,
            self.approval.enrollment_sha256,
            True,
            True,
            tuple(routes.SystemdRoute(unit, fact.sha256, "restarter" if unit == self.watcher else "unrelated") for unit, fact in sorted(self.facts.items())),
            self.ids,
            routes._cron_catalog(roots=self.cron_roots),
            (),
            (routes.LocalScriptRoute(str(self.script), hashlib.sha256(self.script.read_bytes()).hexdigest(), self.script_identity.st_dev, self.script_identity.st_ino, self.watcher),),
            (),
        )
        self.path = self.root / "fixture-enrollment.routes.json"
        self.save()

    def save(self) -> None:
        self.path.write_text(json.dumps(asdict(self.manifest), sort_keys=True))
        self.path.chmod(0o600)

    def observe(self) -> routes.RouteManifestObservation:
        return routes._observe_approved_route_manifest(self.approval, self.docker, root=self.root, expected_uid=os.geteuid(), cron_roots=self.cron_roots)

    def __enter__(self) -> _closed_routes:
        return self

    def __exit__(self, *args: object) -> None:
        self.directory.cleanup()


def test_root_protected_exhaustive_route_manifest_is_independently_reobserved(monkeypatch: pytest.MonkeyPatch) -> None:
    with _closed_routes(monkeypatch) as fixture:
        observed = fixture.observe()
        assert observed.manifest == fixture.manifest
        assert observed.docker_container_ids == fixture.ids
        assert tuple(item.unit for item in observed.systemd) == fixture.catalog
        assert observed.cron_entries == fixture.manifest.cron_entries
        assert observed.local_script_sha256[0][0] == str(fixture.script)
        assert fixture.observe() == observed


def test_draft_never_self_attests_a_generated_route_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    with _closed_routes(monkeypatch) as fixture:
        monkeypatch.setattr(routes, "_CRON_ROOTS", fixture.cron_roots)
        original = fixture.path.read_bytes()
        draft = routes.propose_unapproved_route_draft(fixture.approval, fixture.docker)
        assert draft["complete_ordinary_routes_attested"] is False
        assert draft["local_script_routes"] == []
        assert draft["remote_script_origins"] == []
        assert [item["unit"] for item in draft["systemd_routes"]] == list(fixture.catalog)
        with pytest.raises(routes.RouteManifestUnavailable, match="completeness attestation"):
            routes.ApprovedRouteManifest.parse(draft)
        assert fixture.path.read_bytes() == original


def test_existing_cron_symlink_is_bound_to_protected_target_content_and_inode(monkeypatch: pytest.MonkeyPatch) -> None:
    with _closed_routes(monkeypatch) as fixture:
        target = fixture.base / "google-chrome-cron"
        target.write_text("#!/bin/sh\n# benign cron source\n")
        target.chmod(0o755)
        link = fixture.cron_roots[0] / "google-chrome"
        link.symlink_to(target)
        snapshot = routes._cron_catalog(roots=fixture.cron_roots, trusted_uid=os.geteuid())
        matching = next(item for item in snapshot if item.path == str(link))
        assert matching.kind == "symlink"
        assert matching.link_target == str(target)
        assert matching.target_inode == target.stat().st_ino
        fixture.manifest = replace(fixture.manifest, cron_entries=snapshot)
        fixture.save()
        assert fixture.observe().cron_entries == snapshot
        target.write_text("#!/bin/sh\n# different target contents\n")
        with pytest.raises(routes.RouteManifestUnavailable, match="changed cron/at route"):
            fixture.observe()


def test_cron_symlink_to_nonregular_target_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    with _closed_routes(monkeypatch) as fixture:
        (fixture.cron_roots[0] / "disabled").symlink_to("/dev/null")
        with pytest.raises(routes.RouteManifestUnavailable, match="not root-protected"):
            routes._cron_catalog(roots=fixture.cron_roots, trusted_uid=os.geteuid())


@pytest.mark.parametrize("mutation", ["new-unit", "changed-unit", "concealed-unit", "enabled-watcher", "new-cron", "changed-cron", "new-docker", "changed-script", "changed-manifest", "lost-approval"])
def test_hostile_route_change_refuses_even_after_positive_component(monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    with _closed_routes(monkeypatch) as fixture:
        assert fixture.observe().manifest == fixture.manifest
        if mutation == "new-unit":
            fixture.facts["new-socket.socket"] = routes.SystemdRouteFact("new-socket.socket", "1" * 64, "enabled", "active", 99, False)
            fixture.catalog = tuple(sorted(fixture.facts))
        elif mutation == "changed-unit":
            fixture.facts[fixture.unrelated] = replace(fixture.facts[fixture.unrelated], sha256="1" * 64)
        elif mutation == "concealed-unit":
            fixture.facts[fixture.unrelated] = replace(fixture.facts[fixture.unrelated], management_candidate=True)
        elif mutation == "enabled-watcher":
            fixture.facts[fixture.watcher] = replace(fixture.facts[fixture.watcher], inhibited=False, active_state="active", main_pid=99)
        elif mutation == "new-cron":
            (fixture.cron_roots[0] / "new-restarter").write_text("docker start original\n")
        elif mutation == "changed-cron":
            (fixture.cron_roots[0] / "system-cron").write_text("replaced\n")
        elif mutation == "new-docker":
            fixture.docker += (SimpleNamespace(container_id="f" * 64),)
        elif mutation == "changed-script":
            fixture.script.write_text("#!/bin/sh\ndocker start original\n")
        elif mutation == "changed-manifest":
            fixture.path.write_text(fixture.path.read_text().replace('"fixture-deployment"', '"different-deployment"'))
        elif mutation == "lost-approval":
            fixture.approval.enrollment_sha256 = "f" * 64
        with pytest.raises(routes.RouteManifestUnavailable):
            fixture.observe()


@pytest.mark.parametrize("mutation", ["omitted-watcher", "unrelated-watcher", "unreadable-remote", "extra-docker", "unapproved-cron-restarter", "unapproved-script-inhibitor", "missing-completeness", "duplicate-json"])
def test_forged_root_manifest_never_self_authorizes(monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    with _closed_routes(monkeypatch) as fixture:
        data = asdict(fixture.manifest)
        if mutation == "omitted-watcher":
            data["systemd_routes"] = [entry for entry in data["systemd_routes"] if entry["unit"] != fixture.watcher]
        elif mutation == "unrelated-watcher":
            next(entry for entry in data["systemd_routes"] if entry["unit"] == fixture.watcher)["role"] = "unrelated"
        elif mutation == "unreadable-remote":
            data["remote_script_origins"] = ["remote.example.invalid:/usr/local/bin/recreate"]
        elif mutation == "extra-docker":
            data["docker_container_ids"] += ("f" * 64,)
        elif mutation == "unapproved-cron-restarter":
            data["cron_restarter_paths"] = [str(fixture.cron_roots[0] / "system-cron")]
        elif mutation == "unapproved-script-inhibitor":
            data["local_script_routes"][0]["inhibited_by"] = "unknown.service"
        elif mutation == "missing-completeness":
            data["complete_ordinary_routes_attested"] = False
        if mutation == "duplicate-json":
            fixture.path.write_text('{"schema":"approved-management-ordinary-routes-v1","schema":"duplicate"}')
        else:
            fixture.path.write_text(json.dumps(data, sort_keys=True))
        with pytest.raises(routes.RouteManifestUnavailable):
            fixture.observe()


def test_second_read_refuses_unit_source_race(monkeypatch: pytest.MonkeyPatch) -> None:
    with _closed_routes(monkeypatch) as fixture:
        original = routes._systemd_unit
        calls = 0

        def concurrent_update(unit: str, **kwargs: object) -> routes.SystemdRouteFact:
            nonlocal calls
            calls += 1
            if calls == len(fixture.catalog) + 1:
                fixture.facts[fixture.unrelated] = replace(fixture.facts[fixture.unrelated], sha256="4" * 64)
            return original(unit, **kwargs)

        monkeypatch.setattr(routes, "_systemd_unit", concurrent_update)
        with pytest.raises(routes.RouteManifestUnavailable, match="changed during observation"):
            fixture.observe()


def test_manifest_file_requires_private_root_file_and_original_enrollment(monkeypatch: pytest.MonkeyPatch) -> None:
    with _closed_routes(monkeypatch) as fixture:
        fixture.path.chmod(0o644)
        with pytest.raises(EnrollmentUnavailable):
            fixture.observe()
        fixture.path.chmod(0o600)
        fixture.root.chmod(0o755)
        with pytest.raises(EnrollmentUnavailable):
            fixture.observe()
