"""Real Docker containment and owner-fence observations for fresh startup."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from auto_coder.management.fresh_host_observation import HostObservationUnavailable, assert_known_prior_stack_stopped, assert_replacement_published_listener, capture_prior_container
from auto_coder.management.fresh_initialization import FreshInitializationIntent

pytestmark = [pytest.mark.supervisor_integration, pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")]


def _available_port() -> int:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return int(reservation.getsockname()[1])


def _test_image(root: Path) -> tuple[str, str]:
    """Build a network-free disposable image from the host's own tiny tools."""
    files = {Path("/bin/sh"), Path("/bin/sleep"), Path("/usr/bin/flock")}
    for binary in tuple(files):
        output = subprocess.check_output(["ldd", str(binary)], text=True)
        files.update(Path(value) for value in re.findall(r"(/[^\s()]+)", output))
    filesystem = root / "rootfs"
    for source in files:
        destination = filesystem / source.relative_to("/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source.resolve(strict=True), destination)
    (root / "Dockerfile").write_text('FROM scratch\nCOPY rootfs/ /\nUSER 1000:1000\nENTRYPOINT ["/bin/sh"]\n')
    tag = "auto-coder-fresh-fixture:" + uuid.uuid4().hex
    subprocess.run(["docker", "build", "--network", "none", "--tag", tag, str(root)], check=True, capture_output=True, text=True, timeout=90)
    return tag, subprocess.check_output(["docker", "image", "inspect", tag, "--format", "{{.Id}}"], text=True).strip()


def test_real_docker_binding_rejects_foreign_port(tmp_path: Path) -> None:
    from types import SimpleNamespace

    tag, image = _test_image(tmp_path)
    name = "fresh-test-published-" + uuid.uuid4().hex[:12]
    try:
        container = subprocess.check_output(["docker", "run", "-d", "--publish", f"127.0.0.1:{_available_port()}:8080", "--entrypoint", "/bin/sleep", "--name", name, image, "120"], text=True).strip()
        actual = int(subprocess.check_output(["docker", "port", container, "8080/tcp"], text=True).strip().rsplit(":", 1)[1])
        with socket.socket() as foreign:
            foreign.bind(("127.0.0.1", 0))
            foreign.listen()
            port = foreign.getsockname()[1]
            assert port != actual
            intent = SimpleNamespace(new_container_id=container, image_digest=image, host_listener_port=port)
            with pytest.raises(HostObservationUnavailable):
                assert_replacement_published_listener(intent)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False, timeout=30)
        subprocess.run(["docker", "image", "rm", tag], capture_output=True, check=False, timeout=30)


def test_real_docker_published_listener_is_owned_by_replacement(tmp_path: Path) -> None:
    from types import SimpleNamespace

    tag, image = _test_image(tmp_path)
    name = "fresh-test-published-" + uuid.uuid4().hex[:12]
    try:
        container = subprocess.check_output(["docker", "run", "-d", "--publish", f"127.0.0.1:{_available_port()}:8080", "--entrypoint", "/bin/sleep", "--name", name, image, "120"], text=True).strip()
        port = int(subprocess.check_output(["docker", "port", container, "8080/tcp"], text=True).strip().rsplit(":", 1)[1])
        if os.geteuid() != 0:
            pytest.skip("host root is required to observe the disposable Docker proxy")
        assert_replacement_published_listener(SimpleNamespace(new_container_id=container, image_digest=image, host_listener_port=port))
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False, timeout=30)
        subprocess.run(["docker", "image", "rm", tag], capture_output=True, check=False, timeout=30)


def test_real_docker_listener_rejects_mismatched_container_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from auto_coder.management import fresh_host_observation as observation

    if os.geteuid() != 0:
        pytest.skip("host root is required to observe the disposable Docker proxy")
    tag, image = _test_image(tmp_path)
    name = "fresh-test-published-" + uuid.uuid4().hex[:12]
    try:
        container = subprocess.check_output(["docker", "run", "-d", "--publish", f"127.0.0.1:{_available_port()}:8080", "--entrypoint", "/bin/sleep", "--name", name, image, "120"], text=True).strip()
        port = int(subprocess.check_output(["docker", "port", container, "8080/tcp"], text=True).strip().rsplit(":", 1)[1])
        intent = SimpleNamespace(new_container_id=container, image_digest=image, host_listener_port=port)
        assert_replacement_published_listener(intent)
        inspect = observation._inspect

        def wrong_address(container_id: str) -> list[object]:
            values = inspect(container_id)
            values[18] = {"fixture": {"IPAddress": "198.51.100.1"}}
            return values

        monkeypatch.setattr(observation, "_inspect", wrong_address)
        with pytest.raises(HostObservationUnavailable) as error:
            assert_replacement_published_listener(intent)
        assert isinstance(error.value.__cause__, ValueError)
        assert str(error.value.__cause__) == "Docker proxy destination differs"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False, timeout=30)
        subprocess.run(["docker", "image", "rm", tag], capture_output=True, check=False, timeout=30)


