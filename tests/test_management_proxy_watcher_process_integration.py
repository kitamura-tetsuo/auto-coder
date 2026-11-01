"""Disposable Docker effect with watcher killed before acknowledgement."""

import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

pytestmark = [pytest.mark.supervisor_integration, pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")]


def docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(["/usr/bin/docker", *args], capture_output=True, text=True, check=check, timeout=30)
    return result.stdout.strip()


@pytest.mark.parametrize("interrupted", ["stop", "start"])
def test_actual_proxy_effect_then_watcher_crash_never_replays(tmp_path: Path, interrupted: str) -> None:
    if os.geteuid() != 0:
        pytest.skip("host root is required to observe Docker network namespace identities")
    manager_ref = os.environ.get("AUTOCODER_FRESH_TEST_BASE_IMAGE", "auto-coder:management-local-abc65544")
    proxy_ref = os.environ.get("AUTOCODER_PROXY_TEST_IMAGE", "caddy@sha256:504a44d7d74bd2bc28a66afc4774912fc21223ca8a67734af1c54ab2f999b489")
    manager_image = docker("image", "inspect", manager_ref, "--format", "{{.Id}}")
    proxy_image = docker("image", "inspect", proxy_ref, "--format", "{{.Id}}")
    suffix = uuid.uuid4().hex[:12]
    manager, proxy = "ac-watcher-crash-manager-" + suffix, "ac-watcher-crash-proxy-" + suffix
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(":18082 {\n reverse_proxy 127.0.0.1:18081\n}\n", encoding="utf-8")
    caddyfile.chmod(0o400)
    marker, release = tmp_path / "effect-returned", tmp_path / "release-wrapper"
    wrapper_dir = tmp_path / "commands"
    wrapper_dir.mkdir(mode=0o700)
    wrapper = wrapper_dir / "docker"
    wrapper.write_text(
        "#!/bin/sh\n" '/usr/bin/docker "$@"\n' "status=$?\n" f'if [ "$1" = {shlex.quote(interrupted)} ] && [ "$status" = 0 ]; then\n' f"  touch {shlex.quote(str(marker))}\n" f"  while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.1; done\n" "fi\n" 'exit "$status"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    watcher = None
    resumed = None
    try:
        manager_id = docker("run", "-d", "--name", manager, "--network", "none", "--entrypoint", "python", manager_image, "-m", "http.server", "18081", "--bind", "127.0.0.1")
        proxy_id = docker("run", "-d", "--name", proxy, "--network", "container:" + manager_id, "--entrypoint", "caddy", "--mount", f"type=bind,src={caddyfile},dst=/etc/caddy/Caddyfile,readonly", proxy_image, "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile")
        docker("restart", "-t", "3", manager_id)
        config = state / "config.json"
        config.write_text(json.dumps({"state_root": str(state), "manager_id": manager_id, "manager_image": manager_image, "proxy_id": proxy_id, "proxy_image": proxy_image, "config_sha256": hashlib.sha256(caddyfile.read_bytes()).hexdigest()}), encoding="utf-8")
        config.chmod(0o600)
        command = [sys.executable, "-m", "auto_coder.management.proxy_watch_cli", "--config", str(config)]
        env = dict(os.environ)
        env["PATH"] = str(wrapper_dir) + os.pathsep + env.get("PATH", "")
        watcher = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 45
        while not marker.exists():
            assert watcher.poll() is None and time.monotonic() < deadline, "watcher did not reach the post-effect barrier"
            time.sleep(0.05)
        observed = json.loads(docker("inspect", proxy_id))[0]["State"]
        assert observed["Running"] is (interrupted == "start")
        expected_phase = interrupted + "_unknown"
        assert json.loads((state / "proxy-reconnect.json").read_text())["phase"] == expected_phase
        watcher.send_signal(signal.SIGKILL)
        watcher.wait(timeout=10)
        watcher = None
        release.touch()
        resumed = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        assert resumed.stdout is not None
        import select

        assert select.select([resumed.stdout], [], [], 20)[0], "reconstructed watcher produced no outcome"
        assert resumed.stdout.readline().strip() == "NEEDS_ATTENTION"
        after = json.loads(docker("inspect", proxy_id))[0]["State"]
        assert after["Running"] == observed["Running"] and after["StartedAt"] == observed["StartedAt"]
        assert json.loads((state / "proxy-reconnect.json").read_text())["phase"] == expected_phase
    finally:
        release.touch()
        for process in (watcher, resumed):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        docker("rm", "-f", proxy, manager, check=False)
