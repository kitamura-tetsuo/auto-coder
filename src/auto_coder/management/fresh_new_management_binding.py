"""Live Docker NEW owner proof required before a container-wide HTTP bind."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path

from .bootstrap_supervisor import BootstrapConfig
from .fresh_host_observation import _assert_pid_flock
from .fresh_initialization import _read_record
from .fresh_new_first_launch import CHILD_FIRST_CLAIM, HOST_FIRST_START
from .fresh_preflight import _file
from .oauth_config import ManagementSettings, _isolated_new_docker_http_binding


def assert_admitted_new_docker_http_binding(settings: ManagementSettings) -> None:
    """Verify the currently running admitted child, not just a YAML selector.

    The trusted host independently verified the original Docker engine,
    network namespace and one-shot first start before issuing the durable
    guard receipt. Only a real child of that still-locked PID 1 may bind the
    container-wide address. No Docker daemon socket is exposed to the child.
    """
    if settings.bootstrap_config_path is None or settings.config_path is None:
        raise ValueError("NEW Docker HTTP owner is not selected")
    bootstrap = BootstrapConfig.read(settings.bootstrap_config_path)
    if not _isolated_new_docker_http_binding(settings, bootstrap):
        raise ValueError("NEW Docker HTTP binding does not match the selected bootstrap")
    if os.name != "posix" or os.getppid() != 1 or os.getpid() == 1:
        raise ValueError("NEW Docker HTTP child must be directly owned by container PID 1")
    for namespace in ("pid", "net"):
        if os.readlink(f"/proc/self/ns/{namespace}") != os.readlink(f"/proc/1/ns/{namespace}"):
            raise ValueError("NEW Docker HTTP child differs from the live owner namespace")

    argv = Path("/proc/1/cmdline").read_bytes().rstrip(b"\0").split(b"\0")
    prefix = (b"python3", b"-B", b"-m", b"auto_coder.management.bootstrap_supervisor", b"--config")
    if len(argv) != 9 or tuple(argv[:5]) != prefix or argv[5] != os.fsencode(settings.bootstrap_config_path) or tuple(argv[6:8]) != (b"--new-initialization", b"--request-file"):
        raise ValueError("NEW Docker HTTP owner is not the admitted PID 1 entrypoint")
    request_file = Path(os.fsdecode(argv[8]))
    original = json.loads(_file(request_file, private=True, limit=65536)[1])
    if (
        not isinstance(original, dict)
        or original.get("new_instance") != bootstrap.instance_id
        or original.get("bootstrap_config") != str(settings.bootstrap_config_path)
        or original.get("management_config") != str(settings.config_path)
        or original.get("oauth_root") != str(settings.state_root)
        or original.get("operation_root") != str(settings.operation_state_root)
        or original.get("state_root") != str(bootstrap.state_root)
        or bootstrap.fresh_guard_path is None
        or original.get("guard_root") != str(bootstrap.fresh_guard_path.parent)
    ):
        raise ValueError("NEW Docker HTTP source differs from the admitted owner")

    lock = bootstrap.state_root / ".supervisor-owner.lock"
    descriptor = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise ValueError("NEW Docker HTTP owner lock is unprotected")
        _assert_pid_flock(1, descriptor, "WRITE")
    finally:
        os.close(descriptor)
    owner_stat = Path("/proc/1/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
    if len(owner_stat) < 20 or not owner_stat[19].isdecimal() or int(owner_stat[19]) <= 0:
        raise ValueError("NEW Docker HTTP owner process lifetime is unavailable")

    ledger = bootstrap.state_root / "management-child.sqlite3"
    if ledger.is_symlink() or not ledger.is_file():
        raise ValueError("NEW Docker HTTP native owner ledger is unavailable")
    with sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True, timeout=2) as connection:
        presence = connection.execute("SELECT instance_id,incarnation,pid,start_token FROM supervisor_presence").fetchall()
        states = connection.execute("SELECT record_json FROM child_state WHERE instance_id=?", (bootstrap.instance_id,)).fetchall()
    if len(presence) != 1 or presence[0][0] != bootstrap.instance_id or presence[0][2:] != (1, owner_stat[19]):
        raise ValueError("NEW Docker HTTP native owner lifetime differs")
    incarnation = presence[0][1]
    if len(states) != 1:
        raise ValueError("NEW Docker HTTP child admission is not recorded")
    state = json.loads(states[0][0])
    if state.get("supervisor_incarnation") != incarnation or state.get("phase") not in {"starting", "running"} or type(state.get("child_generation")) is not int or state["child_generation"] < 1:
        raise ValueError("NEW Docker HTTP child selection is not owned")
    if state.get("phase") == "running" and state.get("pid") != os.getpid():
        raise ValueError("NEW Docker HTTP running child PID differs")

    claim = _read_record(bootstrap.state_root, CHILD_FIRST_CLAIM)
    host_bytes = _file(Path(original["guard_root"]) / HOST_FIRST_START, private=True, limit=65536)[1]
    host = json.loads(host_bytes)
    created = host.get("created", {})
    challenge = claim.get("challenge")
    if (
        not isinstance(created, dict)
        or host.get("schema_version") != 1
        or host.get("profile") != "genuinely-new-docker-v1"
        or claim.get("schema_version") != 1
        or claim.get("request_id") != original.get("request_id")
        or claim.get("owner_incarnation") != incarnation
        or claim.get("host_launch_nonce") != host.get("launch_nonce")
        or claim.get("host_claim_sha256") != hashlib.sha256(host_bytes).hexdigest()
        or claim.get("new_container_id") != created.get("container_id")
        or created.get("request_id") != original.get("request_id")
        or created.get("published_host_port") != bootstrap.new_docker_host_port
        or not isinstance(challenge, str)
        or len(challenge) != 64
        or any(c not in "0123456789abcdef" for c in challenge)
    ):
        raise ValueError("NEW Docker HTTP host admission receipt differs")
