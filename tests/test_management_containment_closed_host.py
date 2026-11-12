"""Unit checks for VM authorization and full-host result oracles.

These tests never run Docker/systemd or claim the full host acceptance passed.
The maintained VM driver itself uses actual production CLI processes and kernel
controls; no synthetic observation is supplied to that path.
"""

from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import containment_quarantine_fixture as quarantine
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


def test_quarantine_negative_command_is_one_bounded_real_interpreter_change() -> None:
    ordinary = quarantine.quarantine_command()
    assert ordinary == ("-m", "auto_coder.management.bootstrap_supervisor", "--config", "/bootstrap/config.json", "--fresh-initialization")
    assert quarantine.quarantine_command("effective-command") == ("-B", *ordinary)
    assert quarantine.quarantine_command("shadow-support-mount") == ordinary
    assert quarantine.quarantine_command("writable-support-mount") == ordinary
    with pytest.raises(ValueError, match="unknown quarantined-original fixture variant"):
        quarantine.quarantine_command("arbitrary-command")


def test_independent_consumer_oracle_requires_another_real_process_identity() -> None:
    first: dict[str, object] = {"observer_pid": 20, "observer_start_token": "100", "observation_id": "first", "finished_monotonic_ns": 200}
    second: dict[str, object] = {"observer_pid": 21, "observer_start_token": "101", "observation_id": "second", "started_monotonic_ns": 201}
    fixture._assert_independent(first, second)
    second.update(observer_pid=20, observer_start_token="100")
    with pytest.raises(AssertionError, match="did not independently inspect"):
        fixture._assert_independent(first, second)
    second.update(observer_pid=21, observer_start_token="101", started_monotonic_ns=199)
    with pytest.raises(AssertionError, match="intervals or identities overlap"):
        fixture._assert_independent(first, second)


@pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")
def test_preservation_oracle_detects_original_record_mutation_and_worker_death(tmp_path: Path) -> None:
    record = tmp_path / "original-record.json"
    record.write_bytes(b'{"unchanged":true}')
    worker = subprocess.Popen((sys.executable, "-I", "-B", "-c", "import signal; signal.pause()"), text=True)
    try:
        before = fixture._footprint((record,), worker)
        assert fixture._footprint((record,), worker) == before
        record.write_bytes(b'{"unchanged":false}')
        assert fixture._footprint((record,), worker) != before
        worker.terminate()
        worker.wait(timeout=5)
        with pytest.raises(AssertionError, match="preserved worker exited"):
            fixture._footprint((record,), worker)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)


def test_caddy_creation_keeps_exact_id_for_cleanup_when_start_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created, image = "c" * 64, "sha256:" + "d" * 64
    case = SimpleNamespace(root=tmp_path, fixture_id="ac-test-pr2829-" + "a" * 32, manager="b" * 64, manager_image="sha256:" + "e" * 64, proxy="", watcher=None, routes=None, owns_manager=False, namespace_fd=-1)
    calls: list[tuple[str, ...]] = []
    failure = subprocess.CalledProcessError(127, ("docker", "start", created), stderr="fixture startup failed")

    def docker(*arguments: str, timeout: int = 30) -> str:
        calls.append(arguments)
        if arguments[0] == "create":
            assert arguments[arguments.index("--entrypoint") + 1] == "caddy"
            assert arguments[arguments.index(image) + 1 :] == ("run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile")
            return created
        if arguments[0] == "start":
            assert case.proxy == created
            raise failure
        assert arguments == ("rm", "-f", created)
        return created

    monkeypatch.setattr(fixture, "_docker", docker)
    monkeypatch.setattr(fixture, "APPROVAL_ROOT", tmp_path / "approvals")
    with pytest.raises(subprocess.CalledProcessError) as raised:
        fixture._close_and_approve(case, image, "fixture-authorization")
    assert raised.value is failure
    assert case.proxy == created
    fixture.Case.cleanup(case)
    assert [arguments[0] for arguments in calls] == ["create", "start", "rm"]
    assert case.proxy == ""


