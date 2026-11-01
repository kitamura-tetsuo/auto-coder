"""Disposable Docker experiment; never targets an existing container.

Run explicitly: python tests/manual_proxy_namespace_validation.py
"""

import json
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path


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
    for _ in range(30):
        if http(name) is expected:
            return
        time.sleep(0.25)
    raise RuntimeError("proxy readback did not reach expected state")


def reconnect_once(manager: str, proxy: str, *, fail_after_stop: bool = False) -> None:
    """Experiment only: stop and reattach one exact retained proxy once."""
    before = json.loads(docker("inspect", manager))[0]
    observed_namespace = netns(manager)
    if before["State"]["Running"] is not True or netns(proxy) == observed_namespace:
        raise RuntimeError("replacement manager or stale namespace not established")
    docker("stop", "-t", "3", proxy)
    if json.loads(docker("inspect", proxy))[0]["State"]["Running"] is not False:
        raise RuntimeError("proxy stop is not confirmed")
    after = json.loads(docker("inspect", manager))[0]
    if after["Id"] != before["Id"] or after["State"]["Pid"] != before["State"]["Pid"] or netns(manager) != observed_namespace:
        raise RuntimeError("manager changed during proxy stop")
    if fail_after_stop:
        raise RuntimeError("injected interruption after confirmed stop")
    docker("start", proxy)
    if netns(proxy) != observed_namespace or netns(manager) != observed_namespace:
        raise RuntimeError("new proxy namespace differs")
    wait_http(manager, True)


def main() -> None:
    suffix = uuid.uuid4().hex[:12]
    manager = "ac-proxy-proof-manager-" + suffix
    replacement = "ac-proxy-proof-replacement-" + suffix
    proxy = "ac-proxy-proof-proxy-" + suffix
    successor = "ac-proxy-proof-successor-" + suffix
    image = "sha256:5460b5773b335b60e11c259eca633a44ef7f76fe3579a24183703dca76523c32"
    caddy = "sha256:504a44d7d74bd2bc28a66afc4774912fc21223ca8a67734af1c54ab2f999b489"
    try:
        with tempfile.TemporaryDirectory(prefix="ac-proxy-proof-") as directory:
            root = Path(directory)
            root.chmod(0o755)
            (root / "marker").write_text("fresh-proxy-test", encoding="utf-8")
            (root / "Caddyfile").write_text(":18082 {\n reverse_proxy 127.0.0.1:18081\n}\n", encoding="utf-8")
            docker("run", "-d", "--name", manager, "--entrypoint", "python", "-v", f"{root}:/fixture:ro", "-w", "/fixture", image, "-m", "http.server", "18081", "--bind", "127.0.0.1")
            docker("run", "-d", "--name", proxy, "--network", f"container:{manager}", "--entrypoint", "caddy", "-v", f"{root}/Caddyfile:/fixture/Caddyfile:ro", caddy, "run", "--config", "/fixture/Caddyfile", "--adapter", "caddyfile")
            wait_http(manager, True)
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
                outcome: list[BaseException] = []

                def auto_reconnect() -> None:
                    try:
                        reconnect_once(manager, proxy)
                    except BaseException as error:
                        outcome.append(error)

                controller = threading.Thread(target=auto_reconnect)
                controller.start()
                controller.join(timeout=30)
                if controller.is_alive() or outcome:
                    raise RuntimeError("automatic proxy reconnect failed") from (outcome[0] if outcome else None)
                assert netns(proxy) == netns(manager)
                print("automatic retained proxy reattachment and marker readback: confirmed")
                docker("restart", "-t", "3", manager)
                wait_http(manager, False)
                try:
                    reconnect_once(manager, proxy, fail_after_stop=True)
                except RuntimeError as error:
                    assert "injected interruption" in str(error)
                else:
                    raise AssertionError("injected failure was not observed")
                assert json.loads(docker("inspect", proxy))[0]["State"]["Running"] is False
                print("interruption after proxy stop remains stopped without retry: confirmed")
                docker("run", "-d", "--name", successor, "--network", f"container:{manager}", "--entrypoint", "caddy", "-v", f"{root}/Caddyfile:/fixture/Caddyfile:ro", caddy, "run", "--config", "/fixture/Caddyfile", "--adapter", "caddyfile")
                wait_http(manager, True)
                assert netns(successor) == netns(manager)
                print("successor proxy attached to new namespace and marker readback: confirmed")
                docker("stop", "-t", "3", successor)
                docker("start", proxy)
                wait_http(manager, True)
                assert netns(proxy) == netns(manager)
                print("retained proxy restarted on current manager namespace and marker readback: confirmed")
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
        for name in (successor, proxy, replacement, manager):
            docker("rm", "-f", name, check=False)


if __name__ == "__main__":
    main()
