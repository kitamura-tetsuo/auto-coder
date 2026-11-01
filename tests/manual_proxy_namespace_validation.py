"""Disposable Docker experiment; never targets an existing container.

Run explicitly: python tests/manual_proxy_namespace_validation.py
"""

import json
import select
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from auto_coder.management.proxy_reconnect import DockerProxyObservation, ProxyBinding, ProxyReconnect


def docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(["docker", *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)
    return result.stdout.strip()


def netns(name: str) -> str:
    data = json.loads(docker("inspect", name))[0]
    return str(Path(f"/proc/{data['State']['Pid']}/ns/net").readlink())


def http(name: str) -> bool:
    code = "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:18082/marker',timeout=2).read().decode())"
    return docker("exec", name, "python", "-c", code, check=False) == "fresh-proxy-test"


def wait_http(name: str, expected: bool) -> None:
    for _ in range(140):
        if http(name) is expected:
            return
        time.sleep(0.25)
    raise RuntimeError("proxy readback did not reach expected state")


def main() -> None:
    suffix = uuid.uuid4().hex[:12]
    manager = "ac-proxy-proof-manager-" + suffix
    replacement = "ac-proxy-proof-replacement-" + suffix
    proxy = "ac-proxy-proof-proxy-" + suffix
    successor = "ac-proxy-proof-successor-" + suffix
    image = "sha256:5460b5773b335b60e11c259eca633a44ef7f76fe3579a24183703dca76523c32"
    caddy = "sha256:504a44d7d74bd2bc28a66afc4774912fc21223ca8a67734af1c54ab2f999b489"
    watcher_process: subprocess.Popen[str] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="ac-proxy-proof-") as directory:
            root = Path(directory)
            root.chmod(0o755)
            (root / "marker").write_text("fresh-proxy-test", encoding="utf-8")
            (root / "Caddyfile").write_text(":18082 {\n reverse_proxy 127.0.0.1:18081\n}\n", encoding="utf-8")
            docker("run", "-d", "--name", manager, "--entrypoint", "python", "-v", f"{root}:/fixture:ro", "-w", "/fixture", image, "-m", "http.server", "18081", "--bind", "127.0.0.1")
            docker("run", "-d", "--name", proxy, "--network", f"container:{manager}", "--entrypoint", "caddy", "-v", f"{root}/Caddyfile:/fixture/Caddyfile:ro", caddy, "run", "--config", "/fixture/Caddyfile", "--adapter", "caddyfile")
            wait_http(manager, True)
            manager_id = json.loads(docker("inspect", manager))[0]["Id"]
            proxy_id = json.loads(docker("inspect", proxy))[0]["Id"]
            controller_root = root / "controller"
            controller_root.mkdir(mode=0o700)
            config = controller_root / "watcher.json"
            config.write_text(json.dumps({"state_root": str(controller_root), "manager_id": manager_id, "manager_image": image, "proxy_id": proxy_id, "proxy_image": caddy}), encoding="utf-8")
            config.chmod(0o600)
            watcher_process = subprocess.Popen([sys.executable, "-m", "auto_coder.management.proxy_watch_cli", "--config", str(config)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert watcher_process.stdout is not None and select.select([watcher_process.stdout], [], [], 20)[0]
            assert watcher_process.stdout.readline().strip() == "ATTACHED"
            before = netns(manager)
            assert before == netns(proxy)
            docker("restart", "-t", "3", manager)
            after = netns(manager)
            stale = netns(proxy)
            print("initial proxy HTTP marker readback:", True)
            print("manager/proxy share namespace after manager restart:", after == stale)
            if after != stale:
                wait_http(manager, False)
                print("new manager cannot reach stale proxy: confirmed")
                wait_http(manager, True)
                assert netns(proxy) == netns(manager)
                assert json.loads((controller_root / "proxy-reconnect.json").read_text())["phase"] == "done"
                print("automatic retained proxy reattachment and marker readback: confirmed")
                watcher_process.terminate()
                watcher_process.wait(timeout=10)
                watcher_process = None
                docker("restart", "-t", "3", manager)
                wait_http(manager, False)

                class LostStopReply(DockerProxyObservation):
                    def effect(self, action: str, proxy_id: str) -> None:
                        super().effect(action, proxy_id)
                        if action == "stop":
                            raise TimeoutError("injected lost stop acknowledgement")

                unknown_root = root / "unknown-controller"
                unknown_root.mkdir(mode=0o700)
                binding = ProxyBinding(manager_id, image, proxy_id, caddy)
                unknown = ProxyReconnect(unknown_root, binding, LostStopReply())
                try:
                    unknown.tick()
                except TimeoutError:
                    pass
                else:
                    raise AssertionError("lost acknowledgement was not observed")
                assert json.loads(docker("inspect", proxy))[0]["State"]["Running"] is False
                reconstructed = ProxyReconnect(unknown_root, binding, DockerProxyObservation())
                assert reconstructed.tick() == "NEEDS_ATTENTION"
                assert json.loads((unknown_root / "proxy-reconnect.json").read_text())["phase"] == "stop_unknown"
                assert json.loads(docker("inspect", proxy))[0]["State"]["Running"] is False
                print("lost stop reply survives watcher reconstruction without replay: confirmed")
                docker("run", "-d", "--name", successor, "--network", f"container:{manager}", "--entrypoint", "caddy", "-v", f"{root}/Caddyfile:/fixture/Caddyfile:ro", caddy, "run", "--config", "/fixture/Caddyfile", "--adapter", "caddyfile")
                wait_http(manager, True)
                assert netns(successor) == netns(manager)
                print("successor proxy attached to new namespace and marker readback: confirmed")
                docker("stop", "-t", "3", successor)
                docker("start", proxy)
                wait_http(manager, True)
                assert netns(proxy) == netns(manager)
                print("retained proxy restarted on current manager namespace and marker readback: confirmed")
                docker("restart", "-t", "3", manager)
                wait_http(manager, False)

                class LostStartReply(DockerProxyObservation):
                    def effect(self, action: str, proxy_id: str) -> None:
                        super().effect(action, proxy_id)
                        if action == "start":
                            raise TimeoutError("injected lost start acknowledgement")

                start_unknown_root = root / "unknown-start-controller"
                start_unknown_root.mkdir(mode=0o700)
                unknown_start = ProxyReconnect(start_unknown_root, binding, LostStartReply())
                try:
                    unknown_start.tick()
                except TimeoutError:
                    pass
                else:
                    raise AssertionError("lost start acknowledgement was not observed")
                wait_http(manager, True)
                again = ProxyReconnect(start_unknown_root, binding, DockerProxyObservation())
                assert again.tick() == "NEEDS_ATTENTION"
                assert json.loads((start_unknown_root / "proxy-reconnect.json").read_text())["phase"] == "start_unknown"
                print("lost start reply survives watcher reconstruction without replay: confirmed")
                docker("stop", "-t", "3", proxy)
                docker("stop", "-t", "3", manager)
                docker("run", "-d", "--name", replacement, "--entrypoint", "python", "-v", f"{root}:/fixture:ro", "-w", "/fixture", image, "-m", "http.server", "18081", "--bind", "127.0.0.1")
                docker("rm", successor)
                docker("run", "-d", "--name", successor, "--network", f"container:{replacement}", "--entrypoint", "caddy", "-v", f"{root}/Caddyfile:/fixture/Caddyfile:ro", caddy, "run", "--config", "/fixture/Caddyfile", "--adapter", "caddyfile")
                wait_http(replacement, True)
                assert netns(successor) == netns(replacement)
                print("replacement manager and replacement proxy HTTP readback: confirmed")
                docker("stop", "-t", "3", successor)
                docker("stop", "-t", "3", replacement)
                docker("start", manager)
                docker("start", proxy)
                wait_http(manager, True)
                assert netns(proxy) == netns(manager)
                print("rollback to retained manager and proxy HTTP readback: confirmed")
            else:
                wait_http(manager, True)
                print("namespace survived this restart; proxy readback remains available")
    finally:
        if watcher_process is not None:
            watcher_process.terminate()
            watcher_process.wait(timeout=10)
        for name in (successor, proxy, replacement, manager):
            docker("rm", "-f", name, check=False)


if __name__ == "__main__":
    main()
