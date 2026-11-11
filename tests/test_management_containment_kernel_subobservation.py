"""Kernel component protocol regressions; real host coverage is separate."""

from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_coder.management import containment_kernel_subobservation as kernel
from auto_coder.management.containment_enrollment import ApprovedEnrollment, EnrollmentObservation
from auto_coder.management.fresh_host_observation import HostObservationUnavailable, PriorContainer, PriorTask


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
