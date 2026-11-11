"""Protocol and hostile interleaving tests for protected ordinary-route approval.

These do not stand in for the Issue #2827 real full-profile OS/Docker fixture.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_coder.management import containment_route_manifest as routes
from auto_coder.management.containment_enrollment import EnrollmentUnavailable


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
