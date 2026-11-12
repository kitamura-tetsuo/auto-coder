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


def test_passive_kernel_reflected_mount_is_inventoried_but_cannot_be_an_inhibited_restarter(monkeypatch: pytest.MonkeyPatch) -> None:
    shown = "Id=run-user-1000.mount\nLoadState=loaded\nUnitFileState=\nActiveState=active\n" "SubState=mounted\nFragmentPath=\nSourcePath=/proc/self/mountinfo\nNeedDaemonReload=no\n"

    def command(*args: str, **kwargs: object) -> str:
        if args[1] == "show":
            return shown
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(routes, "_command", command)
    fact = routes._systemd_unit("run-user-1000.mount")
    assert fact.main_pid == 0
    assert fact.active_state == "active"
    assert fact.inhibited is False
    monkeypatch.setattr(routes, "_command", lambda *args, **kwargs: shown.replace("/proc/self/mountinfo", "/unsupported/mountinfo") if args[1] == "show" else command(*args, **kwargs))
    with pytest.raises(routes.RouteManifestUnavailable, match="unit-file state"):
        routes._systemd_unit("run-user-1000.mount")


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


class _closed_v2_routes(_closed_routes):
    def __init__(self, monkeypatch: pytest.MonkeyPatch):
        from auto_coder.management.containment_user_routes import UserAccountObservation, UserControllerInventory

        super().__init__(monkeypatch)
        info = self.base.stat()
        self.binding = routes.DeploymentRootBinding(self.ids[0], "/bootstrap", str(self.base), False, info.st_dev, info.st_ino, info.st_mode)
        self.bindings = (self.binding,)
        self.approval.enrollment.management_id, self.approval.enrollment.proxy_id = self.ids
        self.approval.enrollment.original_files = (SimpleNamespace(path=str(self.script)),)
        account = UserAccountObservation(os.geteuid(), os.getegid(), "fixture-user", str(self.base), "/bin/sh", True, True, 321, "555", (str(self.base),), ())
        self.users = UserControllerInventory((account,), (), (), (os.geteuid(),), ())
        self.user_facts = (
            routes.UserSystemdRouteFact(os.geteuid(), "fixture-user", routes.SystemdRouteFact("desktop.service", "1" * 64, "enabled", "active", 123, False)),
            routes.UserSystemdRouteFact(os.geteuid(), "fixture-user", routes.SystemdRouteFact("manager-user.service", "2" * 64, "masked", "inactive", 0, True, True)),
        )
        monkeypatch.setattr(routes, "_deployment_root_bindings", lambda *args, **kwargs: self.bindings)
        monkeypatch.setattr(routes, "_user_controllers", lambda *args, **kwargs: (self.users, self.user_facts))
        self.manifest = replace(
            self.manifest,
            schema="approved-management-ordinary-routes-v2",
            deployment_roots=self.bindings,
            user_controller_catalog_sha256=routes._digest(asdict(self.users)),
            user_systemd_routes=tuple(routes.UserSystemdRoute(item.uid, item.fact.unit, item.fact.sha256, "restarter" if item.fact.management_candidate else "unrelated") for item in self.user_facts),
        )
        self.save()


def test_v2_closes_only_an_independently_matching_user_and_root_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    with _closed_routes(monkeypatch) as legacy:
        assert legacy.observe().closed_profile is False
    with _closed_v2_routes(monkeypatch) as fixture:
        observation = fixture.observe()
        assert observation.closed_profile is True
        assert observation.deployment_roots == fixture.bindings
        assert observation.user_controller_inventory == fixture.users
        assert observation.user_systemd == fixture.user_facts
        assert fixture.observe() == observation


