"""Kernel component protocol regressions; real host coverage is separate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_coder.management import containment_kernel_subobservation as kernel
from auto_coder.management import fresh_host_observation as fresh
from auto_coder.management.containment_enrollment import ApprovedEnrollment, EnrollmentObservation, OriginalNetworkPin, OriginalOwnerFencePin
from auto_coder.management.fresh_host_observation import HostObservationUnavailable, PriorContainer, PriorTask
from auto_coder.management.installed_bootstrap_mount_profile import InstalledBootstrapMountProfile


def _prior(manager: bool) -> PriorContainer:
    identity = ("a" if manager else "b") * 64
    manager_id = "a" * 64
    pid = 45001 if manager else 45002
    return PriorContainer(
        engine_id="fixture-engine",
        container_id=identity,
        image_id="sha256:" + "c" * 64,
        started_at="2026-11-11T00:00:00Z",
        restart_count=0,
        pid=pid,
        start_token=str(pid + 100),
        pid_namespace="pid:[1001]",
        net_namespace="net:[1002]",
        network_mode="none" if manager else "container:" + manager_id,
        mounts_sha256="f" * 64,
        cgroup_path="/system.slice/docker-" + identity + ".scope",
        cgroup_device=1,
        cgroup_inode=2,
        tasks=(PriorTask(pid, str(pid + 100)),),
        owner_lock_device=1 if manager else 0,
        owner_lock_inode=4 if manager else 0,
        published_port=30771 if manager else 0,
    )


def _enrolled() -> EnrollmentObservation:
    approved = ApprovedEnrollment(
        "approved-current-containment-enrollment-v1",
        "fixture-deployment",
        "fixture-enrollment",
        "approved-fixture",
        "ordinary-v1",
        "fixture-engine",
        "a" * 64,
        "sha256:" + "c" * 64,
        "b" * 64,
        "sha256:" + "c" * 64,
        (),
        "/private/fixture/original.json",
        "d" * 64,
        True,
    )
    return EnrollmentObservation(approved, "e" * 64, 1, 2, 3, (), "d" * 64)


def _stopped_profile(profile_name: str) -> tuple[EnrollmentObservation, InstalledBootstrapMountProfile, PriorContainer, PriorContainer, dict[str, dict[str, object]]]:
    enrolled = _enrolled()
    enrolled = replace(enrolled, enrollment=replace(enrolled.enrollment, profile=profile_name))
    manager = replace(_prior(True), bootstrap_sha256="1" * 64)
    proxy = _prior(False)
    inspections: dict[str, dict[str, object]] = {}
    for prior in (manager, proxy):
        inspections[prior.container_id] = {
            "Id": prior.container_id,
            "Image": prior.image_id,
            "State": {"Running": False, "Pid": 0, "Status": "exited", "StartedAt": prior.started_at},
            "RestartCount": prior.restart_count,
            "HostConfig": {"NetworkMode": prior.network_mode, "SecurityOpt": ["no-new-privileges"] if profile_name == "quarantined-original-v1" and prior == manager else None},
            "Mounts": [{"Type": "bind", "Source": "/fixture/config.json", "Destination": "/bootstrap/config.json" if profile_name == "quarantined-original-v1" else "/bootstrap", "RW": False}],
        }
    profile = InstalledBootstrapMountProfile(
        manager.container_id,
        proxy.container_id,
        profile_name,
        kernel._profile_digest([inspections[manager.container_id]]),
        kernel._profile_digest([inspections[proxy.container_id]]),
        manager.mounts_sha256,
        proxy.mounts_sha256,
        manager.bootstrap_sha256,
        "2" * 64,
        "/bootstrap/config.json",
        "/bootstrap/management.yml",
        manager.owner_lock_device,
        manager.owner_lock_inode,
    )
    return enrolled, profile, manager, proxy, inspections


@pytest.mark.parametrize("profile_name", ("ordinary-v1", "quarantined-original-v1"))
def test_validated_original_profile_reuses_kernel_absence_without_replacement_admission(monkeypatch: pytest.MonkeyPatch, profile_name: str) -> None:
    enrolled, profile, manager, proxy, inspections = _stopped_profile(profile_name)
    checked: list[str] = []
    monkeypatch.setattr(kernel, "_engine_id", lambda: manager.engine_id)
    monkeypatch.setattr(kernel, "_profile_inspect", lambda container: inspections[container])
    monkeypatch.setattr(kernel, "assert_prior_processes_absent", lambda prior: checked.append(prior.container_id))
    kernel._assert_profile_stack_stopped(enrolled, profile, manager, proxy)
    assert checked == [manager.container_id, proxy.container_id]
    changed = dict(inspections[manager.container_id])
    changed["Mounts"] = []
    inspections[manager.container_id] = changed
    with pytest.raises(HostObservationUnavailable, match="Docker profile changed"):
        kernel._assert_profile_stack_stopped(enrolled, profile, manager, proxy)
    assert checked == [manager.container_id, proxy.container_id]


@pytest.mark.parametrize("field,value", (("Running", True), ("Pid", 12345), ("Status", "created"), ("StartedAt", "2026-11-12T00:00:00Z")))
def test_original_profile_cannot_replace_captured_stopped_lifetime(monkeypatch: pytest.MonkeyPatch, field: str, value: object) -> None:
    enrolled, profile, manager, proxy, inspections = _stopped_profile("quarantined-original-v1")
    state = inspections[manager.container_id]["State"]
    assert isinstance(state, dict)
    state[field] = value
    profile = replace(profile, management_inspection_sha256=kernel._profile_digest([inspections[manager.container_id]]))
    monkeypatch.setattr(kernel, "_engine_id", lambda: manager.engine_id)
    monkeypatch.setattr(kernel, "_profile_inspect", lambda container: inspections[container])
    with pytest.raises(HostObservationUnavailable, match="exact stopped lifetime"):
        kernel._assert_profile_stack_stopped(enrolled, profile, manager, proxy)


def test_original_profile_is_bound_to_enrollment_owner_and_captured_mounts(monkeypatch: pytest.MonkeyPatch) -> None:
    enrolled, profile, manager, proxy, _ = _stopped_profile("quarantined-original-v1")
    for changed in (
        replace(profile, profile="ordinary-v1"),
        replace(profile, management_id="f" * 64),
        replace(profile, original_owner_lock_inode=profile.original_owner_lock_inode + 1),
        replace(profile, management_original_mounts_sha256="0" * 64),
        replace(profile, bootstrap_config_sha256="0" * 64),
    ):
        with pytest.raises(HostObservationUnavailable, match="lifetime scope differ"):
            kernel._assert_profile_stack_stopped(enrolled, changed, manager, proxy)


def test_quarantined_security_option_remains_forbidden_for_fresh_replacement_observer(monkeypatch: pytest.MonkeyPatch) -> None:
    prior = _prior(True)
    values: list[object] = [prior.container_id, prior.image_id, False, 0, "exited", False, "", None, ["no-new-privileges"], prior.network_mode, None, [], True, prior.started_at, prior.restart_count]
    monkeypatch.setattr(fresh, "_engine_id", lambda: prior.engine_id)
    monkeypatch.setattr(fresh, "_inspect", lambda _: values)
    with pytest.raises(HostObservationUnavailable, match="prior containment changed"):
        fresh.assert_prior_container_stopped(prior)
    values[2:5] = [True, prior.pid, "running"]
    with pytest.raises(HostObservationUnavailable, match="unsupported prior container containment"):
        fresh.capture_prior_container(prior.container_id, prior.image_id)


def test_quarantined_live_capture_refuses_unreviewed_source_before_reading_kernel_lifetimes(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _prior(True)
    current: dict[str, object] = {
        "Id": manager.container_id,
        "Image": manager.image_id,
        "State": {"Running": True, "Pid": manager.pid, "Status": "running", "StartedAt": manager.started_at},
        "RestartCount": 0,
        "HostConfig": {"Privileged": False, "PidMode": "", "CapAdd": [], "SecurityOpt": ["no-new-privileges"], "ReadonlyRootfs": True, "Devices": [], "NetworkMode": "none"},
        "Config": {
            "Entrypoint": ["python"],
            "Cmd": ["-m", "auto_coder.management.bootstrap_supervisor", "--config", "/bootstrap/config.json", "--fresh-initialization"],
            "WorkingDir": "/workspace",
            "User": "",
            "Healthcheck": None,
        },
        "Mounts": [
            {"Type": "bind", "Source": "/fixture" + destination, "Destination": destination, "RW": writable}
            for destination, writable in (
                ("/bootstrap/config.json", False),
                ("/bootstrap/management.yml", False),
                ("/management-source", False),
                ("/management-versions", False),
                ("/management-child-state", True),
                ("/management-oauth", True),
            )
        ],
    }
    monkeypatch.setattr(kernel.os, "geteuid", lambda: 0)
    monkeypatch.setattr(kernel.os, "readlink", lambda _: "pid:[4026531836]")
    monkeypatch.setattr(kernel, "_validate_host_proc_profile", lambda *_: None)
    monkeypatch.setattr(kernel, "_engine_id", lambda: manager.engine_id)
    monkeypatch.setattr(kernel, "_profile_inspect", lambda _: current)
    monkeypatch.setattr(kernel, "_protected", lambda *_, **_kwargs: (b"config", SimpleNamespace()))
    monkeypatch.setattr(kernel, "_copy_source", lambda _: b"unreviewed bootstrap source")

    def unexpected_kernel(*_args: object, **_kwargs: object) -> PriorContainer:
        raise AssertionError("unreviewed source must not reach live kernel identity capture")

    monkeypatch.setattr(kernel, "_capture_prior_kernel", unexpected_kernel)
    with pytest.raises(HostObservationUnavailable, match="bootstrap source is not reviewed"):
        kernel.capture_quarantined_original_container(manager.container_id, manager.image_id, expected_mounts=kernel._profile_mounts(current), bootstrap_config=Path("/fixture/bootstrap/config.json"))
    host = current["HostConfig"]
    assert isinstance(host, dict)
    host["SecurityOpt"] = None
    with pytest.raises(HostObservationUnavailable, match="command or security profile differs"):
        kernel.capture_quarantined_original_container(manager.container_id, manager.image_id, expected_mounts=kernel._profile_mounts(current), bootstrap_config=Path("/fixture/bootstrap/config.json"))


def test_shared_kernel_absence_rejects_real_live_lifetime_then_accepts_its_exit() -> None:
    process = subprocess.Popen((sys.executable, "-c", "import sys; sys.stdin.read(1)"), stdin=subprocess.PIPE, text=True)
    try:
        token = Path(f"/proc/{process.pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()[19]
        prior = replace(_prior(True), pid=process.pid, start_token=token, tasks=(PriorTask(process.pid, token),))
        with pytest.raises(HostObservationUnavailable, match="prior process absence unavailable"):
            fresh.assert_prior_processes_absent(prior)
        process.communicate(input="q", timeout=5)
        fresh.assert_prior_processes_absent(prior)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_kernel_subobservation_refuses_non_host_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kernel.os, "geteuid", lambda: 1000)
    with pytest.raises(HostObservationUnavailable, match="host-root"):
        kernel.observe_kernel_subcomponent(_enrolled())


def test_kernel_subobservation_rechecks_protected_lifetimes_and_never_claims_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    enrollment = _enrolled()
    manager, proxy = _prior(True), _prior(False)
    evidence = b"protected original lifetime fixture"
    identity = SimpleNamespace(st_dev=1, st_ino=2, st_size=len(evidence), st_mtime_ns=4, st_ctime_ns=5)
    checks: list[object] = []
    monkeypatch.setattr(kernel.os, "geteuid", lambda: 0)
    monkeypatch.setattr(kernel.os, "readlink", lambda _: "pid:[4026531836]")
    monkeypatch.setattr(kernel, "_protected", lambda path, **_kwargs: (evidence, identity))
    monkeypatch.setattr(kernel, "_evidence", lambda path: (manager, proxy, enrollment.original_prior_evidence_sha256))
    monkeypatch.setattr(kernel, "_owner_lock", lambda prior: (1, 4, 5, 6))
    monkeypatch.setattr(kernel, "assert_known_prior_stack_stopped", lambda m, p: checks.append("stopped"))
    monkeypatch.setattr(kernel, "assert_prior_network_unreferenced", lambda ns: checks.append(ns))
    monkeypatch.setattr(kernel, "assert_host_listener_absent", lambda port: checks.append(port))
    report = kernel.observe_kernel_subcomponent(enrollment)
    assert (report.management_id, report.proxy_id) == (manager.container_id, proxy.container_id)
    assert report.original_cgroups_empty and report.unpinned_network_task_scan_absent and report.host_listener_absent
    assert report.retained_original_network_pin is False
    assert checks == ["stopped", manager.net_namespace, manager.published_port, "stopped"]
    assert set(asdict(report)) >= {"boot_id", "owner_lock_inode", "started_monotonic_ns", "finished_monotonic_ns"}


def test_kernel_subobservation_rejects_prior_file_swap_after_task_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    enrolled = _enrolled()
    manager, proxy = _prior(True), _prior(False)
    original = b"original"
    reads = 0

    def protected(path: Path, **_kwargs: object) -> tuple[bytes, SimpleNamespace]:
        nonlocal reads
        reads += 1
        content = original if reads == 1 else b"substituted"
        return content, SimpleNamespace(st_dev=1, st_ino=2, st_size=len(content), st_mtime_ns=4, st_ctime_ns=5)

    monkeypatch.setattr(kernel.os, "geteuid", lambda: 0)
    monkeypatch.setattr(kernel.os, "readlink", lambda _: "pid:[4026531836]")
    monkeypatch.setattr(kernel, "_protected", protected)
    monkeypatch.setattr(kernel, "_evidence", lambda _: (manager, proxy, enrolled.original_prior_evidence_sha256))
    monkeypatch.setattr(kernel, "_owner_lock", lambda _: (1, 4, 5, 6))
    monkeypatch.setattr(kernel, "assert_known_prior_stack_stopped", lambda *_: None)
    monkeypatch.setattr(kernel, "assert_prior_network_unreferenced", lambda *_: None)
    monkeypatch.setattr(kernel, "assert_host_listener_absent", lambda *_: None)
    with pytest.raises(HostObservationUnavailable, match="changed during kernel"):
        kernel.observe_kernel_subcomponent(enrolled)
    assert reads == 2


def test_kernel_entrypoint_reobserves_validated_quarantined_original_without_fresh_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    enrolled, profile, manager, proxy, inspections = _stopped_profile("quarantined-original-v1")
    content = b"protected original quarantined lifetime fixture"
    identity = SimpleNamespace(st_dev=1, st_ino=2, st_size=len(content), st_mtime_ns=4, st_ctime_ns=5)
    checked: list[str] = []
    monkeypatch.setattr(kernel.os, "geteuid", lambda: 0)
    monkeypatch.setattr(kernel.os, "readlink", lambda _: "pid:[4026531836]")
    monkeypatch.setattr(kernel, "_protected", lambda *_, **_kwargs: (content, identity))
    monkeypatch.setattr(kernel, "_evidence", lambda _: (manager, proxy, enrolled.original_prior_evidence_sha256))
    monkeypatch.setattr(kernel, "_owner_lock", lambda _: (manager.owner_lock_device, manager.owner_lock_inode, 5, 6))
    monkeypatch.setattr(kernel, "_engine_id", lambda: manager.engine_id)
    monkeypatch.setattr(kernel, "_profile_inspect", lambda container: inspections[container])
    monkeypatch.setattr(kernel, "assert_prior_processes_absent", lambda prior: checked.append(prior.container_id))
    monkeypatch.setattr(kernel, "assert_prior_network_unreferenced", lambda _: checked.append("network-absent"))
    monkeypatch.setattr(kernel, "assert_host_listener_absent", lambda _: checked.append("listener-absent"))
    report = kernel.observe_kernel_subcomponent(enrolled, installed_profile=profile)
    assert checked == [manager.container_id, proxy.container_id, "network-absent", "listener-absent", manager.container_id, proxy.container_id]
    assert report.installed_profile == "quarantined-original-v1"
    assert report.management_inspection_sha256 == profile.management_inspection_sha256
    assert report.proxy_inspection_sha256 == profile.proxy_inspection_sha256
    assert report.external_namespace_pin_verified is False
    assert report.external_owner_fence_verified is False


def test_real_live_namespace_fd_is_independently_opened_then_released() -> None:
    original = os.open("/proc/self/ns/net", os.O_RDONLY | os.O_CLOEXEC)
    try:
        link = os.readlink(f"/proc/self/fd/{original}")
        info = os.fstat(original)
        token = Path("/proc/self/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()[19]
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        pin = OriginalNetworkPin(os.getpid(), token, boot, original, link, info.st_dev, info.st_ino)
        with kernel._external_namespace_pin(pin, link):
            assert os.readlink(f"/proc/self/fd/{original}") == link
        with pytest.raises(HostObservationUnavailable, match="namespace pin holder lifetime"):
            with kernel._external_namespace_pin(OriginalNetworkPin(os.getpid(), str(int(token) + 1), boot, original, link, info.st_dev, info.st_ino), link):
                pass
        with pytest.raises(HostObservationUnavailable, match="does not match"):
            with kernel._external_namespace_pin(pin, "net:[999999999999]"):
                pass
    finally:
        os.close(original)
    with pytest.raises(HostObservationUnavailable, match="namespace pin"):
        with kernel._external_namespace_pin(pin, link):
            pass


def test_real_external_owner_flock_requires_a_distinct_live_holder(tmp_path: Path) -> None:
    lock = tmp_path / "original-owner.lock"
    lock.write_bytes(b"")
    holder_code = "\n".join(
        (
            "import fcntl, json, os, sys",
            "fd = os.open(sys.argv[1], os.O_RDWR | os.O_NOFOLLOW)",
            "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)",
            "info = os.fstat(fd)",
            "token = open('/proc/self/stat', encoding='ascii').read().rsplit(')', 1)[1].split()[19]",
            "boot = open('/proc/sys/kernel/random/boot_id', encoding='ascii').read().strip()",
            "print(json.dumps({'pid':os.getpid(), 'fd':fd, 'token':token, 'boot':boot, 'device':info.st_dev, 'inode':info.st_ino}), flush=True)",
            "sys.stdin.read(1)",
            "fcntl.flock(fd, fcntl.LOCK_UN)",
            "print('released', flush=True)",
            "sys.stdin.read(1)",
        )
    )
    process = subprocess.Popen((sys.executable, "-c", holder_code, str(lock)), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None and process.stdin is not None
        data = json.loads(process.stdout.readline())
        pin = OriginalOwnerFencePin(data["pid"], data["token"], data["boot"], data["fd"], data["device"], data["inode"])
        prior = replace(_prior(True), owner_lock_device=data["device"], owner_lock_inode=data["inode"])
        with kernel._external_owner_fence(pin, prior):
            assert process.poll() is None
        with pytest.raises(HostObservationUnavailable, match="independent exact-file"):
            with kernel._external_owner_fence(replace(pin, holder_pid=os.getpid()), prior):
                pass
        with pytest.raises(HostObservationUnavailable, match="holder lifetime"):
            with kernel._external_owner_fence(replace(pin, holder_start_token=str(int(pin.holder_start_token) + 1)), prior):
                pass
        with pytest.raises(HostObservationUnavailable, match="independent exact-file"):
            with kernel._external_owner_fence(replace(pin, inode=pin.inode + 1), prior):
                pass
        with pytest.raises(HostObservationUnavailable, match="does not hold the old owner fence"):
            with kernel._external_owner_fence(pin, prior):
                process.stdin.write("r")
                process.stdin.flush()
                assert process.stdout.readline().strip() == "released"
    finally:
        if process.stdin is not None and process.poll() is None:
            process.stdin.write("q")
            process.stdin.flush()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
    with pytest.raises(HostObservationUnavailable, match="external owner fence"):
        with kernel._external_owner_fence(pin, prior):
            pass


def _caddy_live_inspection(tmp_path: Path) -> tuple[PriorContainer, tuple[tuple[str, str, bool], ...], dict[str, object], list[object]]:
    prior = _prior(False)
    paths = (tmp_path / "Caddyfile", tmp_path / "data", tmp_path / "config")
    paths[0].write_bytes(b"admin off\n")
    paths[0].chmod(0o600)
    for path in paths[1:]:
        path.mkdir(mode=0o700)
    mounts = tuple(sorted(((str(paths[0]), "/etc/caddy/Caddyfile", False), (str(paths[1]), "/data", True), (str(paths[2]), "/config", True))))
    inspection = {
        "Id": prior.container_id,
        "Image": prior.image_id,
        "State": {"Running": True, "Pid": prior.pid, "Status": "running", "StartedAt": prior.started_at},
        "RestartCount": prior.restart_count,
        "HostConfig": {"Privileged": False, "PidMode": "", "CapAdd": None, "SecurityOpt": None, "Devices": [], "NetworkMode": prior.network_mode},
        "Config": {"Entrypoint": ["caddy"], "Cmd": ["run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"], "WorkingDir": "/srv", "User": ""},
        "Mounts": [{"Type": "bind", "Source": source, "Destination": destination, "RW": writable} for source, destination, writable in mounts],
    }
    values: list[object] = [prior.container_id, prior.image_id, True, prior.pid, "running", False, "", None, None, prior.network_mode, [], inspection["Mounts"], False, prior.started_at, prior.restart_count, inspection["Config"]["Entrypoint"], inspection["Config"]["Cmd"], {}, {}]
    return prior, mounts, inspection, values


def test_original_caddy_capture_binds_real_profile_without_fresh_mount_relaxation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prior, mounts, inspection, values = _caddy_live_inspection(tmp_path)
    checks: list[tuple[object, ...]] = []
    monkeypatch.setattr(kernel, "_require_capture_host", lambda: None)
    monkeypatch.setattr(kernel, "_engine_id", lambda: prior.engine_id)
    monkeypatch.setattr(kernel, "_profile_inspect", lambda _: inspection)
    monkeypatch.setattr(kernel, "_inspect", lambda _: values)
    monkeypatch.setattr(kernel, "_protected", lambda path, **_: (path.read_bytes(), path.stat()))

    def captured(container, image, engine, observed, observed_mounts, *, require_owner_lock):
        checks.append((container, image, engine, observed, observed_mounts, require_owner_lock))
        return prior

    monkeypatch.setattr(kernel, "_capture_prior_kernel", captured)
    assert kernel.capture_original_caddy_container(prior.container_id, prior.image_id, expected_mounts=mounts) == prior
    assert checks == [(prior.container_id, prior.image_id, prior.engine_id, values, mounts, False)]
    monkeypatch.setattr(fresh, "_engine_id", lambda: prior.engine_id)
    monkeypatch.setattr(fresh, "_inspect", lambda _: values)
    with pytest.raises(HostObservationUnavailable, match="mount boundary"):
        fresh.capture_prior_container(prior.container_id, prior.image_id, expected_mounts=mounts)


@pytest.mark.parametrize("changed", ("command", "network", "writable-config"))
def test_original_caddy_capture_rejects_unreviewed_effects_before_kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str) -> None:
    prior, mounts, inspection, _ = _caddy_live_inspection(tmp_path)
    if changed == "command":
        inspection["Config"]["Cmd"] = ["sleep", "3600"]
    elif changed == "network":
        inspection["HostConfig"]["NetworkMode"] = "host"
    else:
        for mount in inspection["Mounts"]:
            if mount["Destination"] == "/etc/caddy/Caddyfile":
                mount["RW"] = True
    monkeypatch.setattr(kernel, "_require_capture_host", lambda: None)
    monkeypatch.setattr(kernel, "_engine_id", lambda: prior.engine_id)
    monkeypatch.setattr(kernel, "_profile_inspect", lambda _: inspection)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("unsupported Caddy configuration reached kernel capture")

    monkeypatch.setattr(kernel, "_capture_prior_kernel", unexpected)
    with pytest.raises(HostObservationUnavailable, match="original Caddy"):
        kernel.capture_original_caddy_container(prior.container_id, prior.image_id, expected_mounts=mounts)