def _stock_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, inhibited: bool = True) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(fixture, "STOCK_MASK_ROOT", tmp_path)
    monkeypatch.setattr(fixture, "_systemd_catalog", lambda: fixture.STOCK_UNITS)
    monkeypatch.setattr(fixture, "_systemctl", lambda *args: calls.append(args))

    def unit(name: str) -> fixture.SystemdRouteFact:
        if not calls:
            return fixture.SystemdRouteFact(name, "b" * 64, "static" if name.endswith(".service") else "enabled", "inactive" if name.endswith(".service") else "active", 0, False)
        assert calls[:2] == [("stop", *fixture.STOCK_UNITS), ("daemon-reload",)]
        assert (tmp_path / name).is_symlink()
        return fixture.SystemdRouteFact(name, "a" * 64, "masked", "inactive" if inhibited else "failed", 0, inhibited, needs_daemon_reload=not inhibited)

    monkeypatch.setattr(fixture, "_systemd_unit", unit)
    return calls


def test_stock_inhibition_requires_authorized_host_before_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    def unauthorized(_: str) -> dict[str, object]:
        raise PermissionError("unmatched disposable marker")

    def unexpected() -> object:
        raise AssertionError("stock mutation preceded host authorization")

    monkeypatch.setattr(fixture, "require_disposable_host", unauthorized)
    monkeypatch.setattr(fixture.InhibitedStockRoutes, "install", unexpected)
    with pytest.raises(PermissionError, match="unmatched disposable marker"):
        fixture.run_fixture(authorization_id="fixture", ordinary_image="sha256:" + "a" * 64, caddy_image="sha256:" + "b" * 64)


def test_stock_masks_are_observed_recorded_and_removed_by_exact_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stock_routes(tmp_path, monkeypatch)
    stock = fixture.InhibitedStockRoutes.install()
    evidence = stock.evidence()
    assert {entry["unit_file_state"] for entry in evidence["initial_units"]} == {"static", "enabled"}
    assert {entry["unit"] for entry in evidence["units"]} == set(fixture.STOCK_UNITS)
    assert all(entry["inhibited"] for entry in evidence["units"])
    assert {entry["path"] for entry in evidence["masks"]} == {str(tmp_path / name) for name in fixture.STOCK_UNITS}
    assert all(entry["created_by_fixture"] for entry in evidence["masks"])
    assert all(entry["identity"][1] == Path(entry["path"]).lstat().st_ino for entry in evidence["masks"])
    assert stock.mask_paths == tuple(tmp_path / name for name in fixture.STOCK_UNITS)
    stock.cleanup()
    stock.cleanup()
    assert not list(tmp_path.iterdir())
    assert calls == [("stop", *fixture.STOCK_UNITS), ("daemon-reload",), ("daemon-reload",)]


def test_stock_active_timer_is_stopped_before_any_mask_is_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stock_routes(tmp_path, monkeypatch)
    original = fixture._systemctl

    def systemctl(*arguments: str) -> None:
        if arguments[0] == "stop":
            assert arguments == ("stop", *fixture.STOCK_UNITS)
            assert calls == []
            assert not list(tmp_path.iterdir())
        original(*arguments)

    monkeypatch.setattr(fixture, "_systemctl", systemctl)
    stock = fixture.InhibitedStockRoutes.install()
    try:
        assert next(fact for fact in stock.initial_facts if fact.unit.endswith(".timer")).active_state == "active"
        assert all(fact.inhibited for fact in stock.assert_inhibited())
    finally:
        stock.cleanup()


def test_stock_stop_failure_creates_no_masks_or_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stock_routes(tmp_path, monkeypatch)

    def failed_stop(*arguments: str) -> None:
        calls.append(arguments)
        raise AssertionError("stock stop failed")

    monkeypatch.setattr(fixture, "_systemctl", failed_stop)
    with pytest.raises(AssertionError, match="stock stop failed"):
        fixture.InhibitedStockRoutes.install()
    assert calls == [("stop", *fixture.STOCK_UNITS)]
    assert not list(tmp_path.iterdir())


