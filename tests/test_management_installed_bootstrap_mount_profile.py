"""Original ordinary command/mount/config readback is not a closure verdict."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from auto_coder.management import installed_bootstrap_mount_profile as profile
from auto_coder.management.containment_enrollment import ApprovedEnrollment, EnrollmentObservation, OriginalFile
from auto_coder.management.fresh_host_observation import PriorContainer, PriorTask


def _fixture() -> tuple[EnrollmentObservation, dict[str, object], dict[str, object], PriorContainer, PriorContainer, bytes]:
    manager_id, proxy_id = "a" * 64, "b" * 64
    bootstrap = Path("/private/bootstrap/config.json")
    management = Path("/private/bootstrap/management.yaml")
    config = json.dumps(
        {
            "instance_id": "fixture-instance",
            "state_root": "/management-child-state",
            "oauth_root": "/management-oauth",
            "runtimes": [{"reference": "fixture", "executable": "/opt/auto-coder/.venv/bin/auto-coder", "arguments": ["management", "serve", "--config", "/bootstrap/management.yaml"]}],
        },
        sort_keys=True,
    ).encode()
    digest = hashlib.sha256(config).hexdigest()
    config_file = OriginalFile("bootstrap-config", str(bootstrap), digest, 1, 2)
    management_file = OriginalFile("management-config", str(management), hashlib.sha256(b"fixture yaml").hexdigest(), 1, 3)
    source_file = OriginalFile("bootstrap-source", "/private/source", "c" * 64, 1, 4)
    proxy_file = OriginalFile("proxy-config", "/private/proxy", "d" * 64, 1, 5)
    enrollment = ApprovedEnrollment(
        "approved-current-containment-enrollment-v1",
        "fixture-deployment",
        "fixture-enrollment",
        "fixture-authorization",
        "ordinary-v1",
        "fixture-engine",
        manager_id,
        "sha256:" + "c" * 64,
        proxy_id,
        "sha256:" + "d" * 64,
        (config_file, source_file, management_file, proxy_file),
        "/private/prior-evidence",
        "0" * 64,
        True,
    )
    record = EnrollmentObservation(enrollment, "1" * 64, 1, 2, 3, tuple((f.purpose, f.sha256) for f in enrollment.original_files), "0" * 64)
    mounts: list[dict[str, object]] = [
        {"Type": "bind", "Source": "/private/state", "Destination": "/management-child-state", "RW": True},
        {"Type": "bind", "Source": "/private/bootstrap", "Destination": "/bootstrap", "RW": False},
        {"Type": "bind", "Source": "/private/oauth", "Destination": "/management-oauth", "RW": True},
    ]
    inspect_manager: dict[str, object] = {
        "Id": manager_id,
        "Config": {"Entrypoint": ["python"], "Cmd": ["-m", "auto_coder.management.bootstrap_supervisor", "--config", "/bootstrap/config.json"]},
        "HostConfig": {"Privileged": False, "PidMode": "", "CapAdd": None, "Devices": None, "SecurityOpt": None},
        "Mounts": mounts,
    }
    inspect_proxy: dict[str, object] = {
        "Id": proxy_id,
        "Config": {"Entrypoint": ["/bin/sleep"], "Cmd": ["120"]},
        "HostConfig": {"Privileged": False, "PidMode": "", "CapAdd": None, "Devices": None, "SecurityOpt": None, "NetworkMode": "container:" + manager_id},
        "Mounts": [],
    }
    mount_digest = hashlib.sha256(json.dumps(profile._mounts(inspect_manager), separators=(",", ":")).encode()).hexdigest()
    no_mount_digest = hashlib.sha256(b"[]").hexdigest()
    prior_manager = PriorContainer(
        "fixture-engine",
        manager_id,
        enrollment.management_image_id,
        "2026-11-12T00:00:00Z",
        0,
        123,
        "456",
        "pid:[100]",
        "net:[200]",
        "none",
        mount_digest,
        "/system.slice/docker-" + manager_id + ".scope",
        1,
        2,
        (PriorTask(123, "456"),),
        owner_lock_device=1,
        owner_lock_inode=7,
        instance_id="fixture-instance",
        bootstrap_source=str(bootstrap),
        bootstrap_sha256=digest,
    )
    prior_proxy = PriorContainer("fixture-engine", proxy_id, enrollment.proxy_image_id, "2026-11-12T00:00:00Z", 0, 124, "457", "pid:[101]", "net:[200]", "container:" + manager_id, no_mount_digest, "/system.slice/docker-" + proxy_id + ".scope", 1, 3, (PriorTask(124, "457"),))
    return record, inspect_manager, inspect_proxy, prior_manager, prior_proxy, config


def _reviewed_files(monkeypatch: pytest.MonkeyPatch, record: EnrollmentObservation, config: bytes) -> None:
    originals = {item.purpose: item for item in record.enrollment.original_files}

    def read(_record: EnrollmentObservation, purpose: str) -> tuple[Path, bytes, str]:
        item = originals[purpose]
        contents = config if purpose == "bootstrap-config" else b"fixture yaml"
        return Path(item.path), contents, hashlib.sha256(contents).hexdigest()

    monkeypatch.setattr(profile, "_original_config", read)


def test_original_ordinary_effective_command_mounts_and_configuration_are_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    record, manager, proxy, prior_manager, prior_proxy, config = _fixture()
    _reviewed_files(monkeypatch, record, config)
    assert profile._validated_ordinary_profile(record, manager, proxy, prior_manager, prior_proxy) == (
        hashlib.sha256(config).hexdigest(),
        hashlib.sha256(b"fixture yaml").hexdigest(),
    )


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("Cmd", ["-m", "auto_coder.management.bootstrap_supervisor", "--config", "/other/config.json"], "effective command"),
        ("Entrypoint", ["/bin/sh"], "effective command"),
    ],
)
def test_unreviewed_bootstrap_command_refuses(monkeypatch: pytest.MonkeyPatch, field: str, value: object, expected: str) -> None:
    record, manager, proxy, prior_manager, prior_proxy, config = _fixture()
    _reviewed_files(monkeypatch, record, config)
    assert isinstance(manager["Config"], dict)
    manager["Config"][field] = value
    with pytest.raises(ValueError, match=expected):
        profile._validated_ordinary_profile(record, manager, proxy, prior_manager, prior_proxy)


def test_effective_security_and_proxy_network_substitution_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    record, manager, proxy, prior_manager, prior_proxy, config = _fixture()
    _reviewed_files(monkeypatch, record, config)
    assert isinstance(manager["HostConfig"], dict) and isinstance(proxy["HostConfig"], dict)
    manager["HostConfig"]["SecurityOpt"] = ["no-new-privileges"]
    with pytest.raises(ValueError, match="security configuration"):
        profile._validated_ordinary_profile(record, manager, proxy, prior_manager, prior_proxy)
    manager["HostConfig"]["SecurityOpt"] = None
    proxy["HostConfig"]["NetworkMode"] = "bridge"
    with pytest.raises(ValueError, match="proxy network"):
        profile._validated_ordinary_profile(record, manager, proxy, prior_manager, prior_proxy)


def test_altered_or_shadowed_mount_refuses_even_if_prior_digest_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    record, manager, proxy, prior_manager, prior_proxy, config = _fixture()
    _reviewed_files(monkeypatch, record, config)
    assert isinstance(manager["Mounts"], list)
    manager["Mounts"][1]["Source"] = "/private/other-bootstrap"
    with pytest.raises(ValueError, match="captured original Docker mounts"):
        profile._validated_ordinary_profile(record, manager, proxy, prior_manager, prior_proxy)
    manager["Mounts"][1]["Source"] = "/private/bootstrap"
    manager["Mounts"].append({"Type": "bind", "Source": "/private/shadow", "Destination": "/bootstrap/config.json", "RW": False})
    spoofed_prior = replace(prior_manager, mounts_sha256=hashlib.sha256(json.dumps(profile._mounts(manager), separators=(",", ":")).encode()).hexdigest())
    with pytest.raises(ValueError, match="shadowed"):
        profile._validated_ordinary_profile(record, manager, proxy, spoofed_prior, prior_proxy)


def test_original_configuration_or_runtime_selection_substitution_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    record, manager, proxy, prior_manager, prior_proxy, config = _fixture()
    _reviewed_files(monkeypatch, record, config)
    with pytest.raises(ValueError, match="captured live producer"):
        profile._validated_ordinary_profile(record, manager, proxy, replace(prior_manager, bootstrap_sha256="0" * 64), prior_proxy)
    mutated = json.loads(config)
    mutated["runtimes"][0]["arguments"][-1] = "/tmp/management.yaml"
    altered_bytes = json.dumps(mutated).encode()
    _reviewed_files(monkeypatch, record, altered_bytes)
    with pytest.raises(ValueError, match="runtime command/config"):
        profile._validated_ordinary_profile(record, manager, proxy, replace(prior_manager, bootstrap_sha256=hashlib.sha256(altered_bytes).hexdigest()), prior_proxy)


def test_quarantined_original_is_explicitly_unsupported_without_mount_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    record, manager, proxy, prior_manager, prior_proxy, config = _fixture()
    _reviewed_files(monkeypatch, record, config)
    record = replace(record, enrollment=replace(record.enrollment, profile="quarantined-original-v1"))
    with pytest.raises(ValueError, match="does not cover the quarantined original"):
        profile._validated_ordinary_profile(record, manager, proxy, prior_manager, prior_proxy)
    with pytest.raises(ValueError, match="no reviewed exact mount/config profile"):
        profile.inspect_installed_bootstrap_mount_profile(record, {})


def test_separate_profile_reader_binds_both_original_inspections_and_detects_races(monkeypatch: pytest.MonkeyPatch) -> None:
    record, manager, proxy, prior_manager, prior_proxy, config = _fixture()
    _reviewed_files(monkeypatch, record, config)
    prior_bytes = b"fixture evidence"
    record = replace(record, original_prior_evidence_sha256=hashlib.sha256(prior_bytes).hexdigest())
    monkeypatch.setattr(profile, "_protected", lambda *_args, **_kwargs: (prior_bytes, os.stat(__file__)))
    monkeypatch.setattr(profile, "_evidence", lambda *_args: (prior_manager, prior_proxy, record.original_prior_evidence_sha256))
    inspections = {record.enrollment.management_id: manager, record.enrollment.proxy_id: proxy}
    calls: list[str] = []

    def inspect(identifier: str) -> dict[str, object]:
        calls.append(identifier)
        return inspections[identifier]

    monkeypatch.setattr(profile, "_inspect", inspect)
    expected = {identifier: profile._digest([value]) for identifier, value in inspections.items()}
    result = profile.inspect_installed_bootstrap_mount_profile(record, expected)
    assert result.profile == "ordinary-v1"
    assert result.original_owner_lock_inode == 7
    assert calls == [record.enrollment.management_id, record.enrollment.proxy_id] * 2
    with pytest.raises(ValueError, match="since complete inventory"):
        profile.inspect_installed_bootstrap_mount_profile(record, {**expected, record.enrollment.proxy_id: "0" * 64})

    calls.clear()

    def changed_inspect(identifier: str) -> dict[str, object]:
        calls.append(identifier)
        if len(calls) > 2:
            return {**inspections[identifier], "unexpected-change": True}
        return inspections[identifier]

    monkeypatch.setattr(profile, "_inspect", changed_inspect)
    with pytest.raises(ValueError, match="changed during observation"):
        profile.inspect_installed_bootstrap_mount_profile(record, expected)


def test_original_file_readback_rejects_identity_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    record, _, _, _, _, config = _fixture()

    class _Stat:
        st_dev = 1
        st_ino = 9

    monkeypatch.setattr(profile, "_protected", lambda *_args, **_kwargs: (config, _Stat()))
    with pytest.raises(ValueError, match="changed since protected enrollment"):
        profile._original_config(record, "bootstrap-config")
