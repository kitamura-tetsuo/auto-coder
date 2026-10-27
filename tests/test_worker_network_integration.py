"""Disposable two-channel authenticated private Docker-network regression."""

from __future__ import annotations

import base64
import json
import os
import random
import subprocess
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.supervisor_integration

_FIXTURES = Path(__file__).parent / "fixtures"
_COMPOSE = _FIXTURES / "compose.supervisor-network.yml"


def _docker(*args: str, environment: dict[str, str] | None = None, timeout: int = 40, check: bool = True) -> str:
    result = subprocess.run(["docker", *args], text=True, capture_output=True, check=check, timeout=timeout, env=environment)
    return result.stdout.strip()


def _wait_for(predicate, *, deadline_seconds: float = 60.0):
    until = time.monotonic() + deadline_seconds
    last = None
    while time.monotonic() < until:
        try:
            result = predicate()
            if result:
                return result
        except (subprocess.CalledProcessError, ValueError, OSError) as error:
            last = error
        time.sleep(0.15)
    raise AssertionError(f"private network fixture did not settle: {last}")


def test_two_channel_manager_restart_peer_isolation_and_lost_response(tmp_path: Path, _use_real_commands: None, _use_real_sleep: None) -> None:
    image = os.environ.get("AUTOCODER_SUPERVISOR_IMAGE")
    if not image:
        if os.environ.get("AUTO_CODER_REQUIRE_SUPERVISOR_INTEGRATION") == "1":
            pytest.fail("Supervisor Integration Tests need a prepared production image")
        pytest.skip("provide AUTOCODER_SUPERVISOR_IMAGE for disposable Docker fixture")

    root = tmp_path / "network"
    for path in ("release", "beta", "release-workspace", "beta-workspace", "release/home", "beta/home"):
        (root / path).mkdir(parents=True)
    for name in ("release-secret", "beta-secret"):
        (root / name).write_bytes(base64.b64encode(os.urandom(48)) + b"\n")
    octet = random.SystemRandom().randrange(64, 239)
    environment = dict(os.environ)
    environment.update(
        AUTOCODER_SUPERVISOR_IMAGE=image,
        SUPERVISOR_RUN_ROOT=str(root),
        SUPERVISOR_FIXTURES_DIR=str(_FIXTURES.resolve()),
        SUPERVISOR_HOST_UID=str(os.getuid()),
        SUPERVISOR_HOST_GID=str(os.getgid()),
        SUPERVISOR_BRIDGE_SUBNET=f"10.252.{octet}.0/24",
        SUPERVISOR_RELEASE_IP=f"10.252.{octet}.11",
        SUPERVISOR_BETA_IP=f"10.252.{octet}.12",
    )
    project = "worker-private-" + uuid.uuid4().hex[:12]
    compose = ("compose", "--project-name", project, "-f", str(_COMPOSE))

    def compose_call(*args: str, timeout: int = 60) -> str:
        return _docker(*compose, *args, environment=environment, timeout=timeout)

    def manager_call(manager: str, operation: str, channel: str, *extra: str) -> dict[str, object]:
        output = _docker("exec", manager, "python", "/fixtures/supervisor_network_client_fixture.py", operation, channel, *extra, timeout=15)
        result = json.loads(output)
        assert isinstance(result, dict)
        return result

    try:
        compose_call("up", "-d", "--no-build", timeout=90)
        manager = compose_call("ps", "-q", "manager")
        release = compose_call("ps", "-q", "release")
        beta = compose_call("ps", "-q", "beta")
        assert manager and release and beta and len({manager, release, beta}) == 3
        for container in (manager, release, beta):
            metadata = json.loads(_docker("inspect", container))[0]
            assert metadata["HostConfig"]["Privileged"] is False
            assert metadata["Mounts"] and all("docker.sock" not in item["Destination"] for item in metadata["Mounts"])
            assert not any(host_bindings for host_bindings in (metadata["HostConfig"]["PortBindings"] or {}).values())
            assert metadata["HostConfig"]["NetworkMode"] != "host"
            facts = json.loads(_docker("exec", container, "python", "-c", "import json,shutil;from pathlib import Path;print(json.dumps({'socket':Path('/var/run/docker.sock').exists(),'binary':shutil.which('docker')}))"))
            assert facts == {"socket": False, "binary": None}

        def observe(channel: str) -> dict[str, object] | None:
            result = manager_call(manager, "observe", channel)
            data = result.get("data")
            return data if result["result_code"] == "OK" and isinstance(data, dict) else None

        release_seen = _wait_for(lambda: observe("release"))
        beta_seen = _wait_for(lambda: observe("beta"))
        assert release_seen["observation"]["repository"] == "fixture/release"
        assert beta_seen["observation"]["repository"] == "fixture/beta"
        assert release_seen["observation"]["process_run_id"] != beta_seen["observation"]["process_run_id"]
        assert beta_seen["observation"]["nonce"].startswith("fixture-request-")

        assert manager_call(manager, "auth-attacks", "release")["statuses"] == [200, 401, 401, 401]
        swapped_responses = manager_call(manager, "cross-request-swap", "release")
        assert swapped_responses["upstream_statuses"] == [200, 200]
        assert len(set(swapped_responses["upstream_request_ids"])) == 2
        assert len(set(swapped_responses["upstream_nonces"])) == 2
        assert swapped_responses["upstream_runs"] == [release_seen["observation"]["process_run_id"]] * 2
        assert swapped_responses["returned_codes"] == ["UNKNOWN", "UNKNOWN"]
        assert swapped_responses["returned_data"] == [None, None]

        held_uds = root / "release" / "fixture-hold-observation"
        entered_uds = root / "release" / "fixture-observation-entered"
        held_uds.write_text("hold")
        try:
            began = time.monotonic()
            slow = manager_call(manager, "observe", "release")
            assert time.monotonic() - began < 15
            assert entered_uds.is_file()  # Real worker UDS producer entered the held path.
            assert slow["result_code"] in {"UNAVAILABLE", "UNKNOWN"}
            assert slow["data"] is None
            health = manager_call(manager, "inspect", "release")
            assert health["result_code"] == "OK" and health["data"]["alive"] is True
            assert health["data"]["worker"]["binding_status"] != "VERIFIED"
            assert manager_call(manager, "read-operation", "release", "slow-unrecorded")["result_code"] == "NOT_FOUND"
            assert observe("beta") is not None
        finally:
            held_uds.unlink(missing_ok=True)
        after_slow = _wait_for(lambda: observe("release"))
        assert after_slow["observation"]["process_run_id"] == release_seen["observation"]["process_run_id"]

        swapped = manager_call(manager, "swapped-target", "beta")
        assert swapped["result_code"] == "UNKNOWN"  # Target does not possess the beta key.
        stale = manager_call(manager, "stale-stop", "beta", "replaced-supervisor-incarnation")
        assert stale["http_status"] == 409
        assert observe("beta") is not None

        manager_call(manager, "drop-stop", "beta", "beta-held-stop")
        _wait_for(lambda: (root / "beta" / "fixture-draining").exists())
        compose_call("restart", "manager")
        manager = compose_call("ps", "-q", "manager")
        held = manager_call(manager, "read-operation", "beta", "beta-held-stop")
        assert held["result_code"] == "DRAINING"
        assert held["data"]["operation_id"] == "beta-held-stop"
        assert observe("release") is not None
        (root / "beta" / "fixture-release").write_text("release")
        done = _wait_for(lambda: (result if result["result_code"] == "STOPPED" else None) if (result := manager_call(manager, "read-operation", "beta", "beta-held-stop")) else None)
        assert done["data"]["status"] == "STOPPED"
        stopped = manager_call(manager, "inspect", "beta")
        assert stopped["result_code"] == "OK"
        assert stopped["data"]["status"] == "STOPPED"
        assert manager_call(manager, "observe", "beta")["result_code"] == "UNAVAILABLE"
        assert observe("release") is not None
    finally:
        compose_call("down", "--remove-orphans", timeout=90)
