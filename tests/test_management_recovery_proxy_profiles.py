"""Real protected mount checks for recovery's two explicit proxy profiles.

The typed selector and Docker description are component inputs, not evidence
of a real running deployment. Full native capture remains a separate VM gate.
"""

from __future__ import annotations

import hashlib
import os
import pwd
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from auto_coder.management.containment_route_manifest import DeploymentRootBinding
from auto_coder.management.recovery_containment_model import RecoveryContainmentUnavailable, RecoveryFile
from auto_coder.management.recovery_containment_profile import digest, validate_proxy_profile
from tests.test_management_recovery_containment import _enrollment


@contextmanager
def proxy_case(destinations: tuple[tuple[str, bool], ...]):
    with tempfile.TemporaryDirectory(prefix="ac-recovery-proxy-", dir=pwd.getpwuid(os.geteuid()).pw_dir) as directory:
        root = Path(directory)
        config = root / "Caddyfile"
        config.write_bytes(b"{\n admin off\n auto_https off\n}\n")
        config.chmod(0o600)
        selected = _enrollment().originals[0]
        bindings = []
        mounts = []
        for index, (destination, writable) in enumerate(destinations):
            path = config if destination == "/etc/caddy/Caddyfile" else root / str(index)
            if path != config:
                path.mkdir(mode=0o700)
            info = path.stat()
            bindings.append(DeploymentRootBinding(selected.proxy_id, destination, str(path), writable, info.st_dev, info.st_ino, info.st_mode))
            mounts.append({"Type": "bind", "Source": str(path), "Destination": destination, "RW": writable})
        info = config.stat()
        selected = replace(selected, proxy_config=RecoveryFile(str(config), hashlib.sha256(config.read_bytes()).hexdigest(), info.st_dev, info.st_ino), mounts=tuple(bindings))
        inspection = {
            "Id": selected.proxy_id,
            "Image": selected.proxy_image_id,
            "HostConfig": {"Privileged": False, "PidMode": "", "CapAdd": None, "Devices": [], "SecurityOpt": None, "NetworkMode": "container:" + selected.management_id},
            "Config": {"User": "", "WorkingDir": "/srv", "Entrypoint": ["caddy"], "Cmd": ["run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]},
            "Mounts": mounts,
        }
        yield selected, inspection


@pytest.mark.parametrize("destinations", [(("/etc/caddy/Caddyfile", False),), (("/etc/caddy/Caddyfile", False), ("/data", True), ("/config", True))])
def test_recovery_proxy_accepts_exact_ordinary_or_original_storage_effects(destinations):
    with proxy_case(destinations) as (selected, inspection):
        assert validate_proxy_profile(selected, inspection, expected_uid=os.geteuid()) == digest([inspection])


@pytest.mark.parametrize(
    "destinations",
    [
        (("/etc/caddy/Caddyfile", False), ("/data", True)),
        (("/etc/caddy/Caddyfile", False), ("/config", True)),
        (("/etc/caddy/Caddyfile", False), ("/data", False), ("/config", True)),
        (("/etc/caddy/Caddyfile", False), ("/data", True), ("/config", False)),
        (("/etc/caddy/Caddyfile", True),),
        (("/etc/caddy/Caddyfile", False), ("/unselected", True)),
        (("/etc/caddy/Caddyfile", False), ("/unselected", False)),
        (("/etc/caddy/Caddyfile", False), ("/data", True), ("/config", True), ("/extra", False)),
    ],
)
def test_recovery_proxy_refuses_partial_or_additional_effects(destinations):
    with proxy_case(destinations) as (selected, inspection):
        with pytest.raises(RecoveryContainmentUnavailable, match="mount effects"):
            validate_proxy_profile(selected, inspection, expected_uid=os.geteuid())