def test_stock_initial_failure_is_not_reset_to_claim_inhibition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stock_routes(tmp_path, monkeypatch)

    def failed_unit(name: str) -> fixture.SystemdRouteFact:
        state = "masked" if calls else "enabled" if name.endswith(".timer") else "static"
        return fixture.SystemdRouteFact(name, "a" * 64, state, "failed", 0, False)

    monkeypatch.setattr(fixture, "_systemd_unit", failed_unit)
    with pytest.raises(AssertionError, match="not masked and inactive") as raised:
        fixture.InhibitedStockRoutes.install()
    details = json.loads(str(raised.value).split(": ", 1)[1])
    assert details["active_state"] == "failed"
    assert details["needs_daemon_reload"] is False
    assert calls == [("stop", *fixture.STOCK_UNITS), ("daemon-reload",), ("daemon-reload",)]
    assert not list(tmp_path.iterdir())


def test_preexisting_stock_mask_is_preserved_with_its_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stock_routes(tmp_path, monkeypatch)
    existing = tmp_path / fixture.STOCK_UNITS[0]
    existing.symlink_to("/dev/null")
    before = existing.lstat()
    stock = fixture.InhibitedStockRoutes.install()
    recorded = {entry["path"]: entry for entry in stock.evidence()["masks"]}
    assert recorded[str(existing)]["created_by_fixture"] is False
    stock.cleanup()
    assert existing.is_symlink()
    after = existing.lstat()
    assert (before.st_dev, before.st_ino, before.st_ctime_ns) == (after.st_dev, after.st_ino, after.st_ctime_ns)
    assert os.readlink(existing) == "/dev/null"
    assert not (tmp_path / fixture.STOCK_UNITS[1]).is_symlink()


