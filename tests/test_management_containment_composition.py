"""Component-protocol regressions; these are not real-host acceptance evidence."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from auto_coder.management import containment_observation as observation
from auto_coder.management.containment_enrollment import ApprovedEnrollment, EnrollmentObservation
from auto_coder.management.containment_kernel_subobservation import KernelSubObservation
from auto_coder.management.containment_route_manifest import ApprovedRouteManifest, RouteManifestObservation, SystemdRoute
from auto_coder.management.installed_bootstrap_mount_profile import InstalledBootstrapMountProfile
from auto_coder.management.installed_bootstrap_source import InstalledBootstrapSource
from auto_coder.management.systemd_watcher_observation import WatcherSystemdObservation


@pytest.fixture
def closed_components(monkeypatch: pytest.MonkeyPatch) -> observation.ProposedScope:
    """Supply independently tested component contracts to exercise composition."""
    scope = observation.ProposedScope("deployment", "enrollment", "authorization", "ordinary-v1", "a" * 64, "b" * 64)
    image = "sha256:" + "c" * 64
    enrollment = ApprovedEnrollment("approved-current-containment-enrollment-v3", scope.deployment_id, scope.enrollment_id, scope.authorization_id, scope.profile, "engine", scope.management_id, image, scope.proxy_id, image, (), "/fixture/prior.json", "d" * 64, True)
    enrolled = EnrollmentObservation(enrollment, "e" * 64, 1, 2, 3, (), "d" * 64)
    docker = tuple(observation.DockerObservation("engine", identifier, image, digest * 64, "no", 0, False, "exited", 0, "start", "finish", 0, "d" * 64) for identifier, digest in ((scope.management_id, "a"), (scope.proxy_id, "b")))
    source = InstalledBootstrapSource(scope.management_id, image, "/opt/reviewed/bootstrap.py", "c" * 64, 1000, True)
    profile = InstalledBootstrapMountProfile(scope.management_id, scope.proxy_id, scope.profile, "a" * 64, "b" * 64, "c" * 64, "d" * 64, "e" * 64, "f" * 64, "/bootstrap/config.json", "/bootstrap/management.yml", 1, 2)
    watcher = WatcherSystemdObservation("auto-coder-management-proxy-watch.service", "/etc/systemd/system/auto-coder-management-proxy-watch.service", "masked", "masked", "inactive", "dead", 0, False, 1, 2, 3, 0, 0o777, "/dev/null", True)
    manifest = ApprovedRouteManifest("approved-management-ordinary-routes-v2", scope.deployment_id, scope.enrollment_id, scope.authorization_id, scope.profile, "e" * 64, True, True, (SystemdRoute(watcher.unit, "f" * 64, "restarter"),), tuple(item.container_id for item in docker), (), (), (), ())
    route = RouteManifestObservation(manifest, "f" * 64, 1, 3, 4, (), (), (), tuple(item.container_id for item in docker), closed_profile=True)
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()

    def kernel(*_args: object, **_kwargs: object) -> KernelSubObservation:
        started = time.monotonic_ns()
        return KernelSubObservation(
            "engine",
            scope.management_id,
            scope.proxy_id,
            111,
            "1",
            222,
            "2",
            "net:[1234]",
            1,
            2,
            8080,
            boot,
            started,
            time.monotonic_ns(),
            True,
            True,
            True,
            external_namespace_pin_verified=True,
            namespace_pin_holder_pid=333,
            namespace_pin_holder_start_token="3",
            namespace_pin_holder_fd=4,
            external_owner_fence_verified=True,
            owner_fence_holder_pid=333,
            owner_fence_holder_start_token="3",
            owner_fence_holder_fd=5,
            installed_profile=scope.profile,
            management_inspection_sha256="a" * 64,
            proxy_inspection_sha256="b" * 64,
        )

    monkeypatch.setattr(observation.os, "geteuid", lambda: 0)
    monkeypatch.setattr(observation, "_validate_host_proc_profile", lambda *_args: None)
    monkeypatch.setattr(observation, "observe_approved_enrollment", lambda _scope: enrolled)
    monkeypatch.setattr(observation, "_docker", lambda _scope: docker)
    monkeypatch.setattr(observation, "observe_watcher_unit", lambda: watcher)
    monkeypatch.setattr(observation, "inspect_installed_bootstrap_source", lambda _enrolled: source)
    monkeypatch.setattr(observation, "inspect_installed_bootstrap_mount_profile", lambda *_args: profile)
    monkeypatch.setattr(observation, "observe_approved_route_manifest", lambda *_args: route)
    monkeypatch.setattr(observation, "observe_kernel_subcomponent", kernel)
    return scope


def _saved(report: observation.PrerequisiteReport) -> dict:
    return json.loads(json.dumps(asdict(report)))


def test_fresh_kernel_sampling_windows_do_not_make_unchanged_evidence_fail(closed_components: observation.ProposedScope) -> None:
    producer = observation.observe(closed_components)
    assert producer.status == observation.OBSERVED
    assert not producer.unavailable
    consumer = observation.verify(closed_components, _saved(producer))
    assert consumer.status == observation.OBSERVED
    assert not consumer.unavailable
    assert consumer.observation_id != producer.observation_id
    assert consumer.kernel_subobservation.started_monotonic_ns > producer.kernel_subobservation.finished_monotonic_ns
    assert "never a grant" in consumer.resource_limit


@pytest.mark.parametrize("changed", ("namespace_pin_holder_fd", "owner_fence_holder_start_token", "prior_network_namespace", "management_inspection_sha256"))
def test_saved_complete_kernel_scope_cannot_be_substituted(closed_components: observation.ProposedScope, changed: str) -> None:
    saved = _saved(observation.observe(closed_components))
    saved["kernel_subobservation"][changed] = 99 if changed.endswith("_fd") else "substituted"
    result = observation.verify(closed_components, saved)
    assert result.status == "unavailable"
    assert "saved-report:kernel_subobservation" in {item.authority for item in result.unavailable}


@pytest.mark.parametrize("field,value", (("started_monotonic_ns", True), ("finished_monotonic_ns", 0)))
def test_malformed_kernel_sampling_window_is_refused(closed_components: observation.ProposedScope, field: str, value: object) -> None:
    saved = _saved(observation.observe(closed_components))
    saved["kernel_subobservation"][field] = value
    result = observation.verify(closed_components, saved)
    assert result.status == "unavailable"
    assert "saved-report:kernel_subobservation" in {item.authority for item in result.unavailable}


@pytest.mark.parametrize(
    "function,authority", (("inspect_installed_bootstrap_source", "original-source-profile"), ("inspect_installed_bootstrap_mount_profile", "original-source-profile"), ("observe_approved_route_manifest", "ordinary-route-closure"), ("observe_kernel_subcomponent", "kernel-containment"))
)
def test_one_missing_authority_prevents_partial_components_becoming_success(monkeypatch: pytest.MonkeyPatch, closed_components: observation.ProposedScope, function: str, authority: str) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise ValueError("independent authority unavailable")

    monkeypatch.setattr(observation, function, unavailable)
    result = observation.observe(closed_components)
    assert result.status == "unavailable"
    assert authority in {item.authority for item in result.unavailable}
    assert "deployment-enrollment" in {item.authority for item in result.unavailable}


def test_legacy_route_component_does_not_become_complete_scope(monkeypatch: pytest.MonkeyPatch, closed_components: observation.ProposedScope) -> None:
    original = observation.observe_approved_route_manifest
    monkeypatch.setattr(observation, "observe_approved_route_manifest", lambda *args: replace(original(*args), closed_profile=False))
    result = observation.observe(closed_components)
    assert result.status == "unavailable"
    assert "ordinary-route-closure" in {item.authority for item in result.unavailable}


def test_holder_loss_after_first_kernel_sample_refuses_complete_observation(monkeypatch: pytest.MonkeyPatch, closed_components: observation.ProposedScope) -> None:
    original = observation.observe_kernel_subcomponent
    calls = 0

    def lose_holder(*args: object, **kwargs: object) -> KernelSubObservation:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise ValueError("external owner fence holder exited")
        return original(*args, **kwargs)

    monkeypatch.setattr(observation, "observe_kernel_subcomponent", lose_holder)
    result = observation.observe(closed_components)
    assert calls == 2
    assert result.status == "unavailable"
    assert "complete-observation-race" in {item.authority for item in result.unavailable}


def test_current_success_does_not_accept_a_saved_launch_grant(closed_components: observation.ProposedScope) -> None:
    saved = _saved(observation.observe(closed_components))
    saved["status"] = "launch-authorized"
    result = observation.verify(closed_components, saved)
    assert result.status == "unavailable"
    assert "saved-report" in {item.authority for item in result.unavailable}
