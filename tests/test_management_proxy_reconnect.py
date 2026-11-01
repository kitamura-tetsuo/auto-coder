import json
import select
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from auto_coder.management.proxy_reconnect import ProxyBinding, ProxyReconnect


class Driver:
    def __init__(self, binding: ProxyBinding) -> None:
        self.binding = binding
        self.manager = {"id": binding.manager_id, "image": binding.manager_image, "running": True, "pid": 10, "started": "one", "network": "bridge"}
        self.proxy = {"id": binding.proxy_id, "image": binding.proxy_image, "running": True, "pid": 20, "started": "one", "network": "container:" + binding.manager_id, "entrypoint": ["caddy"], "command": ["run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]}
        self.namespaces = {10: "net:[2]", 20: "net:[1]"}
        self.effects: list[str] = []
        self.fail = ""
        self.change_after_stop = False

    def inspect(self, container: str) -> dict[str, object]:
        return dict(self.manager if container == self.binding.manager_id else self.proxy)

    def namespace(self, pid: int) -> str:
        return self.namespaces[pid]

    def effect(self, action: str, _proxy: str) -> None:
        self.effects.append(action)
        if self.fail == action:
            raise TimeoutError("uncertain delivery")
        if action == "stop":
            self.proxy.update(running=False, pid=0)
            if self.change_after_stop:
                self.manager.update(pid=11, started="two")
                self.namespaces[11] = "net:[3]"
        else:
            self.proxy.update(running=True, pid=21, started="two")
            self.namespaces[21] = self.namespaces[int(self.manager["pid"])]


def fixture(tmp_path: Path) -> tuple[ProxyReconnect, Driver]:
    binding = ProxyBinding("a" * 64, "sha256:" + "b" * 64, "c" * 64, "sha256:" + "d" * 64)
    driver = Driver(binding)
    return ProxyReconnect(tmp_path, binding, driver), driver  # type: ignore[arg-type]


def test_stale_namespace_reconnects_once_and_replay_does_not_dispatch(tmp_path: Path) -> None:
    controller, driver = fixture(tmp_path)
    assert controller.tick() == "ATTACHED"
    assert driver.effects == ["stop", "start"]
    assert controller.tick() == "ATTACHED"
    assert driver.effects == ["stop", "start"]
    driver.manager.update(pid=11, started="two")
    driver.namespaces[11] = "net:[3]"
    assert controller.tick() == "ATTACHED"
    assert driver.effects == ["stop", "start", "stop", "start"]


@pytest.mark.parametrize("effect", ["stop", "start"])
def test_uncertain_effect_never_retries_after_controller_reconstruction(tmp_path: Path, effect: str) -> None:
    controller, driver = fixture(tmp_path)
    driver.fail = effect
    with pytest.raises(TimeoutError):
        controller.tick()
    calls = list(driver.effects)
    reconstructed = ProxyReconnect(tmp_path, driver.binding, driver)  # type: ignore[arg-type]
    assert reconstructed.tick() == "NEEDS_ATTENTION"
    assert driver.effects == calls


def test_manager_change_after_confirmed_stop_never_starts_proxy(tmp_path: Path) -> None:
    controller, driver = fixture(tmp_path)
    driver.change_after_stop = True
    assert controller.tick() == "NEEDS_ATTENTION"
    assert driver.effects == ["stop"]
    assert controller.tick() == "NEEDS_ATTENTION" and driver.effects == ["stop"]


def test_reconstruction_after_durably_confirmed_stop_starts_once(tmp_path: Path) -> None:
    controller, driver = fixture(tmp_path)
    manager = controller._manager()
    driver.effect("stop", driver.binding.proxy_id)
    controller._write(manager, "stopped")
    reconstructed = ProxyReconnect(tmp_path, driver.binding, driver)  # type: ignore[arg-type]
    assert reconstructed.tick() == "ATTACHED"
    assert driver.effects == ["stop", "start"]
    assert reconstructed.tick() == "ATTACHED" and driver.effects == ["stop", "start"]


def test_unexpected_stopped_proxy_and_wrong_image_never_issue_effects(tmp_path: Path) -> None:
    controller, driver = fixture(tmp_path)
    driver.proxy.update(running=False, pid=0)
    assert controller.tick() == "NEEDS_ATTENTION" and driver.effects == []
    driver.proxy.update(running=True, pid=20, image="sha256:" + "e" * 64)
    with pytest.raises(ValueError):
        controller.tick()
    assert driver.effects == []


def test_signal_gated_proxy_is_never_treated_as_reconnected(tmp_path: Path) -> None:
    controller, driver = fixture(tmp_path)
    driver.proxy.update(entrypoint=["/bin/sh"], command=["-c", "wait for signal"])
    with pytest.raises(ValueError):
        controller.tick()
    assert driver.effects == []


def test_persistent_watchers_contend_on_one_host_lock(tmp_path: Path) -> None:
    _, driver = fixture(tmp_path)
    path = tmp_path / "watch.json"
    path.write_text(json.dumps({"state_root": str(tmp_path), **asdict(driver.binding)}), encoding="utf-8")
    path.chmod(0o600)
    command = [sys.executable, "-m", "auto_coder.management.proxy_watch_cli", "--config", str(path)]
    first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert first.stdout is not None and select.select([first.stdout], [], [], 15)[0]
        assert first.stdout.readline().startswith("UNAVAILABLE:")
        second = subprocess.run(command, check=False, capture_output=True, text=True, timeout=15)
        assert second.returncode != 0 and "BlockingIOError" in second.stderr
    finally:
        first.terminate()
        first.wait(timeout=10)