@pytest.mark.parametrize("mutation", ("root-inode", "root-source", "user-lifetime", "user-account", "new-user-unit", "user-config", "user-restarter-enabled", "user-restarter-hidden", "system-pending-reload"))
def test_v2_refuses_actual_changed_or_uninhibited_user_and_root_scope(monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    with _closed_v2_routes(monkeypatch) as fixture:
        assert fixture.observe().closed_profile
        if mutation == "root-inode":
            fixture.bindings = (replace(fixture.binding, inode=fixture.binding.inode + 1),)
        elif mutation == "root-source":
            fixture.bindings = (replace(fixture.binding, source=str(fixture.base / "alternate")),)
        elif mutation in ("user-lifetime", "user-account"):
            account = fixture.users.accounts[0]
            account = replace(account, manager_start_token="556") if mutation == "user-lifetime" else replace(account, home=str(fixture.base / "other-home"))
            fixture.users = replace(fixture.users, accounts=(account,))
        elif mutation == "new-user-unit":
            fixture.user_facts += (routes.UserSystemdRouteFact(os.geteuid(), "fixture-user", routes.SystemdRouteFact("unknown.timer", "3" * 64, "enabled", "active", 0, False)),)
        elif mutation == "user-config":
            fixture.user_facts = (replace(fixture.user_facts[0], fact=replace(fixture.user_facts[0].fact, sha256="4" * 64)), fixture.user_facts[1])
        elif mutation == "user-restarter-enabled":
            fixture.user_facts = (fixture.user_facts[0], replace(fixture.user_facts[1], fact=replace(fixture.user_facts[1].fact, inhibited=False)))
        elif mutation == "user-restarter-hidden":
            fixture.manifest = replace(fixture.manifest, user_systemd_routes=tuple(replace(item, role="unrelated") for item in fixture.manifest.user_systemd_routes))
            fixture.save()
        elif mutation == "system-pending-reload":
            fixture.facts[fixture.unrelated] = replace(fixture.facts[fixture.unrelated], needs_daemon_reload=True)
        with pytest.raises(routes.RouteManifestUnavailable):
            fixture.observe()


def test_v2_refuses_a_user_controller_change_during_second_read(monkeypatch: pytest.MonkeyPatch) -> None:
    with _closed_v2_routes(monkeypatch) as fixture:
        calls = 0

        def concurrent_user_change(*args: object, **kwargs: object):
            nonlocal calls
            calls += 1
            if calls == 2:
                fixture.users = replace(fixture.users, runtime_uids=())
            return fixture.users, fixture.user_facts

        monkeypatch.setattr(routes, "_user_controllers", concurrent_user_change)
        with pytest.raises(routes.RouteManifestUnavailable, match="changed during observation"):
            fixture.observe()


def test_v2_draft_is_not_self_approval_and_includes_observed_user_and_root_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    with _closed_v2_routes(monkeypatch) as fixture:
        monkeypatch.setattr(routes, "_CRON_ROOTS", fixture.cron_roots)
        original = fixture.path.read_bytes()
        draft = routes.propose_unapproved_route_draft(fixture.approval, fixture.docker, closed_profile=True)
        assert draft["schema"] == "approved-management-ordinary-routes-v2"
        assert draft["complete_ordinary_routes_attested"] is False
        assert draft["deployment_roots"] == [asdict(fixture.binding)]
        assert draft["user_controller_catalog_sha256"] == routes._digest(asdict(fixture.users))
        assert len(draft["user_systemd_routes"]) == 2
        with pytest.raises(routes.RouteManifestUnavailable, match="completeness attestation"):
            routes.ApprovedRouteManifest.parse(draft)
        assert fixture.path.read_bytes() == original


def test_user_systemctl_reads_target_the_exact_local_user_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = []

    def command(*args: str, **kwargs: object) -> str:
        commands.append(args)
        if "--property=UnitPath" in args:
            return "/home/fixture-user/.config/systemd/user /usr/lib/systemd/user\n"
        field = "unit_file" if "list-unit-files" in args else "unit"
        return json.dumps([{field: "desktop.service"}])

    monkeypatch.setattr(routes, "_command", command)
    assert routes._systemd_catalog(user="fixture-user") == ("desktop.service",)
    assert routes._user_unit_paths("fixture-user") == ("/home/fixture-user/.config/systemd/user", "/usr/lib/systemd/user")
    assert all(call[:3] == ("systemctl", "--user", "--machine=fixture-user@.host") for call in commands)
    with pytest.raises(routes.RouteManifestUnavailable, match="account name"):
        routes._systemctl_prefix("another@remote-host")


def test_user_unit_candidate_detection_includes_original_deployment_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    shown = "Id=innocent.service\nLoadState=loaded\nUnitFileState=enabled\nActiveState=active\nSubState=running\nFragmentPath=/home/fixture/.config/systemd/user/innocent.service\nNeedDaemonReload=no\nMainPID=123\n"
    source = "[Service]\nExecStart=/usr/local/bin/recreate --config /srv/original-state/config.json\n"
    monkeypatch.setattr(routes, "_command", lambda *args, **kwargs: shown if "show" in args else source)
    fact = routes._systemd_unit("innocent.service", user="fixture", trusted_uid=1000, extra_cues=("/srv/original-state",))
    assert fact.management_candidate is True
    assert fact.inhibited is False


def test_user_source_tree_tracks_real_masks_links_and_source_substitution(tmp_path: Path) -> None:
    from auto_coder.management import containment_user_routes as users

    root = tmp_path / "user"
    root.mkdir()
    unit = root / "example.service"
    unit.write_text("[Service]\nExecStart=/usr/bin/true\n")
    mask = root / "blocked.service"
    mask.symlink_to("/dev/null")
    linked = root / "linked.service"
    linked.symlink_to(unit)
    first = users.observe_user_sources((root,), uid=os.geteuid(), cues=("/srv/original-state",))
    assert next(item for item in first if item.path == str(mask)).kind == "mask"
    assert next(item for item in first if item.path == str(linked)).target_inode == unit.stat().st_ino
    unit.write_text("[Service]\nExecStart=/usr/local/bin/recreate --config /srv/original-state/config.json\n")
    second = users.observe_user_sources((root,), uid=os.geteuid(), cues=("/srv/original-state",))
    assert second != first
    assert next(item for item in second if item.path == str(linked)).management_candidate is True
    unit.chmod(0o666)
    with pytest.raises(users.UserControllerUnavailable, match="writable|permissions"):
        users.observe_user_sources((root,), uid=os.geteuid(), cues=())


def test_user_source_tree_refuses_opaque_linked_unit_directories(tmp_path: Path) -> None:
    from auto_coder.management import containment_user_routes as users

    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "user"
    link.symlink_to(target)
    with pytest.raises(users.UserControllerUnavailable, match="unsupported"):
        users.observe_user_sources((link,), uid=os.geteuid(), cues=())


def _user_inventory_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from auto_coder.management import containment_user_routes as users

    home = tmp_path / "account"
    home.mkdir()
    global_units = tmp_path / "system-user"
    global_units.mkdir()
    runtime = tmp_path / "run-user"
    runtime.mkdir()
    linger = tmp_path / "linger"
    linger.mkdir()
    account = SimpleNamespace(pw_uid=os.geteuid(), pw_gid=os.getegid(), pw_name="fixture-user", pw_dir=str(home), pw_shell="/bin/sh")
    monkeypatch.setattr(users.pwd, "getpwall", lambda: (account,))
    monkeypatch.setattr(users, "observe_user_manager_lifetimes", lambda: ())

    def read():
        return users.observe_user_controller_inventory({}, lambda user: (), cues=("/srv/original-state",), global_roots=(global_units,), runtime_root=runtime, linger_root=linger, trusted_uid=os.geteuid())

    return users, account, home, global_units, runtime, linger, read


def test_inactive_user_inventory_reads_every_installed_home_and_refuses_unknown_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    users, account, home, global_units, runtime, linger, read = _user_inventory_fixture(tmp_path, monkeypatch)
    first = read()
    assert len(first.accounts) == 1
    assert first.accounts[0].manager_pid == 0
    assert any(item.path == str(home / ".config/systemd/user") and item.kind == "missing" for item in first.accounts[0].sources)
    (runtime / str(os.geteuid() + 1)).mkdir()
    with pytest.raises(users.UserControllerUnavailable, match="unknown user runtime"):
        read()


def test_inactive_user_with_installed_management_route_requires_real_inhibition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    users, account, home, global_units, runtime, linger, read = _user_inventory_fixture(tmp_path, monkeypatch)
    unit_root = home / ".config/systemd/user"
    unit_root.mkdir(parents=True)
    (unit_root / "renamed.service").write_text("[Service]\nExecStart=/usr/local/bin/recreate --config /srv/original-state/config.json\n")
    with pytest.raises(users.UserControllerUnavailable, match="inactive user"):
        read()


def test_live_user_manager_must_match_independent_systemd_process_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    users, account, home, global_units, runtime, linger, read = _user_inventory_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(users, "observe_user_manager_lifetimes", lambda: (users.UserManagerLifetime(os.geteuid(), 8765, "999"),))
    with pytest.raises(users.UserControllerUnavailable, match="unlisted or contradictory"):
        read()


def test_user_manager_discovery_reads_proc_lifetimes_independently_of_unit_names(tmp_path: Path) -> None:
    from auto_coder.management import containment_user_routes as users

    process = tmp_path / "4321"
    process.mkdir()
    (process / "stat").write_text("4321 (systemd) S " + "0 " * 18 + "987654 0\n")
    (process / "cmdline").write_bytes(b"/usr/lib/systemd/systemd\x00--user\x00")
    (process / "exe").symlink_to("/usr/lib/systemd/systemd")
    (process / "status").write_text("Uid:\t1000\t1000\t1000\t1000\n")
    assert users.observe_user_manager_lifetimes(proc=tmp_path) == (users.UserManagerLifetime(1000, 4321, "987654"),)
    (process / "exe").unlink()
    with pytest.raises(users.UserControllerUnavailable, match="inaccessible"):
        users.observe_user_manager_lifetimes(proc=tmp_path)


def test_active_user_inventory_binds_actual_manager_load_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    users, account, home, global_units, runtime, linger, read = _user_inventory_fixture(tmp_path, monkeypatch)
    manager = users.UserManagerLifetime(os.geteuid(), 8765, "999")
    monkeypatch.setattr(users, "observe_user_manager_lifetimes", lambda: (manager,))
    custom_root = home / "custom-user-units"
    custom_root.mkdir()
    source = custom_root / "desktop.service"
    source.write_text("[Service]\nExecStart=/usr/bin/true\n")
    actual_paths = (str(custom_root), str(global_units))
    first = users.observe_user_controller_inventory({os.geteuid(): 8765}, lambda user: actual_paths, cues=(), global_roots=(global_units,), runtime_root=runtime, linger_root=linger, trusted_uid=os.geteuid())
    assert first.accounts[0].unit_paths == actual_paths
    assert first.accounts[0].manager_start_token == "999"
    assert any(item.path == str(source) for item in first.accounts[0].sources)
    source.write_text("[Service]\nExecStart=/usr/bin/false\n")
    second = users.observe_user_controller_inventory({os.geteuid(): 8765}, lambda user: actual_paths, cues=(), global_roots=(global_units,), runtime_root=runtime, linger_root=linger, trusted_uid=os.geteuid())
    assert first != second


def test_unchanged_unit_does_not_hide_changed_external_local_launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wrapper = tmp_path / "launcher.sh"
    wrapper.write_text("#!/bin/sh\nexec /usr/bin/true\n")
    shown = "Id=desktop.service\nLoadState=loaded\nUnitFileState=enabled\nActiveState=active\nSubState=running\nFragmentPath=/home/fixture/.config/systemd/user/desktop.service\nNeedDaemonReload=no\nMainPID=123\nExecStart=" + str(wrapper) + "\n"
    source = "[Service]\nExecStart=" + str(wrapper) + "\n"
    monkeypatch.setattr(routes, "_command", lambda *args, **kwargs: shown if "show" in args else source)
    first = routes._systemd_unit("desktop.service", user="fixture", trusted_uid=os.geteuid(), extra_cues=("/srv/original-state",), inspect_local_inputs=True)
    assert first.management_candidate is False
    assert len(first.command_sources) == 1
    assert first.command_sources[0].path == str(wrapper)
    wrapper.write_text("#!/bin/sh\nexec /usr/local/bin/recreate --config /srv/original-state/config.json\n")
    second = routes._systemd_unit("desktop.service", user="fixture", trusted_uid=os.geteuid(), extra_cues=("/srv/original-state",), inspect_local_inputs=True)
    assert second.sha256 != first.sha256
    assert second.management_candidate is True
    assert second.command_sources != first.command_sources


def test_v2_route_observer_refuses_mutated_external_cron_wrapper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _closed_v2_routes(monkeypatch) as fixture:
        wrapper = tmp_path / "ordinary-job.sh"
        wrapper.write_text("#!/bin/sh\nexec /usr/bin/true\n")
        (fixture.cron_roots[0] / "system-cron").write_text("* * * * * root " + str(wrapper) + "\n")
        cues = routes._deployment_cues(fixture.approval, fixture.bindings)
        fixture.manifest = replace(fixture.manifest, cron_entries=routes._cron_catalog(roots=fixture.cron_roots, trusted_uid=os.geteuid(), extra_cues=cues, inspect_local_inputs=True))
        fixture.save()
        assert fixture.observe().closed_profile
        wrapper.write_text("#!/bin/sh\nexec /usr/bin/false\n")
        with pytest.raises(routes.RouteManifestUnavailable, match="changed cron/at route"):
            fixture.observe()


def test_explicit_nested_local_script_inputs_are_bound_but_distro_programs_stay_trusted(tmp_path: Path) -> None:
    from auto_coder.management import containment_route_sources as sources

    helper = tmp_path / "helper.sh"
    helper.write_text("#!/bin/sh\nexec /usr/bin/true\n")
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text("#!/bin/sh\n. " + str(helper) + "\n")
    facts, candidate = sources.observe_command_sources(str(wrapper), uid=os.geteuid(), cues=("/srv/original-state",))
    assert {item.path for item in facts} == {str(wrapper), str(helper)}
    assert candidate is False
    assert sources.observe_command_sources("/usr/bin/true", uid=os.geteuid(), cues=()) == ((), False)
    helper.write_text("#!/bin/sh\nexec /usr/local/bin/recreate --config /srv/original-state/config.json\n")
    changed, candidate = sources.observe_command_sources(str(wrapper), uid=os.geteuid(), cues=("/srv/original-state",))
    assert changed != facts
    assert candidate is True


@pytest.mark.parametrize("command", ('/bin/bash -c "$RUN"', "/usr/bin/python3 -c 'run_selected_module()'", "/bin/sh ./unknown.sh", "/usr/bin/python3 -m mutable_local_package"))
def test_opaque_local_script_dispatch_cannot_inherit_an_unrelated_classification(command: str) -> None:
    from auto_coder.management import containment_route_sources as sources

    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch|source binding"):
        sources.observe_command_sources(command, uid=os.geteuid(), cues=())


def test_known_isolated_watcher_module_requires_approved_current_source(tmp_path: Path) -> None:
    from auto_coder.management import containment_route_sources as sources

    watcher = tmp_path / "proxy_watch_cli.py"
    watcher.write_text("def main():\n    return 0\n")
    command = "/usr/bin/python3 -I -B -m auto_coder.management.proxy_watch_cli --config /etc/fixture-watcher.json"
    with pytest.raises(sources.CommandSourceUnavailable, match="source binding"):
        sources.observe_command_sources(command, uid=os.geteuid(), cues=())
    facts, _ = sources.observe_command_sources(command, uid=os.geteuid(), cues=(), known_modules=(("auto_coder.management.proxy_watch_cli", str(watcher)),))
    assert next(item for item in facts if item.path == str(watcher)).sha256 == hashlib.sha256(watcher.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "command",
    (
        "exec ./unbound-launcher",
        "exec relative/launcher",
        "exec $LOCAL_ROOT/launcher",
        "/bin/sh /opt/${JOB}/launcher.sh",
        '/bin/sh "/opt/local job/launcher.sh"',
        "/bin/sh /opt/local,job/launcher.sh",
        "/bin/sh /opt/非ASCII/launcher.sh",
    ),
)
def test_nonliteral_local_dispatch_is_refused_without_prefix_truncation(command: str) -> None:
    from auto_coder.management import containment_route_sources as sources

    with pytest.raises(sources.CommandSourceUnavailable, match="unsupported"):
        sources.observe_command_sources(command, uid=os.geteuid(), cues=())


def test_nested_local_wrapper_cannot_hide_unbound_module_dispatch(tmp_path: Path) -> None:
    from auto_coder.management import containment_route_sources as sources

    wrapper = tmp_path / "module-wrapper.sh"
    wrapper.write_text("#!/bin/sh\nexec /usr/bin/python3 -I -B -m unknown_local_module\n")
    with pytest.raises(sources.CommandSourceUnavailable, match="source binding"):
        sources.observe_command_sources(str(wrapper), uid=os.geteuid(), cues=())


def test_path_resolved_shell_helper_refuses_stale_unrelated_classification(tmp_path: Path) -> None:
    from auto_coder.management import containment_route_sources as sources

    tools = tmp_path / "tools"
    tools.mkdir(mode=0o700)
    helper = tools / "maintenance"
    helper.write_text("#!/bin/sh\necho original\n")
    helper.chmod(0o700)
    command = f"ExecStart=/bin/sh -c maintenance\nEnvironment=PATH={tools}\n"
    observed_before = subprocess.run(["/bin/sh", "-c", "maintenance"], env={"PATH": str(tools)}, check=True, capture_output=True, text=True)
    assert observed_before.stdout.strip() == "original"
    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        sources.observe_command_sources(command, uid=os.geteuid(), cues=("/srv/original-state",))

    helper.write_text("#!/bin/sh\necho /srv/original-state\n")
    observed_after = subprocess.run(["/bin/sh", "-c", "maintenance"], env={"PATH": str(tools)}, check=True, capture_output=True, text=True)
    assert observed_after.stdout.strip() == "/srv/original-state"
    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        sources.observe_command_sources(command, uid=os.geteuid(), cues=("/srv/original-state",))


def test_systemd_source_boundary_refuses_bare_shell_path_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from auto_coder.management import containment_route_sources as sources

    tools = tmp_path / "tools"
    tools.mkdir(mode=0o700)
    helper = tools / "maintenance"
    helper.write_text("#!/bin/sh\nexec /usr/bin/true\n")
    helper.chmod(0o700)
    shown = (
        "Id=desktop.service\nLoadState=loaded\nUnitFileState=enabled\n"
        "ActiveState=active\nSubState=running\n"
        "FragmentPath=/etc/systemd/system/desktop.service\nNeedDaemonReload=no\nMainPID=123\n"
        f"ExecStart=/bin/sh -c maintenance\nEnvironment=PATH={tools}\n"
    )
    monkeypatch.setattr(routes, "_command", lambda *args, **kwargs: shown if "show" in args else "[Service]\nExecStart=/bin/sh -c maintenance\n")
    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        routes._systemd_unit("desktop.service", trusted_uid=os.geteuid(), extra_cues=("/srv/original-state",), inspect_local_inputs=True)
    helper.write_text("#!/bin/sh\nexec /usr/local/bin/recreate --config /srv/original-state/config.json\n")
    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        routes._systemd_unit("desktop.service", trusted_uid=os.geteuid(), extra_cues=("/srv/original-state",), inspect_local_inputs=True)


@pytest.mark.parametrize(
    "command",
    (
        "/bin/sh -c maintenance",
        "/bin/sh -c 'exec maintenance'",
        '/usr/bin/bash -c "PATH=/opt/tools maintenance"',
        "/bin/sh -c '/usr/bin/true; maintenance'",
        "/bin/dash -c '/usr/bin/true && maintenance'",
    ),
)
def test_shell_c_refuses_path_dispatch_and_compound_fallback(command: str) -> None:
    from auto_coder.management import containment_route_sources as sources

    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        sources.observe_command_sources(command, uid=os.geteuid(), cues=())
    assert sources.observe_command_sources("/bin/sh -c '/usr/bin/true'", uid=os.geteuid(), cues=()) == ((), False)