def test_real_docker_listener_rejects_changed_container_inspection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from auto_coder.management import fresh_host_observation as observation

    if os.geteuid() != 0:
        pytest.skip("host root is required to observe the disposable Docker proxy")
    tag, image = _test_image(tmp_path)
    name = "fresh-test-published-" + uuid.uuid4().hex[:12]
    try:
        container = subprocess.check_output(["docker", "run", "-d", "--publish", f"127.0.0.1:{_available_port()}:8080", "--entrypoint", "/bin/sleep", "--name", name, image, "120"], text=True).strip()
        port = int(subprocess.check_output(["docker", "port", container, "8080/tcp"], text=True).strip().rsplit(":", 1)[1])
        intent = SimpleNamespace(new_container_id=container, image_digest=image, host_listener_port=port)
        inspect = observation._inspect
        calls = 0

        def changed_inspection(container_id: str) -> list[object]:
            nonlocal calls
            values = inspect(container_id)
            calls += 1
            if calls == 2:
                values[18] = {**values[18], "fixture": {"IPAddress": "198.51.100.1"}}
            return values

        monkeypatch.setattr(observation, "_inspect", changed_inspection)
        with pytest.raises(HostObservationUnavailable) as error:
            assert_replacement_published_listener(intent)
        assert calls == 2
        assert str(error.value.__cause__) == "replacement container inspection changed at fields 18"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False, timeout=30)
        subprocess.run(["docker", "image", "rm", tag], capture_output=True, check=False, timeout=30)