def test_absent_stock_route_refuses_before_creating_any_mask(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stock_routes(tmp_path, monkeypatch)
    monkeypatch.setattr(fixture, "_systemd_catalog", lambda: fixture.STOCK_UNITS[:1])
    with pytest.raises(AssertionError, match="absent from the installed catalog"):
        fixture.InhibitedStockRoutes.install()
    assert calls == []
    assert not list(tmp_path.iterdir())


def test_referenced_but_uninstalled_stock_route_never_gets_a_mask(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stock_routes(tmp_path, monkeypatch)
    monkeypatch.setattr(fixture, "_systemd_unit", lambda name: fixture.SystemdRouteFact(name, "a" * 64, "", "inactive", 0, False))
    with pytest.raises(AssertionError, match="no supported installed unit state"):
        fixture.InhibitedStockRoutes.install()
    assert calls == []
    assert not list(tmp_path.iterdir())


def test_stock_source_collision_is_preserved_before_any_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stock_routes(tmp_path, monkeypatch)
    existing = tmp_path / fixture.STOCK_UNITS[1]
    existing.write_bytes(b"[Service]\nExecStart=/usr/bin/true\n")
    before = existing.lstat()
    with pytest.raises(AssertionError, match="not a protected /dev/null link"):
        fixture.InhibitedStockRoutes.install()
    assert existing.lstat() == before
    assert calls == []
    assert list(tmp_path.iterdir()) == [existing]


def test_stock_active_or_incomplete_state_rolls_back_owned_masks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stock_routes(tmp_path, monkeypatch, inhibited=False)
    with pytest.raises(AssertionError, match="not masked and inactive") as raised:
        fixture.InhibitedStockRoutes.install()
    assert json.loads(str(raised.value).split(": ", 1)[1]) == {"unit": fixture.STOCK_UNITS[0], "unit_file_state": "masked", "active_state": "failed", "main_pid": 0, "inhibited": False, "needs_daemon_reload": True}
    assert not list(tmp_path.iterdir())
    assert calls[-1] == ("daemon-reload",)


def test_stock_cleanup_refuses_replaced_mask_without_removing_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stock_routes(tmp_path, monkeypatch)
    stock = fixture.InhibitedStockRoutes.install()
    changed = stock.mask_paths[0]
    changed.rename(tmp_path / "moved-original-mask")
    changed.symlink_to("/dev/null")
    replacement = changed.lstat()
    with pytest.raises(AssertionError, match="externally replaced"):
        stock.cleanup()
    assert changed.lstat() == replacement
    assert changed.is_symlink()
    assert not stock.mask_paths[1].is_symlink()


@pytest.mark.parametrize("omitted", (None, *fixture.STOCK_UNITS))
def test_every_approval_requires_all_inhibited_stock_restarter_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, omitted: str | None) -> None:
    _stock_routes(tmp_path, monkeypatch)
    stock = fixture.InhibitedStockRoutes.install()
    names = ("fixture.service", "fixture.timer", "fixture.socket")
    case = SimpleNamespace(routes=SimpleNamespace(names=names), stock_routes=stock)
    expected = {*names, fixture.WATCHER_UNIT, *fixture.STOCK_UNITS}
    routes = [{"unit": name, "role": "unrelated", "sha256": "a" * 64} for name in sorted(expected | {"unrelated.service"}) if name != omitted]
    draft = {"systemd_routes": routes}
    try:
        if omitted is not None:
            with pytest.raises(AssertionError, match="absent from the complete catalog"):
                fixture._classify_fixture_restarter_routes(case, draft)
        else:
            fixture._classify_fixture_restarter_routes(case, draft)
            assert {route["unit"] for route in routes if route["role"] == "restarter"} == expected
            assert next(route for route in routes if route["unit"] == "unrelated.service")["role"] == "unrelated"
    finally:
        stock.cleanup()


def test_fixed_cron_job_is_installed_once_without_modifying_vendor_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fixture, "CRON_ENTRY_ROOT", tmp_path)
    vendor = tmp_path / "vendor"
    vendor.write_bytes(b"unchanged vendor schedule\n")
    before = vendor.lstat(), vendor.read_bytes()
    job = fixture.CronDispatchFixture.install("ac-test-pr2829-" + "d" * 32)
    assert job.source.read_bytes() == b"* * * * * root /usr/bin/sleep 120\n"
    assert job.source.stat().st_mode & 0o777 == 0o600
    job.assert_unchanged()
    with pytest.raises(FileExistsError):
        fixture.CronDispatchFixture.install("ac-test-pr2829-" + "d" * 32)
    job.cleanup()
    assert (vendor.lstat(), vendor.read_bytes()) == before
    assert list(tmp_path.iterdir()) == [vendor]


def test_cron_job_cleanup_refuses_replacement_or_in_place_source_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fixture, "CRON_ENTRY_ROOT", tmp_path)
    job = fixture.CronDispatchFixture.install("ac-test-pr2829-" + "e" * 32)
    job.source.write_bytes(b"* * * * * root /usr/bin/sleep 121\n")
    changed = job.source.lstat()
    with pytest.raises(AssertionError, match="externally replaced"):
        job.cleanup()
    assert job.source.lstat() == changed


def test_cron_task_refusal_oracle_requires_both_actual_reasons() -> None:
    expected = "inhibited stock cron scope retains tasks"
    refusal = {"unavailable": [{"authority": "approved-route-manifest", "reason": expected}]}
    fixture._require_refusal_reason((refusal, copy.deepcopy(refusal)), expected)
    for changed in (
        {"unavailable": [{"authority": "approved-route-manifest", "reason": "unknown, omitted or newly installed systemd route"}]},
        {"unavailable": [{"authority": "kernel-subcomponent", "reason": expected}]},
    ):
        with pytest.raises(AssertionError, match="intended cron predicate"):
            fixture._require_refusal_reason((refusal, changed), expected)


@pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")
def test_fixture_task_pins_real_lifetime_and_refuses_wrong_start_before_signalling() -> None:
    child = subprocess.Popen(("/usr/bin/sleep", "120"), text=True)
    task = fixture.BoundFixtureTask.capture(child.pid)
    try:
        assert task.pid == child.pid and task.parent == os.getpid()
        assert task.command == ("/usr/bin/sleep", "120")
        task.send(signal.SIGSTOP)
        deadline = time.monotonic() + 3
        while Path(f"/proc/{child.pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "T":
            if time.monotonic() >= deadline:
                raise AssertionError("the real owned task did not stop")
            time.sleep(0.01)
        wrong = replace(task, start_token=str(int(task.start_token) + 1))
        with pytest.raises(AssertionError, match="lifetime or cgroup changed"):
            wrong.send(signal.SIGKILL)
        assert child.poll() is None
        task.send(signal.SIGKILL)
        assert child.wait(timeout=5) == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        task.close()


def _cron_approval_mask_change(tmp_path: Path) -> tuple[dict[str, object], dict[str, object], Path]:
    mask = tmp_path / "cron.service"
    mask.symlink_to("/dev/null")
    info = mask.lstat()
    observed_identity = [info.st_dev, info.st_ino, info.st_ctime_ns, info.st_mode, info.st_uid, info.st_gid]
    proof = {
        "scheduler_unit": "cron.service",
        "scheduler_sha256": "a" * 64,
        "inhibited_services": [["cron.service", "a" * 64], ["fixture.service", "f" * 64]],
        "mask_path": str(mask),
        "mask_identity": [1, 2, 3, info.st_mode, info.st_uid, info.st_gid],
        "sources": [{"sha256": "unchanged"}],
        "cgroups": {"missing_roots": ["/system.slice/cron.service"]},
    }
    prior = {"cron_entries": [{"path": "/etc/crontab", "sha256": "original raw source", "inhibited_source": proof}]}
    current = copy.deepcopy(prior)
    current["cron_entries"][0]["inhibited_source"].update(scheduler_sha256="b" * 64, mask_identity=observed_identity)
    current["cron_entries"][0]["inhibited_source"]["inhibited_services"][0][1] = "b" * 64
    return prior, current, mask


def test_cron_reapproval_allows_only_actual_recreated_mask_fields(tmp_path: Path) -> None:
    prior, current, mask = _cron_approval_mask_change(tmp_path)
    fixture._allow_restored_cron_proofs(prior, current, old_digest="a" * 64, new_digest="b" * 64, mask=mask)
    assert prior == current


@pytest.mark.parametrize("change", ("source", "scope", "raw", "mask", "scheduler", "another-controller", "added-controller"))
def test_cron_reapproval_refuses_other_authority_changes(tmp_path: Path, change: str) -> None:
    prior, current, mask = _cron_approval_mask_change(tmp_path)
    entry = current["cron_entries"][0]
    proof = entry["inhibited_source"]
    if change == "source":
        proof["sources"][0]["sha256"] = "substituted"
    elif change == "scope":
        proof["cgroups"]["missing_roots"] = []
    elif change == "raw":
        entry["sha256"] = "substituted raw cron"
    elif change == "mask":
        proof["mask_identity"][1] += 1
    elif change == "scheduler":
        proof["scheduler_sha256"] = "c" * 64
    elif change == "another-controller":
        proof["inhibited_services"][1][1] = "e" * 64
    else:
        proof["inhibited_services"].append(["new-controller.service", "c" * 64])
    with pytest.raises(AssertionError, match="(another cron source or task authority|actual new mask)"):
        fixture._allow_restored_cron_proofs(prior, current, old_digest="a" * 64, new_digest="b" * 64, mask=mask)


@pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")
def test_preservation_oracle_binds_deliberately_absent_mask(tmp_path: Path) -> None:
    mask = tmp_path / "cron.service"
    worker = subprocess.Popen((sys.executable, "-I", "-B", "-c", "import signal; signal.pause()"), text=True)
    try:
        before = fixture._footprint((mask,), worker)
        assert fixture._footprint((mask,), worker) == before
        mask.symlink_to("/dev/null")
        assert fixture._footprint((mask,), worker) != before
    finally:
        worker.terminate()
        worker.wait(timeout=5)


def test_generic_controller_remask_updates_only_its_bound_pair(tmp_path: Path) -> None:
    prior, _, cron_mask = _cron_approval_mask_change(tmp_path)
    mask = tmp_path / "fixture.service"
    mask.symlink_to("/dev/null")
    current = copy.deepcopy(prior)
    current["cron_entries"][0]["inhibited_source"]["inhibited_services"][1][1] = "g" * 64
    unchanged_scheduler = current["cron_entries"][0]["inhibited_source"]["scheduler_sha256"]
    unchanged_mask = copy.deepcopy(current["cron_entries"][0]["inhibited_source"]["mask_identity"])
    fixture._allow_restored_cron_proofs(prior, current, old_digest="f" * 64, new_digest="g" * 64, mask=mask, target="fixture.service")
    assert prior == current
    assert current["cron_entries"][0]["inhibited_source"]["scheduler_sha256"] == unchanged_scheduler
    assert current["cron_entries"][0]["inhibited_source"]["mask_identity"] == unchanged_mask
    assert cron_mask.is_symlink()


def _custom_cron_controller_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, list[tuple[str, ...]]]:
    sources, masks = tmp_path / "sources", tmp_path / "masks"
    sources.mkdir()
    masks.mkdir()
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(fixture, "CUSTOM_UNIT_ROOT", sources)
    monkeypatch.setattr(fixture, "STOCK_MASK_ROOT", masks)
    monkeypatch.setattr(fixture, "_systemctl", lambda *args: calls.append(args))
    monkeypatch.setattr(fixture, "_systemd_unit", lambda name: fixture.SystemdRouteFact(name, "a" * 64, "masked", "inactive", 0, True))
    return sources, masks, calls


