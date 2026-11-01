"""Trusted-host persistent proxy namespace watcher; no application control."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import time
from pathlib import Path

from .proxy_reconnect import DockerProxyObservation, ProxyBinding, ProxyReconnect


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch one pinned management proxy namespace")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    if not args.config.is_absolute() or args.config.is_symlink():
        raise ValueError("absolute protected config required")
    descriptor = os.open(args.config, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        content = stream.read(8193)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or len(content) > 8192:
        raise PermissionError("proxy watcher config is not private")
    raw = json.loads(content)
    if not isinstance(raw, dict) or set(raw) != {"state_root", "manager_id", "manager_image", "proxy_id", "proxy_image", "config_sha256"} or any(not isinstance(value, str) for value in raw.values()):
        raise ValueError("proxy watcher config differs")
    root = Path(raw["state_root"])
    binding = ProxyBinding(raw["manager_id"], raw["manager_image"], raw["proxy_id"], raw["proxy_image"], raw["config_sha256"])
    controller = ProxyReconnect(root, binding, DockerProxyObservation())
    lock = os.open(root / "proxy-watch.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError("proxy watcher lock is not private")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = ""
        while True:
            try:
                status = controller.tick()
            except Exception as error:
                status = "UNAVAILABLE:" + type(error).__name__
            if status != previous:
                print(status, flush=True)
                previous = status
            time.sleep(5)
    finally:
        os.close(lock)


if __name__ == "__main__":
    raise SystemExit(main())
