"""Trusted-host one-effect-at-a-time proxy namespace reconnection.

The caller must provide a private state directory, immutable container/image
identities and the host's exclusive controller lock. This module does not
authorize application startup, issue OAuth credentials, or assert MCP readiness.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

_ID = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class ProxyBinding:
    manager_id: str
    manager_image: str
    proxy_id: str
    proxy_image: str
    config_sha256: str

    def validate(self) -> None:
        if not _ID.fullmatch(self.manager_id) or not _ID.fullmatch(self.proxy_id) or self.manager_id == self.proxy_id:
            raise ValueError("exact distinct container identities required")
        if not self.manager_image.startswith("sha256:") or not _ID.fullmatch(self.manager_image[7:]) or not self.proxy_image.startswith("sha256:") or not _ID.fullmatch(self.proxy_image[7:]):
            raise ValueError("exact image identities required")
        if not _ID.fullmatch(self.config_sha256):
            raise ValueError("exact proxy configuration digest required")


class DockerProxyObservation:
    """Read narrowly selected Docker and kernel state; never print env/config."""

    @staticmethod
    def inspect(container: str) -> dict[str, object]:
        template = "{{json .Id}}\n{{json .Image}}\n{{json .State.Running}}\n{{json .State.Pid}}\n{{json .State.StartedAt}}\n{{json .HostConfig.NetworkMode}}\n{{json .Config.Entrypoint}}\n{{json .Config.Cmd}}"
        result = subprocess.run(["docker", "inspect", "--type", "container", "--format", template, container], check=True, capture_output=True, text=True, timeout=10)
        fields = [json.loads(line) for line in result.stdout.splitlines()]
        if len(fields) != 8 or fields[0] != container or type(fields[2]) is not bool or type(fields[3]) is not int or not isinstance(fields[4], str):
            raise ValueError("container observation unavailable")
        return dict(zip(("id", "image", "running", "pid", "started", "network", "entrypoint", "command"), fields))

    @staticmethod
    def namespace(pid: int) -> str:
        if type(pid) is not int or pid <= 0:
            raise ValueError("container process unavailable")
        namespace = os.readlink(f"/proc/{pid}/ns/net")
        if not re.fullmatch(r"net:\[[0-9]+\]", namespace):
            raise ValueError("network namespace unavailable")
        return namespace

    @staticmethod
    def config_digest(container: str) -> str:
        result = subprocess.run(["docker", "inspect", "--type", "container", "--format", "{{json .Mounts}}", container], check=True, capture_output=True, text=True, timeout=10)
        mounts = json.loads(result.stdout)
        if not isinstance(mounts, list):
            raise ValueError("proxy mount evidence unavailable")
        target = Path("/etc/caddy/Caddyfile")
        matches = [m for m in mounts if isinstance(m, dict) and m.get("Destination") == str(target)]
        if len(matches) != 1 or matches[0].get("Type") != "bind" or matches[0].get("RW") is not False or not isinstance(matches[0].get("Source"), str):
            raise ValueError("proxy configuration bind differs")
        for mount in mounts:
            if not isinstance(mount, dict) or not isinstance(mount.get("Destination"), str):
                raise ValueError("proxy mount evidence unavailable")
            other = Path(mount["Destination"])
            if other != target and (other in target.parents or target in other.parents):
                raise ValueError("proxy configuration bind is shadowed")
        source = Path(matches[0]["Source"])
        if not source.is_absolute() or source.is_symlink() or source != source.resolve():
            raise ValueError("proxy configuration source differs")
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) & 0o022:
                raise ValueError("proxy configuration source is not protected")
            content = stream.read(1024 * 1024 + 1)
            after = os.fstat(stream.fileno())
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if len(content) > 1024 * 1024 or before_identity != after_identity:
            raise ValueError("proxy configuration changed while observed")
        current = source.lstat()
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns) != after_identity:
            raise ValueError("proxy configuration source changed")
        return hashlib.sha256(content).hexdigest()

    @staticmethod
    def effect(action: str, proxy_id: str) -> None:
        if action not in {"stop", "start"}:
            raise ValueError("unsupported proxy effect")
        subprocess.run(["docker", action, proxy_id], check=True, capture_output=True, text=True, timeout=30)


class ProxyReconnect:
    """Durable restart state; uncertain effects require operator inspection."""

    def __init__(self, root: Path, binding: ProxyBinding, driver: DockerProxyObservation) -> None:
        binding.validate()
        if not root.is_absolute() or root.is_symlink():
            raise ValueError("private absolute controller root required")
        info = root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError("controller root is not private")
        self.root, self.binding, self.driver = root, binding, driver

    @staticmethod
    def _pid(value: dict[str, object]) -> int:
        pid = value.get("pid")
        if type(pid) is not int or pid <= 0:
            raise ValueError("container process identity unavailable")
        return pid

    def _read(self) -> dict[str, object] | None:
        path = self.root / "proxy-reconnect.json"
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            raw = stream.read(8193)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or len(raw) > 8192:
            raise ValueError("proxy journal unavailable")
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"binding", "manager", "phase"} or value["binding"] != asdict(self.binding) or value["phase"] not in {"stop_unknown", "stopped", "start_unknown", "done"} or not isinstance(value["manager"], dict):
            raise ValueError("proxy journal identity differs")
        return value

    def _write(self, manager: dict[str, object], phase: str) -> None:
        content = json.dumps({"binding": asdict(self.binding), "manager": manager, "phase": phase}, sort_keys=True, separators=(",", ":")).encode()
        root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            name = ".proxy-reconnect-" + os.urandom(12).hex()
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, "proxy-reconnect.json", src_dir_fd=root_fd, dst_dir_fd=root_fd)
            os.fsync(root_fd)
        finally:
            os.close(root_fd)

    def _manager(self) -> dict[str, object]:
        value = self.driver.inspect(self.binding.manager_id)
        if value["image"] != self.binding.manager_image or value["running"] is not True:
            raise ValueError("manager unavailable or changed")
        namespace = self.driver.namespace(self._pid(value))
        again = self.driver.inspect(self.binding.manager_id)
        if again != value:
            raise ValueError("manager changed during observation")
        return {"id": value["id"], "pid": value["pid"], "started": value["started"], "namespace": namespace}

    def _proxy(self) -> dict[str, object]:
        value = self.driver.inspect(self.binding.proxy_id)
        if (
            value["image"] != self.binding.proxy_image
            or value["network"] != "container:" + self.binding.manager_id
            or value["entrypoint"] != ["caddy"]
            or value["command"] != ["run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
            or self.driver.config_digest(self.binding.proxy_id) != self.binding.config_sha256
        ):
            raise ValueError("proxy binding differs")
        return value

    def tick(self) -> str:
        """Reattach after proven manager reconstruction, never repeat unknowns."""
        manager, proxy, saved = self._manager(), self._proxy(), self._read()
        if saved is not None and saved["phase"] in {"stop_unknown", "start_unknown"}:
            return "NEEDS_ATTENTION"
        if saved is not None and saved["phase"] == "stopped":
            if saved["manager"] != manager or proxy["running"] is not False:
                return "NEEDS_ATTENTION"
        else:
            if proxy["running"] is not True:
                return "NEEDS_ATTENTION"
            namespace = self.driver.namespace(self._pid(proxy))
            if namespace == manager["namespace"]:
                return "ATTACHED"
            if saved is not None and saved["manager"] == manager:
                return "NEEDS_ATTENTION"
            self._write(manager, "stop_unknown")
            if self._manager() != manager or self._proxy() != proxy or self.driver.namespace(self._pid(proxy)) != namespace:
                return "NEEDS_ATTENTION"
            self.driver.effect("stop", self.binding.proxy_id)
            if self._manager() != manager or self._proxy()["running"] is not False:
                return "NEEDS_ATTENTION"
            self._write(manager, "stopped")
        if self._manager() != manager:
            return "NEEDS_ATTENTION"
        self._write(manager, "start_unknown")
        if self._manager() != manager or self._proxy()["running"] is not False:
            return "NEEDS_ATTENTION"
        self.driver.effect("start", self.binding.proxy_id)
        proxy = self._proxy()
        if self._manager() != manager or proxy["running"] is not True or self.driver.namespace(self._pid(proxy)) != manager["namespace"]:
            return "NEEDS_ATTENTION"
        self._write(manager, "done")
        return "ATTACHED"