def test_custom_cron_controller_is_real_owned_source_with_unchanged_mask(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources, masks, calls = _custom_cron_controller_roots(tmp_path, monkeypatch)
    controller = fixture.MaskedCronController.install("ac-test-pr2829-" + "f" * 32)
    assert controller.source.parent == sources and controller.mask.parent == masks
    assert "ExecStart=/usr/sbin/cron -f -P\nKillMode=process\n" in controller.source.read_text()
    before = controller.mask.lstat()
    controller.assert_inhibited()
    assert controller.mask.lstat() == before
    controller.cleanup()
    assert not list(sources.iterdir()) and not list(masks.iterdir())
    assert calls == [("daemon-reload",), ("daemon-reload",)]


def test_custom_cron_controller_mask_collision_preserves_preexisting_object(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources, masks, _ = _custom_cron_controller_roots(tmp_path, monkeypatch)
    name = "ac-test-pr2829-" + "f" * 32
    preexisting = masks / (name + "-custom-cron.service")
    preexisting.symlink_to("/dev/null")
    before = preexisting.lstat()
    with pytest.raises(FileExistsError):
        fixture.MaskedCronController.install(name)
    assert preexisting.lstat() == before
    assert not list(sources.iterdir())


def test_custom_cron_cleanup_refuses_replaced_mask(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, masks, _ = _custom_cron_controller_roots(tmp_path, monkeypatch)
    controller = fixture.MaskedCronController.install("ac-test-pr2829-" + "f" * 32)
    controller.mask.rename(masks / "original-mask")
    controller.mask.symlink_to("/dev/null")
    replacement = controller.mask.lstat()
    with pytest.raises(AssertionError, match="externally replaced"):
        controller.cleanup()
    assert controller.mask.lstat() == replacement
    assert controller.source.is_file()


def test_cron_task_fault_never_targets_an_unowned_service() -> None:
    case = SimpleNamespace(custom_cron=None)
    with pytest.raises(ValueError, match="unowned cron controller"):
        fixture._cron_task_negative(case, controller="another.service")