def test_real_prior_supervisor_binds_instance_and_published_port(tmp_path: Path) -> None:
    import hashlib

    tag = "auto-coder:management-local-abc65544"
    try:
        image = subprocess.check_output(["docker", "image", "inspect", tag, "--format", "{{.Id}}"], text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError:
        pytest.skip("production-profile image is not available locally")
    fixture_tag = "auto-coder-fresh-owner:" + uuid.uuid4().hex
    subprocess.run(["docker", "build", "--network", "none", "--tag", fixture_tag, "-"], input=f'FROM {tag}\nENTRYPOINT ["python", "-m", "auto_coder.management.bootstrap_supervisor"]\n', text=True, capture_output=True, check=True, timeout=90)
    image = subprocess.check_output(["docker", "image", "inspect", fixture_tag, "--format", "{{.Id}}"], text=True).strip()
    state = tmp_path / "state"
    config_dir = tmp_path / "bootstrap"
    oauth = tmp_path / "oauth"
    state.mkdir(mode=0o700)
    config_dir.mkdir(mode=0o700)
    oauth.mkdir(mode=0o700)
    config = config_dir / "config.json"
    config.write_text(
        json.dumps(
            {
                "instance_id": "actual-old-owner",
                "state_root": "/management-child-state",
                "oauth_root": "/management-oauth",
                "journal_root": "/management-child-state/journal",
                "listener_url": "http://127.0.0.1:8081",
                "initial_runtime": "bootstrap",
                "crash_budget": 0,
                "runtimes": [{"reference": "bootstrap", "receipt": "sha256:b8cb1f05b3261967b1432b6d312f0cc6d787924f9dd84eba2227ac40d597b6d9", "executable": "/opt/auto-coder/.venv/bin/auto-coder", "arguments": ["management", "serve", "--config", "/bootstrap/management.yaml"]}],
            }
        )
    )
    name = "fresh-test-owner-" + uuid.uuid4().hex[:12]
    try:
        container = subprocess.check_output(
            [
                "docker",
                "run",
                "-d",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--publish",
                f"127.0.0.1:{_available_port()}:8080",
                "--mount",
                f"type=bind,src={state},dst=/management-child-state",
                "--mount",
                f"type=bind,src={config_dir},dst=/bootstrap,readonly",
                "--mount",
                f"type=bind,src={oauth},dst=/management-oauth",
                "--name",
                name,
                image,
                "--config",
                "/bootstrap/config.json",
            ],
            text=True,
        ).strip()
        port = int(subprocess.check_output(["docker", "port", container, "8080/tcp"], text=True).strip().rsplit(":", 1)[1])
        deadline = time.monotonic() + 15
        while True:
            try:
                observed = capture_prior_container(container, image, expected_mounts=((str(state), "/management-child-state", True), (str(config_dir), "/bootstrap", False), (str(oauth), "/management-oauth", True)), require_owner_lock=True, require_deployment_identity=True)
                break
            except HostObservationUnavailable:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        assert observed.instance_id == "actual-old-owner"
        assert observed.published_port == port
        assert observed.bootstrap_sha256 == hashlib.sha256(config.read_bytes()).hexdigest()
        original = config.read_text()
        config.write_text(original.replace("actual-old-owner", "forged-old-owner"))
        with pytest.raises(HostObservationUnavailable):
            capture_prior_container(container, image, expected_mounts=((str(state), "/management-child-state", True), (str(config_dir), "/bootstrap", False), (str(oauth), "/management-oauth", True)), require_owner_lock=True, require_deployment_identity=True)
        config.write_text(original)
        from types import SimpleNamespace

        from auto_coder.management.fresh_host_observation import assert_retained_oauth_mount
        from auto_coder.management.oauth_config import OAuthClient, OAuthSettings
        from auto_coder.management.oauth_store import OAuthStore

        alternate = tmp_path / "alternate-oauth"
        alternate.mkdir()
        # Two real OAuth databases with identical authority metadata remain
        # distinct stores; the replacement must retain the mounted one.
        for directory in (oauth, alternate):
            OAuthStore(OAuthSettings(directory / "oauth.sqlite3", "http://127.0.0.1:8081", "fixture", (OAuthClient("fixture-client", ("http://127.0.0.1:8081/callback",)),)))
        own_info = oauth.stat()
        assert_retained_oauth_mount(observed, SimpleNamespace(host_oauth_root=str(oauth), oauth_device=own_info.st_dev, oauth_inode=own_info.st_ino))
        info = alternate.stat()
        with pytest.raises(HostObservationUnavailable):
            assert_retained_oauth_mount(observed, SimpleNamespace(host_oauth_root=str(alternate), oauth_device=info.st_dev, oauth_inode=info.st_ino))
        subprocess.run(["docker", "stop", "--time", "1", container], check=True, capture_output=True, timeout=20)
        from auto_coder.management.fresh_host_observation import assert_prior_container_stopped

        assert_prior_container_stopped(observed)
        config.write_text(config.read_text().replace("actual-old-owner", "forged-old-owner"))
        with pytest.raises(HostObservationUnavailable):
            assert_prior_container_stopped(observed)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False, timeout=30)
        subprocess.run(["docker", "image", "rm", fixture_tag], capture_output=True, check=False, timeout=30)


