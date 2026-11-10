"""Unit-level protected-enrollment reader tests, not positive containment tests."""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import tempfile
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_coder.management.containment_enrollment import (
    ApprovedEnrollment,
    EnrollmentUnavailable,
    OriginalFile,
    _observe_approved_enrollment,
)
from auto_coder.management.fresh_host_observation import PriorContainer, PriorTask


def _protected_file(path: Path, content: bytes) -> OriginalFile:
    path.write_bytes(content)
    path.chmod(0o600)
    info = path.stat()
    return OriginalFile(path.stem, str(path), hashlib.sha256(content).hexdigest(), info.st_dev, info.st_ino)


class _approved_fixture:
    def __enter__(self) -> tuple[Path, ApprovedEnrollment, SimpleNamespace]:
        self.directory = tempfile.TemporaryDirectory(prefix="ac-enrollment-", dir=pwd.getpwuid(os.geteuid()).pw_dir)
        self.base = Path(self.directory.name)
        root = self.base / "approved"
        root.mkdir(mode=0o700)
        files = []
        for purpose in ("bootstrap-config", "bootstrap-source", "management-config", "proxy-config"):
            path = self.base / purpose
            content = ("original " + purpose).encode()
            binding = _protected_file(path, content)
            files.append(OriginalFile(purpose, binding.path, binding.sha256, binding.device, binding.inode))
        prior = self.base / "original-prior-evidence"
        manager_id, proxy_id = "a" * 64, "c" * 64
        manager = PriorContainer(
            engine_id="fixture-engine",
            container_id=manager_id,
            image_id="sha256:" + "b" * 64,
            started_at="2026-11-10T00:00:00Z",
            restart_count=0,
            pid=12345,
            start_token="100000",
            pid_namespace="pid:[1111]",
            net_namespace="net:[2222]",
            network_mode="none",
            mounts_sha256="f" * 64,
            cgroup_path="/system.slice/docker-" + manager_id + ".scope",
            cgroup_device=1,
            cgroup_inode=2,
            tasks=(PriorTask(12345, "100000"),),
            owner_lock_device=1,
            owner_lock_inode=4,
            instance_id="original-instance",
            bootstrap_source=files[0].path,
            bootstrap_sha256=files[0].sha256,
            published_port=8080,
        )
        proxy = PriorContainer(
            engine_id="fixture-engine",
            container_id=proxy_id,
            image_id="sha256:" + "d" * 64,
            started_at="2026-11-10T00:00:01Z",
            restart_count=0,
            pid=12346,
            start_token="100001",
            pid_namespace="pid:[3333]",
            net_namespace="net:[2222]",
            network_mode="container:" + manager_id,
            mounts_sha256="e" * 64,
            cgroup_path="/system.slice/docker-" + proxy_id + ".scope",
            cgroup_device=1,
            cgroup_inode=3,
            tasks=(PriorTask(12346, "100001"),),
        )
        prior.write_bytes(json.dumps({"management": asdict(manager), "proxy": asdict(proxy)}, sort_keys=True, separators=(",", ":")).encode())
        prior.chmod(0o600)
        enrollment = ApprovedEnrollment(
            "approved-current-containment-enrollment-v1",
            "fixture-deployment",
            "fixture-enrollment",
            "separately-approved-fixture",
            "ordinary-v1",
            "fixture-engine",
            manager_id,
            "sha256:" + "b" * 64,
            proxy_id,
            "sha256:" + "d" * 64,
            tuple(files),
            str(prior),
            hashlib.sha256(prior.read_bytes()).hexdigest(),
            True,
        )
        (root / "fixture-enrollment.json").write_text(json.dumps(asdict(enrollment), sort_keys=True))
        (root / "fixture-enrollment.json").chmod(0o600)
        selection = SimpleNamespace(
            deployment_id=enrollment.deployment_id,
            enrollment_id=enrollment.enrollment_id,
            authorization_id=enrollment.authorization_id,
            profile=enrollment.profile,
            management_id=enrollment.management_id,
            proxy_id=enrollment.proxy_id,
        )
        return root, enrollment, selection

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.directory.cleanup()


