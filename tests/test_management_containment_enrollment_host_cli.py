"""CLI safety and image selection for the explicitly authorized host fixture.

These parser tests do not execute Docker or grant a containment verdict.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from scripts import test_management_containment_enrollment_host as fixture

REVIEWED_SOURCE_SHA = "66bcfefa86aa2bd88dd4a253405fc6faac65675917e8d393e587d5a62fff83bb"


def test_host_fixture_requires_explicit_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["host-fixture", "--test-image", "sha256:" + "a" * 64])
    with pytest.raises(SystemExit) as raised:
        fixture.main()
    assert raised.value.code == 2


def test_host_fixture_passes_exact_test_image_to_authorized_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    options: dict[str, object] = {}

    def selected(*, exercise_test_route_manifest: bool, exercise_disposable_systemd: bool, test_image: str, require_reviewed_ordinary_source: bool) -> None:
        options.update(route_manifest=exercise_test_route_manifest, systemd=exercise_disposable_systemd, image=test_image, reviewed_source=require_reviewed_ordinary_source)

    monkeypatch.setattr(fixture, "run_fixture", selected)
    selected_image = "sha256:" + "b" * 64
    monkeypatch.setattr(sys, "argv", ["host-fixture", "--confirm-test-only-fixture", "--exercise-test-route-manifest", "--exercise-disposable-systemd", "--require-reviewed-ordinary-source", "--test-image", selected_image])
    fixture.main()
    assert options == {"route_manifest": True, "systemd": True, "image": selected_image, "reviewed_source": True}


def test_host_fixture_keeps_default_preinstalled_image(monkeypatch: pytest.MonkeyPatch) -> None:
    options: dict[str, object] = {}

    def selected(*, exercise_test_route_manifest: bool, exercise_disposable_systemd: bool, test_image: str, require_reviewed_ordinary_source: bool) -> None:
        options.update(route_manifest=exercise_test_route_manifest, systemd=exercise_disposable_systemd, image=test_image, reviewed_source=require_reviewed_ordinary_source)

    monkeypatch.setattr(fixture, "run_fixture", selected)
    monkeypatch.setattr(sys, "argv", ["host-fixture", "--confirm-test-only-fixture"])
    fixture.main()
    assert options == {"route_manifest": False, "systemd": False, "image": fixture.IMAGE_TAG, "reviewed_source": False}


def test_disposable_systemd_requires_a_read_back_route_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fixture.os, "geteuid", lambda: 0)
    monkeypatch.setattr(fixture.os, "readlink", lambda _: "pid:[1]")
    with pytest.raises(ValueError, match="requires independently read-back"):
        fixture.run_fixture(exercise_disposable_systemd=True)


def test_disposable_systemd_identities_reject_unrelated_names_without_effects() -> None:
    with pytest.raises(ValueError, match="fixture identity differs"):
        fixture.DisposableSystemdRoutes.install(fixture_id="production-watcher", manager_id="a" * 64)


def test_disposable_systemd_masks_are_persistent_but_sources_are_runtime_only(monkeypatch: pytest.MonkeyPatch) -> None:
    name = "ac-test-pr2829-" + "a" * 32 + "-management.service"
    routes = fixture.DisposableSystemdRoutes((name,), ())
    actions: list[tuple[str, ...]] = []
    monkeypatch.setattr(fixture, "_systemctl", lambda *arguments: actions.append(arguments))
    monkeypatch.setattr(fixture, "_verify_expected", lambda *_: {"route_manifest": None})
    monkeypatch.setattr(routes, "assert_inhibited", lambda: None)
    assert fixture._SYSTEMD_UNIT_ROOT == Path("/run/systemd/system")
    assert fixture._SYSTEMD_MASK_ROOT == Path("/etc/systemd/system")
    routes.temporarily_unmask_and_verify_refusal(Path("/fixture/scope"), Path("/fixture/previous"))
    assert actions == [("unmask", name), ("mask", name)]


def test_reviewed_source_requires_actual_image_readback() -> None:
    with pytest.raises(AssertionError, match="reviewed original-source readback is unavailable"):
        fixture._assert_reviewed_ordinary_source({"installed_bootstrap_source": None, "unavailable": [{"authority": "installed-bootstrap-source", "reason": "absent"}]})
    with pytest.raises(AssertionError, match="contradictory refusal"):
        fixture._assert_reviewed_ordinary_source({"installed_bootstrap_source": {"reviewed_lock_before_launch": True, "source_sha256": REVIEWED_SOURCE_SHA, "source_bytes": 40}, "unavailable": [{"authority": "installed-bootstrap-source", "reason": "contradiction"}]})
    with pytest.raises(AssertionError, match="reviewed original-source readback is unavailable"):
        fixture._assert_reviewed_ordinary_source({"installed_bootstrap_source": {"reviewed_lock_before_launch": True, "source_sha256": "0" * 64, "source_bytes": 40}, "unavailable": []})
    fixture._assert_reviewed_ordinary_source({"installed_bootstrap_source": {"reviewed_lock_before_launch": True, "source_sha256": REVIEWED_SOURCE_SHA, "source_bytes": 40}, "unavailable": []})


def test_host_verifier_retries_only_transient_global_docker_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    reports = [
        {"route_manifest": None, "unavailable": [{"authority": "approved-route-manifest", "reason": "whole-engine Docker inventory differs from approved routes"}]},
        {"route_manifest": {"manifest": "reobserved"}, "kernel_subobservation": {"pinned": True}, "unavailable": []},
    ]
    observed: list[tuple[str, Path, Path]] = []

    def verify(command: str, scope: Path, previous: Path) -> dict[str, object]:
        observed.append((command, scope, previous))
        return reports.pop(0)

    monkeypatch.setattr(fixture, "_call_cli", verify)
    monkeypatch.setattr(fixture.time, "sleep", lambda _: None)
    scope, previous = Path("/test/fixture/scope"), Path("/test/fixture/previous")
    assert fixture._verify_after_transient_docker_inventory(scope, previous)["route_manifest"] == {"manifest": "reobserved"}
    assert observed == [("verify", scope, previous), ("verify", scope, previous)]


def test_host_verifier_does_not_retry_unrelated_route_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    report = {"route_manifest": None, "unavailable": [{"authority": "approved-route-manifest", "reason": "installed ordinary routes changed"}]}
    observed: list[tuple[str, Path, Path]] = []

    def verify(command: str, scope: Path, previous: Path) -> dict[str, object]:
        observed.append((command, scope, previous))
        return report

    monkeypatch.setattr(fixture, "_call_cli", verify)
    scope, previous = Path("/test/fixture/scope"), Path("/test/fixture/previous")
    assert fixture._verify_after_transient_docker_inventory(scope, previous) is report
    assert observed == [("verify", scope, previous)]


def test_host_verifier_does_not_accept_a_route_without_kernel_readback(monkeypatch: pytest.MonkeyPatch) -> None:
    report = {"route_manifest": {"manifest": "reobserved"}, "kernel_subobservation": None, "unavailable": [{"authority": "kernel-subcomponent", "reason": "missing"}]}
    called = 0

    def verify(*_: object) -> dict[str, object]:
        nonlocal called
        called += 1
        return report

    monkeypatch.setattr(fixture, "_call_cli", verify)
    assert fixture._verify_after_transient_docker_inventory(Path("/fixture/scope"), Path("/fixture/previous")) is report
    assert called == 1


def test_fixture_docker_failure_logs_bounded_stderr_and_preserves_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    message = 'exec: "run": executable file not found in $PATH\n' + "x" * 3000 + "unlogged-tail"
    failure = subprocess.CalledProcessError(127, ("docker", "start", "fixture-container"), output="unlogged-stdout", stderr=message)

    def failed(*_: object, **__: object) -> object:
        raise failure

    monkeypatch.setattr(fixture.subprocess, "run", failed)
    messages: list[str] = []
    sink = fixture.logger.add(messages.append, format="{message}", level="ERROR")
    try:
        with pytest.raises(subprocess.CalledProcessError) as raised:
            fixture._docker("start", "fixture-container")
    finally:
        fixture.logger.remove(sink)
    assert raised.value is failure
    assert len(messages) == 1
    assert "Docker start failed (exit 127)" in messages[0]
    assert message[:2048] in messages[0]
    assert "unlogged-tail" not in messages[0]
    assert "unlogged-stdout" not in messages[0]
