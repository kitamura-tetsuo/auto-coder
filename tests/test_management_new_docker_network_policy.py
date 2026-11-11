"""Fail-closed checks for the bounded NEW host-loopback Docker profile."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence

import pytest

from auto_coder.management import fresh_new_docker
from auto_coder.management.fresh_host_observation import HostObservationUnavailable

pytestmark = pytest.mark.supervisor_integration


@pytest.mark.parametrize(
    "version,accepted",
    [
        ("27.5.1", False),
        ("28.0.0-rc.1", False),
        ("unknown", False),
        ("28.0.0", True),
        ("28.0.0+ubuntu", True),
        ("29.9.2", True),
    ],
)
def test_loopback_publication_refuses_legacy_or_unverifiable_docker_engine(monkeypatch: pytest.MonkeyPatch, version: str, accepted: bool) -> None:
    def fake_run(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert list(command) == ["docker", "version", "--format", "{{json .Server.Version}}"]
        return subprocess.CompletedProcess(command, 0, json.dumps(version), "")

    monkeypatch.setattr(fresh_new_docker.subprocess, "run", fake_run)
    if accepted:
        fresh_new_docker._require_safe_loopback_docker_engine()
    else:
        with pytest.raises(HostObservationUnavailable, match="localhost publication isolation"):
            fresh_new_docker._require_safe_loopback_docker_engine()


@pytest.mark.parametrize("ipv6_enabled", [False, True])
def test_dedicated_new_bridge_rejects_ipv6_even_with_masquerading_disabled(monkeypatch: pytest.MonkeyPatch, ipv6_enabled: bool) -> None:
    name = "ac-new-network-policy"
    network = {
        "Name": name,
        "Id": "a" * 64,
        "Driver": "bridge",
        "Scope": "local",
        "Internal": False,
        "Attachable": False,
        "Ingress": False,
        "EnableIPv6": ipv6_enabled,
        "Options": {"com.docker.network.bridge.enable_ip_masquerade": "false"},
        "Containers": {},
    }

    def fake_run(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert list(command) == ["docker", "network", "inspect", "--format", "{{json .}}", name]
        return subprocess.CompletedProcess(command, 0, json.dumps(network), "")

    monkeypatch.setattr(fresh_new_docker.subprocess, "run", fake_run)
    if ipv6_enabled:
        with pytest.raises(HostObservationUnavailable, match="network scope differs"):
            fresh_new_docker._dedicated_network_id(name)
    else:
        assert fresh_new_docker._dedicated_network_id(name) == "a" * 64