def test_protected_reader_observes_exact_separately_installed_selection_only() -> None:
    with _approved_fixture() as (root, enrollment, selection):
        saved = _observe_approved_enrollment(selection, root=root, expected_uid=os.geteuid())
        assert saved.enrollment == enrollment
        assert saved.enrollment_sha256 == hashlib.sha256((root / "fixture-enrollment.json").read_bytes()).hexdigest()
        assert {name for name, _ in saved.original_file_sha256} == {"bootstrap-source", "bootstrap-config", "management-config", "proxy-config"}
        assert saved.original_prior_evidence_sha256 == enrollment.original_prior_evidence_sha256
        assert _observe_approved_enrollment(selection, root=root, expected_uid=os.geteuid()) == saved
        selection.deployment_id = "other-deployment"
        with pytest.raises(EnrollmentUnavailable, match="proposed deployment differ"):
            _observe_approved_enrollment(selection, root=root, expected_uid=os.geteuid())


def test_protected_reader_refuses_original_file_and_prior_evidence_substitution() -> None:
    with _approved_fixture() as (root, enrollment, selection):
        original = Path(enrollment.original_files[0].path)
        original.write_bytes(b"substituted source")
        with pytest.raises(EnrollmentUnavailable, match="original source/configuration differs"):
            _observe_approved_enrollment(selection, root=root, expected_uid=os.geteuid())
    with _approved_fixture() as (root, enrollment, selection):
        Path(enrollment.original_prior_evidence_path).write_bytes(b"substituted provenance")
        with pytest.raises(EnrollmentUnavailable, match="original Docker/lifetime evidence differs"):
            _observe_approved_enrollment(selection, root=root, expected_uid=os.geteuid())


def test_protected_reader_refuses_invalid_approval_protection_and_schema() -> None:
    with _approved_fixture() as (root, enrollment, selection):
        path = root / "fixture-enrollment.json"
        path.chmod(0o644)
        with pytest.raises(EnrollmentUnavailable, match="not protected"):
            _observe_approved_enrollment(selection, root=root, expected_uid=os.geteuid())
        path.chmod(0o600)
        data = asdict(enrollment)
        data["approved_observation_only"] = False
        path.write_text(json.dumps(data))
        with pytest.raises(EnrollmentUnavailable, match="cannot be decoded"):
            _observe_approved_enrollment(selection, root=root, expected_uid=os.geteuid())
        path.unlink()
        (root / "other.json").write_text("{}")
        (root / "other.json").chmod(0o600)
        path.symlink_to(root / "other.json")
        with pytest.raises(EnrollmentUnavailable, match="not canonical"):
            _observe_approved_enrollment(selection, root=root, expected_uid=os.geteuid())


def test_protected_reader_refuses_unreviewed_quarantine_support_and_duplicate_keys() -> None:
    with _approved_fixture() as (root, enrollment, selection):
        path = root / "fixture-enrollment.json"
        data = asdict(enrollment)
        data["profile"] = "quarantined-original-v1"
        selection.profile = "quarantined-original-v1"
        path.write_text(json.dumps(data))
        with pytest.raises(EnrollmentUnavailable, match="cannot be decoded"):
            _observe_approved_enrollment(selection, root=root, expected_uid=os.geteuid())
        path.write_text('{"schema":"approved-current-containment-enrollment-v1","schema":"duplicate"}')
        with pytest.raises(EnrollmentUnavailable, match="cannot be decoded"):
            _observe_approved_enrollment(selection, root=root, expected_uid=os.geteuid())


def test_protected_reader_refuses_other_uid_and_mutable_approval_root() -> None:
    with _approved_fixture() as (root, _, selection):
        with pytest.raises(EnrollmentUnavailable, match="trusted host owner"):
            _observe_approved_enrollment(selection, root=root, expected_uid=os.geteuid() + 1)
        root.chmod(0o755)
        with pytest.raises(EnrollmentUnavailable, match="not private"):
            _observe_approved_enrollment(selection, root=root, expected_uid=os.geteuid())


def test_protected_reader_never_creates_missing_approval() -> None:
    with _approved_fixture() as (root, _, selection):
        approval = root / "fixture-enrollment.json"
        approval.unlink()
        with pytest.raises((EnrollmentUnavailable, OSError)):
            _observe_approved_enrollment(selection, root=root, expected_uid=os.geteuid())
        assert not approval.exists()
        assert list(root.iterdir()) == []