def test_actual_docker_owner_proxy_and_restart_fencing(tmp_path: Path) -> None:
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    tag, image = _test_image(image_dir)
    # No project data or live service is mounted into these disposable fixtures.
    old_state = tmp_path / "old-state"
    old_state.mkdir(mode=0o700)
    management = "fresh-test-management-" + uuid.uuid4().hex[:12]
    proxy_name = "fresh-test-proxy-" + uuid.uuid4().hex[:12]
    old = ""
    try:
        command = [
            "docker",
            "run",
            "-d",
            "--network",
            "none",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--mount",
            f"type=bind,src={old_state},dst=/management-child-state",
            "--entrypoint",
            "/usr/bin/flock",
            "--name",
            management,
            image,
            "-x",
            "/management-child-state/.supervisor-owner.lock",
            "/bin/sh",
            "-c",
            "/bin/sleep 120 & wait",
        ]
        old = subprocess.check_output(command, text=True).strip()
        deadline = time.monotonic() + 15
        while True:
            try:
                observed = capture_prior_container(old, image, expected_mounts=((str(old_state), "/management-child-state", True),), require_owner_lock=True)
                if len(observed.tasks) >= 2:
                    break
            except HostObservationUnavailable:
                pass
            if time.monotonic() >= deadline:
                raise AssertionError("the prior owner and child did not reach the observed cgroup barrier")
            time.sleep(0.05)
        proxy = subprocess.check_output(["docker", "run", "-d", "--network", "container:" + old, "--user", f"{os.getuid()}:{os.getgid()}", "--entrypoint", "/bin/sleep", "--name", proxy_name, image, "120"], text=True).strip()
        proxy_observation = capture_prior_container(proxy, image)
        guard = tmp_path / "guard"
        guard.mkdir(mode=0o700)
        mounts = tmp_path / "mounts.json"
        mounts.write_text(json.dumps({"management": [[str(old_state), "/management-child-state", True]], "proxy": []}))
        mounts.chmod(0o600)
        capture = subprocess.run(
            [sys.executable, "-m", "auto_coder.management.fresh_host", "capture", "--management-id", old, "--management-image", image, "--proxy-id", proxy, "--proxy-image", image, "--mounts-file", str(mounts), "--guard-dir", str(guard)], check=False, capture_output=True, text=True, timeout=30
        )
        # An arbitrary flock-holder is not an authoritative management owner.
        assert capture.returncode != 0 and not list(guard.iterdir())
        from auto_coder.management.fresh_host import _write_evidence

        evidence_digest = _write_evidence(guard, {"management": asdict(observed), "proxy": asdict(proxy_observation)})
        state, journal, oauth = (tmp_path / name for name in ("fresh-state", "fresh-journal", "oauth"))
        for directory in (state, journal, oauth):
            directory.mkdir(mode=0o700)
        retained = oauth / "retained"
        retained.write_bytes(b"unchanged")
        state_info, journal_info, oauth_info, guard_info = (path.stat() for path in (state, journal, oauth, guard))
        intent = FreshInitializationIntent(
            str(uuid.uuid4()),
            "prior",
            old,
            "fresh",
            "f" * 64,
            evidence_digest,
            observed.owner_lock_device,
            observed.owner_lock_inode,
            8080,
            "1" * 64,
            "2" * 64,
            image,
            "3" * 64,
            "4" * 64,
            "bootstrap",
            "/management-child-state",
            "/management-operations",
            "/management-oauth",
            str(state),
            str(journal),
            str(oauth),
            state_info.st_dev,
            state_info.st_ino,
            journal_info.st_dev,
            journal_info.st_ino,
            oauth_info.st_dev,
            oauth_info.st_ino,
            guard_info.st_dev,
            guard_info.st_ino,
        )
        intent_path = tmp_path / "intent.json"
        intent_path.write_text(json.dumps(asdict(intent)))
        intent_path.chmod(0o600)
        refused = subprocess.run([sys.executable, "-m", "auto_coder.management.fresh_host", "claim", "--intent-file", str(intent_path), "--evidence-file", str(guard / "prior-evidence.json"), "--prior-lock", str(old_state / ".supervisor-owner.lock")], capture_output=True, text=True, timeout=30)
        assert refused.returncode != 0 and not list(state.iterdir()) and not list(journal.iterdir()) and retained.read_bytes() == b"unchanged"
        subprocess.run(["docker", "stop", "--time", "1", old], check=True, capture_output=True, text=True, timeout=20)
        with pytest.raises(HostObservationUnavailable):
            assert_known_prior_stack_stopped(observed, proxy_observation)
        subprocess.run(["docker", "stop", "--time", "1", proxy], check=True, capture_output=True, text=True, timeout=20)
        assert_known_prior_stack_stopped(observed, proxy_observation)
        for altered in (replace(observed, engine_id="different-engine"), replace(observed, mounts_sha256="0" * 64), replace(observed, restart_count=observed.restart_count + 1)):
            with pytest.raises(HostObservationUnavailable):
                assert_known_prior_stack_stopped(altered, proxy_observation)
        subprocess.run(["docker", "start", old], check=True, capture_output=True, text=True, timeout=20)
        subprocess.run(["docker", "stop", "--time", "1", old], check=True, capture_output=True, text=True, timeout=20)
        with pytest.raises(HostObservationUnavailable):
            assert_known_prior_stack_stopped(observed, proxy_observation)
    finally:
        subprocess.run(["docker", "rm", "-f", proxy_name, management], capture_output=True, text=True, check=False, timeout=30)
        subprocess.run(["docker", "image", "rm", tag], capture_output=True, text=True, check=False, timeout=30)
