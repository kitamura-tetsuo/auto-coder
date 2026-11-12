"""Unit checks for VM authorization and full-host result oracles.

These tests never run Docker/systemd or claim the full host acceptance passed.
The maintained VM driver itself uses actual production CLI processes and kernel
controls; no synthetic observation is supplied to that path.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from scripts import test_management_containment_closed_host as fixture


def _authorization(path: Path) -> dict[str, object]:
    value: dict[str, object] = {"schema": fixture.MARKER_SCHEMA, "authorization_id": "disposable-test", "machine_id": "a" * 32, "boot_id": "ceab74bd-72c8-4d02-b3b1-3581e9d1eca2", "engine_id": "fixture-engine", "synthetic_only": True}
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return value


def test_authorization_is_an_existing_protected_record(tmp_path: Path) -> None:
    marker = tmp_path / "host.json"
    expected = _authorization(marker)
    assert fixture.read_authorization(marker, uid=os.geteuid()) == expected
    assert marker.read_text() == json.dumps(expected)
    marker.chmod(0o644)
    with pytest.raises(PermissionError, match="unprotected"):
        fixture.read_authorization(marker, uid=os.geteuid())


def test_absent_or_linked_authorization_never_creates_a_record(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(FileNotFoundError):
        fixture.read_authorization(missing, uid=os.geteuid())
    assert not missing.exists()
    target = tmp_path / "target.json"
    _authorization(target)
    missing.symlink_to(target)
    with pytest.raises(PermissionError, match="not canonical"):
        fixture.read_authorization(missing, uid=os.geteuid())


@pytest.mark.parametrize("field,value", [("synthetic_only", False), ("authorization_id", "../../not-a-fixture"), ("schema", "production-host"), ("engine_id", ""), ("unexpected", True)])
def test_authorization_rejects_ambiguous_scope(tmp_path: Path, field: str, value: object) -> None:
    marker = tmp_path / "host.json"
    content = _authorization(marker)
    content[field] = value
    marker.write_text(json.dumps(content))
    with pytest.raises(PermissionError):
        fixture.read_authorization(marker, uid=os.geteuid())


def test_duplicate_authorization_fields_refuse(tmp_path: Path) -> None:
    marker = tmp_path / "host.json"
    _authorization(marker)
    marker.write_text(marker.read_text()[:-1] + ',"synthetic_only":false}')
    with pytest.raises(ValueError, match="duplicate"):
        fixture.read_authorization(marker, uid=os.geteuid())


def test_unmatched_authorization_stops_before_any_host_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "host.json"
    _authorization(marker)
    monkeypatch.setattr(fixture, "MARKER", marker)
    monkeypatch.setattr(fixture.os, "geteuid", lambda: 0)
    # This is a parser/gate unit test. It deliberately never supplies a valid
    # host authorization or substitutes an end-to-end observation.
    reader = fixture.read_authorization
    monkeypatch.setattr(fixture, "read_authorization", lambda path: reader(path, uid=marker.stat().st_uid))

    def unexpected(*_: object, **__: object) -> object:
        raise AssertionError("unauthorized fixture attempted a host command")

    monkeypatch.setattr(fixture.subprocess, "run", unexpected)
    with pytest.raises(PermissionError, match="does not match"):
        fixture.require_disposable_host("another-authorization")


def test_quarantine_requires_both_exact_image_and_source_before_host_probe() -> None:
    with pytest.raises(ValueError, match="selected together"):
        fixture.run_fixture(authorization_id="fixture", ordinary_image="sha256:" + "a" * 64, caddy_image="sha256:" + "b" * 64, quarantine_image="sha256:" + "c" * 64)


def _complete_report() -> dict[str, object]:
    return {
        "status": fixture.OBSERVED,
        "unavailable": [],
        "approved_enrollment": {},
        "watcher_systemd": {},
        "installed_bootstrap_source": {"reviewed_lock_before_launch": True},
        "installed_mount_profile": {},
        "route_manifest": {"closed_profile": True},
        "kernel_subobservation": {
            "original_cgroups_empty": True,
            "unpinned_network_task_scan_absent": True,
            "host_listener_absent": True,
            "external_namespace_pin_verified": True,
            "external_owner_fence_verified": True,
            "retained_original_network_pin": False,
        },
    }


def test_result_oracle_requires_full_positive_components() -> None:
    complete = _complete_report()
    assert fixture.assert_report(complete, 0, accepted=True) is complete
    for component in ("approved_enrollment", "watcher_systemd", "installed_bootstrap_source", "installed_mount_profile", "route_manifest", "kernel_subobservation"):
        changed = copy.deepcopy(complete)
        changed[component] = None
        with pytest.raises(AssertionError, match="missing a required component"):
            fixture.assert_report(changed, 0, accepted=True)


@pytest.mark.parametrize("predicate", ["original_cgroups_empty", "unpinned_network_task_scan_absent", "host_listener_absent", "external_namespace_pin_verified", "external_owner_fence_verified"])
def test_result_oracle_refuses_missing_kernel_predicate(predicate: str) -> None:
    report = _complete_report()
    report["kernel_subobservation"][predicate] = False
    with pytest.raises(AssertionError, match="retained kernel facts"):
        fixture.assert_report(report, 0, accepted=True)


def test_result_oracle_does_not_treat_observer_descriptor_as_retained_authority() -> None:
    report = _complete_report()
    report["kernel_subobservation"]["retained_original_network_pin"] = True
    with pytest.raises(AssertionError, match="retained kernel facts"):
        fixture.assert_report(report, 0, accepted=True)


def test_result_oracle_requires_exit_status_and_specific_negative_evidence() -> None:
    with pytest.raises(AssertionError, match="exit code or status"):
        fixture.assert_report(_complete_report(), 2, accepted=True)
    report = {"status": "unavailable", "unavailable": [{"authority": "docker", "reason": "unavailable"}]}
    with pytest.raises(AssertionError, match="required refusal was absent"):
        fixture.assert_report(report, 2, accepted=False, authority="kernel-subcomponent")
    assert fixture.assert_report(report, 2, accepted=False, authority="docker") is report


def test_cli_requires_authorization_and_exact_local_image_arguments() -> None:
    result = subprocess.run((sys.executable, "-B", "-m", "scripts.test_management_containment_closed_host"), capture_output=True, text=True, check=False, timeout=15)
    assert result.returncode == 2
    assert "--authorization-id" in result.stderr
    assert "--ordinary-image" in result.stderr
    assert "--caddy-image" in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_rejects_incomplete_quarantine_selection_without_host_probe() -> None:
    result = subprocess.run(
        (sys.executable, "-B", "-m", "scripts.test_management_containment_closed_host", "--authorization-id", "fixture", "--ordinary-image", "sha256:" + "a" * 64, "--caddy-image", "sha256:" + "b" * 64, "--quarantine-image", "sha256:" + "c" * 64),
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 2
    assert "required together" in result.stderr
    assert "Traceback" not in result.stderr
