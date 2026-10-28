"""Read-only Docker and kernel observations for a bounded host cutover.

These facts are not a grant: a caller must retain the owner fence and bind a
fresh observation to the replacement supervisor's one-use challenge.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import socket
import sqlite3
import stat
import struct
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .fresh_initialization import FreshInitializationIntent


class HostObservationUnavailable(ValueError):
    """The exact prior containment or lifetime could not be established."""


@dataclass(frozen=True)
class PriorTask:
    pid: int
    start_token: str


@dataclass(frozen=True)
class PriorContainer:
    engine_id: str
    container_id: str
    image_id: str
    started_at: str
    restart_count: int
    pid: int
    start_token: str
    pid_namespace: str
    net_namespace: str
    network_mode: str
    mounts_sha256: str
    cgroup_path: str
    cgroup_device: int
    cgroup_inode: int
    tasks: tuple[PriorTask, ...]
    owner_lock_device: int = 0
    owner_lock_inode: int = 0
    instance_id: str = ""
    bootstrap_source: str = ""
    bootstrap_sha256: str = ""
    published_port: int = 0
    oauth_source: str = ""
    oauth_device: int = 0
    oauth_inode: int = 0

    def validate(self) -> None:
        if not isinstance(self.engine_id, str) or not self.engine_id or len(self.engine_id) > 128 or not isinstance(self.container_id, str) or len(self.container_id) != 64 or any(c not in "0123456789abcdef" for c in self.container_id):
            raise ValueError("invalid prior engine or container identity")
        if not isinstance(self.image_id, str) or len(self.image_id) != 71 or not self.image_id.startswith("sha256:") or any(c not in "0123456789abcdef" for c in self.image_id[7:]):
            raise ValueError("invalid prior image identity")
        if not isinstance(self.started_at, str) or not self.started_at or len(self.started_at) > 128 or type(self.restart_count) is not int or self.restart_count < 0:
            raise ValueError("invalid prior container start identity")
        if type(self.pid) is not int or self.pid <= 0 or not isinstance(self.start_token, str) or not self.start_token.isdecimal() or int(self.start_token) <= 0 or str(int(self.start_token)) != self.start_token:
            raise ValueError("invalid prior supervisor lifetime")
        if not isinstance(self.network_mode, str) or not self.network_mode or self.cgroup_path != "/system.slice/docker-" + self.container_id + ".scope":
            raise ValueError("invalid prior containment identity")
        if not isinstance(self.mounts_sha256, str) or len(self.mounts_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.mounts_sha256):
            raise ValueError("invalid prior mount identity")
        for namespace, prefix in ((self.pid_namespace, "pid:["), (self.net_namespace, "net:[")):
            if not isinstance(namespace, str) or not namespace.startswith(prefix) or not namespace.endswith("]") or not namespace[len(prefix) : -1].isdecimal():
                raise ValueError("invalid prior namespace")
        for value in (self.cgroup_device, self.owner_lock_device):
            if type(value) is not int or value < 0:
                raise ValueError("invalid prior device")
        if type(self.cgroup_inode) is not int or self.cgroup_inode <= 0 or type(self.owner_lock_inode) is not int or self.owner_lock_inode < 0:
            raise ValueError("invalid prior inode")
        if (
            not isinstance(self.tasks, tuple)
            or not self.tasks
            or len(self.tasks) > 4096
            or any(not isinstance(task, PriorTask) or type(task.pid) is not int or task.pid <= 0 or not isinstance(task.start_token, str) or not task.start_token.isdecimal() or int(task.start_token) <= 0 for task in self.tasks)
        ):
            raise ValueError("invalid prior task list")
        if len({task.pid for task in self.tasks}) != len(self.tasks) or PriorTask(self.pid, self.start_token) not in self.tasks:
            raise ValueError("prior supervisor task is absent")


def _engine_id() -> str:
    try:
        result = subprocess.run(["docker", "info", "--format", "{{json .ID}}"], check=True, capture_output=True, text=True, timeout=10)
        value = json.loads(result.stdout)
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError("invalid engine ID")
        return value
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise HostObservationUnavailable("Docker engine identity unavailable") from exc


def _inspect(container_id: str) -> list[object]:
    if len(container_id) != 64 or any(c not in "0123456789abcdef" for c in container_id):
        raise HostObservationUnavailable("exact container ID required")
    fields = (
        ".Id",
        ".Image",
        ".State.Running",
        ".State.Pid",
        ".State.Status",
        ".HostConfig.Privileged",
        ".HostConfig.PidMode",
        ".HostConfig.CapAdd",
        ".HostConfig.SecurityOpt",
        ".HostConfig.NetworkMode",
        ".HostConfig.Devices",
        ".Mounts",
        ".HostConfig.ReadonlyRootfs",
        ".State.StartedAt",
        ".RestartCount",
        ".Config.Entrypoint",
        ".Config.Cmd",
        ".HostConfig.PortBindings",
        ".NetworkSettings.Networks",
    )
    template = "\\n".join("{{json " + field + "}}" for field in fields)
    try:
        result = subprocess.run(["docker", "inspect", "--type", "container", "--format", template, container_id], check=True, capture_output=True, text=True, timeout=10)
        values = [json.loads(value) for value in result.stdout.split("\\n")]
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise HostObservationUnavailable("container inspection unavailable") from exc
    if len(values) != len(fields) or values[0] != container_id:
        raise HostObservationUnavailable("container inspection identity differs")
    return values


def capture_prior_container(container_id: str, image_id: str, *, expected_mounts: tuple[tuple[str, str, bool], ...] = (), require_owner_lock: bool = False, require_deployment_identity: bool = False) -> PriorContainer:
    """Observe the actual live container and its supported containment profile."""
    engine = _engine_id()
    values = _inspect(container_id)
    _, image, running, pid, status, privileged, pid_mode, caps, security, network, devices, mounts = values[:12]
    started_at, restart_count = values[13:15]
    if image != image_id or running is not True or type(pid) is not int or pid <= 0 or status != "running":
        raise HostObservationUnavailable("prior container is not the expected live instance")
    if privileged is not False or pid_mode not in ("", None) or caps not in (None, []) or security not in (None, []) or network in ("host", None, "") or devices not in (None, []):
        raise HostObservationUnavailable("unsupported prior container containment")
    allowed_destinations = {"/bootstrap", "/management-child-state", "/management-oauth", "/management-operations", "/management-policy", "/management-versions", "/run/firebase-admin", "/run/secrets/release_worker_bridge", "/run/secrets/beta_worker_bridge", "/etc/caddy/Caddyfile"}
    if not isinstance(mounts, list) or any(not isinstance(mount, dict) or mount.get("Type") != "bind" or mount.get("Destination") not in allowed_destinations or not isinstance(mount.get("Source"), str) or type(mount.get("RW")) is not bool for mount in mounts):
        raise HostObservationUnavailable("prior container mount boundary is unsupported")
    observed_mounts = tuple(sorted((str(mount["Source"]), str(mount["Destination"]), bool(mount["RW"])) for mount in mounts))
    if observed_mounts != tuple(sorted(expected_mounts)) or any(source in {"/", "/proc", "/sys", "/dev", "/run", "/var/run"} or source.startswith(("/proc/", "/sys/", "/dev/", "/run/", "/var/run/")) for source, _, _ in observed_mounts):
        raise HostObservationUnavailable("prior container mounts differ from the approved profile")
    try:
        proc = Path("/proc") / str(pid)
        fields = (proc / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        token = fields[19]
        if not token.isdecimal() or int(token) <= 0:
            raise ValueError("invalid process start token")
        pid_namespace = os.readlink(proc / "ns/pid")
        net_namespace = os.readlink(proc / "ns/net")
        cgroup = (proc / "cgroup").read_text(encoding="utf-8").splitlines()
        expected = "/system.slice/docker-" + container_id + ".scope"
        if cgroup != ["0::" + expected]:
            raise ValueError("unsupported cgroup layout")
        cgroup_mounts = []
        for line in (proc / "mountinfo").read_text(encoding="utf-8").splitlines():
            halves = line.split(" - ", 1)
            if len(halves) != 2:
                raise ValueError("prior mount evidence is malformed")
            left, right = halves[0].split(), halves[1].split()
            if len(left) < 6 or len(right) < 3:
                raise ValueError("prior mount evidence is incomplete")
            if right[0] in {"cgroup", "cgroup2"}:
                cgroup_mounts.append((left[3], left[4], left[5].split(","), right[0]))
        if len(cgroup_mounts) != 1 or cgroup_mounts[0][0] != expected or cgroup_mounts[0][1] != "/sys/fs/cgroup" or "ro" not in cgroup_mounts[0][2] or cgroup_mounts[0][3] != "cgroup2":
            raise ValueError("prior cgroup mount is not confined and read-only")
        root = Path("/sys/fs/cgroup") / expected.lstrip("/")
        info = root.stat()
        tasks = _cgroup_tasks(root)
        if pid not in tasks:
            raise ValueError("cgroup task evidence unavailable")
        lifetimes = []
        for task in sorted(tasks):
            task_proc = Path("/proc") / str(task)
            task_fields = (task_proc / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            if len(task_fields) < 20 or not task_fields[19].isdecimal() or os.readlink(task_proc / "ns/pid") != pid_namespace or os.readlink(task_proc / "ns/net") != net_namespace:
                raise ValueError("prior cgroup task lifetime differs")
            lifetimes.append(PriorTask(task, task_fields[19]))
        if _cgroup_tasks(root) != tasks:
            raise ValueError("prior cgroup tasks changed during observation")
        final = _inspect(container_id)
        if final[2:5] != [True, pid, "running"] or final[13:15] != [started_at, restart_count] or _engine_id() != engine:
            raise ValueError("prior container changed during observation")
        if not isinstance(network, str):
            raise ValueError("network mode unavailable")
        lock_device, lock_inode = 0, 0
        if require_owner_lock:
            sources = [source for source, destination, _ in observed_mounts if destination == "/management-child-state"]
            if len(sources) != 1:
                raise ValueError("prior supervisor state mount unavailable")
            lock = Path(sources[0]) / ".supervisor-owner.lock"
            descriptor = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                lock_info = os.fstat(descriptor)
                if not stat.S_ISREG(lock_info.st_mode):
                    raise ValueError("prior owner lock is not regular")
                _assert_pid_flock(pid, descriptor, "WRITE")
                lock_device, lock_inode = lock_info.st_dev, lock_info.st_ino
            finally:
                os.close(descriptor)
        mount_digest = hashlib.sha256(json.dumps(observed_mounts, separators=(",", ":")).encode()).hexdigest()
        if not isinstance(started_at, str) or type(restart_count) is not int:
            raise ValueError("prior container start identity is unavailable")
        instance_id, bootstrap_source, bootstrap_sha256, published_port = "", "", "", 0
        oauth_source, oauth_device, oauth_inode = "", 0, 0
        if require_deployment_identity:
            if values[15:17] != [["python", "-m", "auto_coder.management.bootstrap_supervisor"], ["--config", "/bootstrap/config.json"]]:
                raise ValueError("prior supervisor entrypoint differs")
            sources = [source for source, destination, rw in observed_mounts if destination == "/bootstrap" and not rw]
            if len(sources) != 1:
                raise ValueError("prior bootstrap config mount differs")
            config_path = Path(sources[0]) / "config.json"
            descriptor = os.open(config_path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                config_bytes = stream.read(65537)
            if len(config_bytes) > 65536:
                raise ValueError("prior bootstrap config is too large")
            config = json.loads(config_bytes)
            instance_id = config.get("instance_id")
            state_sources = [source for source, destination, _ in observed_mounts if destination == "/management-child-state"]
            oauth_sources = [source for source, destination, _ in observed_mounts if destination == "/management-oauth"]
            if config.get("state_root") != "/management-child-state" or config.get("oauth_root") != "/management-oauth" or len(state_sources) != 1 or len(oauth_sources) != 1:
                raise ValueError("prior state or OAuth mount differs")
            ledger = Path(state_sources[0]) / "management-child.sqlite3"
            if ledger.is_symlink() or not ledger.is_file():
                raise ValueError("prior supervisor ledger unavailable")
            with sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True, timeout=2) as connection:
                rows = connection.execute("SELECT instance_id,incarnation,pid,start_token FROM supervisor_presence").fetchall()
            if len(rows) != 1 or rows[0][0] != instance_id or rows[0][2:] != (1, token) or not isinstance(rows[0][1], str) or str(uuid.UUID(rows[0][1])) != rows[0][1]:
                raise ValueError("prior live supervisor identity differs")
            oauth_source = oauth_sources[0]
            descriptor = os.open(oauth_source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                oauth_stat = os.fstat(descriptor)
                oauth_device, oauth_inode = oauth_stat.st_dev, oauth_stat.st_ino
            finally:
                os.close(descriptor)
            final = _inspect(container_id)
            current_fields = (proc / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            if final[2:5] != [True, pid, "running"] or final[13:15] != [started_at, restart_count] or len(current_fields) < 20 or current_fields[19] != token:
                raise ValueError("prior supervisor changed during identity observation")
            bindings = values[17]
            if not isinstance(instance_id, str) or not instance_id or not isinstance(bindings, dict) or set(bindings) != {"8080/tcp"}:
                raise ValueError("prior instance or port binding unavailable")
            binding = bindings["8080/tcp"]
            if not isinstance(binding, list) or len(binding) != 1 or not isinstance(binding[0], dict) or binding[0].get("HostIp") != "127.0.0.1" or not isinstance(binding[0].get("HostPort"), str) or not binding[0]["HostPort"].isdecimal() or not 0 < int(binding[0]["HostPort"]) <= 65535:
                raise ValueError("prior published endpoint differs")
            bootstrap_source, bootstrap_sha256, published_port = str(config_path), hashlib.sha256(config_bytes).hexdigest(), int(binding[0]["HostPort"])
        return PriorContainer(
            engine,
            container_id,
            image_id,
            started_at,
            restart_count,
            pid,
            token,
            pid_namespace,
            net_namespace,
            network,
            mount_digest,
            expected,
            info.st_dev,
            info.st_ino,
            tuple(lifetimes),
            lock_device,
            lock_inode,
            instance_id,
            bootstrap_source,
            bootstrap_sha256,
            published_port,
            oauth_source,
            oauth_device,
            oauth_inode,
        )
    except (OSError, sqlite3.Error, ValueError, IndexError, UnicodeError) as exc:
        raise HostObservationUnavailable("prior kernel containment unavailable") from exc


def assert_prior_container_stopped(prior: PriorContainer) -> None:
    """Reject live, replaced, or unverifiable prior process and cgroup state."""
    try:
        prior.validate()
    except (ValueError, TypeError) as exc:
        raise HostObservationUnavailable("prior containment identity is invalid") from exc
    if _engine_id() != prior.engine_id:
        raise HostObservationUnavailable("Docker engine identity changed")
    if prior.instance_id:
        try:
            path = Path(prior.bootstrap_source)
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                content = stream.read(65537)
            if len(content) > 65536 or hashlib.sha256(content).hexdigest() != prior.bootstrap_sha256 or json.loads(content).get("instance_id") != prior.instance_id:
                raise ValueError("prior bootstrap identity changed")
        except (OSError, ValueError, AttributeError) as exc:
            raise HostObservationUnavailable("prior bootstrap identity unavailable") from exc
    values = _inspect(prior.container_id)
    if prior.instance_id:
        mounts = values[11]
        expected_source = str(Path(prior.bootstrap_source).parent)
        if (
            values[15:17] != [["python", "-m", "auto_coder.management.bootstrap_supervisor"], ["--config", "/bootstrap/config.json"]]
            or values[17] != {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(prior.published_port)}]}
            or Path(prior.bootstrap_source).name != "config.json"
            or not isinstance(mounts, list)
            or sum(isinstance(mount, dict) and mount.get("Source") == expected_source and mount.get("Destination") == "/bootstrap" and mount.get("RW") is False for mount in mounts) != 1
        ):
            raise HostObservationUnavailable("prior deployment identity changed")
    if values[1] != prior.image_id or values[2:5] != [False, 0, "exited"] or values[13:15] != [prior.started_at, prior.restart_count]:
        raise HostObservationUnavailable("prior container is not stopped")
    if values[5] is not False or values[6] not in ("", None) or values[7] not in (None, []) or values[8] not in (None, []) or values[9] != prior.network_mode or values[10] not in (None, []) or not isinstance(values[11], list):
        raise HostObservationUnavailable("prior containment changed")
    try:
        mounts = tuple(sorted((str(mount["Source"]), str(mount["Destination"]), mount["RW"]) for mount in values[11] if isinstance(mount, dict) and mount.get("Type") == "bind" and type(mount.get("RW")) is bool))
        if len(mounts) != len(values[11]) or hashlib.sha256(json.dumps(mounts, separators=(",", ":")).encode()).hexdigest() != prior.mounts_sha256:
            raise ValueError("prior mounts changed")
    except (KeyError, TypeError, ValueError) as exc:
        raise HostObservationUnavailable("prior mount evidence unavailable") from exc
    try:
        if not prior.tasks or PriorTask(prior.pid, prior.start_token) not in prior.tasks:
            raise ValueError("prior task evidence is incomplete")
        for task in prior.tasks:
            try:
                descriptor = os.pidfd_open(task.pid, 0)
            except OSError as exc:
                if exc.errno != errno.ESRCH:
                    raise
            else:
                try:
                    fields = (Path("/proc") / str(task.pid) / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
                    if len(fields) < 20 or not fields[19].isdecimal() or fields[19] == task.start_token:
                        raise ValueError("prior process lifetime remains present or unreadable")
                finally:
                    os.close(descriptor)
        root = Path("/sys/fs/cgroup") / prior.cgroup_path.lstrip("/")
        if root.is_symlink():
            raise ValueError("prior cgroup path is a symlink")
        try:
            info = root.stat()
        except FileNotFoundError:
            return
        if (info.st_dev, info.st_ino) != (prior.cgroup_device, prior.cgroup_inode):
            raise ValueError("prior cgroup identity changed")
        if _cgroup_tasks(root):
            raise ValueError("prior cgroup retains tasks")
    except (OSError, ValueError, IndexError, UnicodeError) as exc:
        raise HostObservationUnavailable("prior process absence unavailable") from exc


def _cgroup_tasks(root: Path) -> set[int]:
    def unavailable(error: OSError) -> None:
        raise error

    result: set[int] = set()
    for current, _, _ in os.walk(root, onerror=unavailable):
        for value in (Path(current) / "cgroup.procs").read_text(encoding="ascii").splitlines():
            if not value.isdecimal() or int(value) <= 0 or int(value) in result:
                raise ValueError("cgroup task evidence is incomplete")
            result.add(int(value))
    return result


def assert_known_prior_stack_stopped(management: PriorContainer, proxy: PriorContainer) -> None:
    """Check both known cgroups; this alone does not prove namespace absence."""
    if management.engine_id != proxy.engine_id or proxy.network_mode != "container:" + management.container_id or proxy.net_namespace != management.net_namespace:
        raise HostObservationUnavailable("proxy does not match the prior management namespace")
    assert_prior_container_stopped(management)
    assert_prior_container_stopped(proxy)


def assert_prior_network_pin(descriptor: int, namespace: str) -> None:
    """Keep the observed namespace object alive so its inode cannot be reused."""
    try:
        if type(descriptor) is not int or descriptor < 0 or os.readlink(f"/proc/self/fd/{descriptor}") != namespace:
            raise ValueError("prior namespace pin differs")
        os.fstat(descriptor)
    except (OSError, ValueError) as exc:
        raise HostObservationUnavailable("prior namespace pin unavailable") from exc


def assert_retained_oauth_mount(prior: PriorContainer, intent: FreshInitializationIntent) -> None:
    """Bind the retained OAuth root to the exact old container mount."""
    try:
        if (prior.oauth_source, prior.oauth_device, prior.oauth_inode) != (intent.host_oauth_root, intent.oauth_device, intent.oauth_inode) or not prior.oauth_source or prior.oauth_inode <= 0:
            raise ValueError("retained OAuth mount differs")
        descriptor = os.open(prior.oauth_source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) != (prior.oauth_device, prior.oauth_inode):
                raise ValueError("retained OAuth mount identity changed")
        finally:
            os.close(descriptor)
    except (OSError, ValueError) as exc:
        raise HostObservationUnavailable("retained OAuth provenance unavailable") from exc


def _validate_host_proc_profile(pid_namespace: str, uid_map: str, mountinfo: str) -> None:
    """Require the supported unrestricted initial Linux procfs profile."""
    if pid_namespace != "pid:[4026531836]" or uid_map.split() != ["0", "0", "4294967295"]:
        raise ValueError("observer is not in the initial Linux namespaces")
    proc_mounts = []
    for line in mountinfo.splitlines():
        halves = line.split(" - ", 1)
        if len(halves) != 2:
            raise ValueError("procfs mount evidence is malformed")
        left, right = halves[0].split(), halves[1].split()
        if len(left) < 6 or len(right) < 3:
            raise ValueError("procfs mount evidence is incomplete")
        mountpoint = left[4]
        if mountpoint == "/proc":
            proc_mounts.append((left[3], left[5].split(","), right[0], right[2].split(",")))
        elif re.match(r"^/proc/(?:[0-9]+|self|thread-self)(?:/|$)", mountpoint):
            raise ValueError("procfs task paths are shadowed")
    if len(proc_mounts) != 1:
        raise ValueError("procfs mount is unavailable or ambiguous")
    root, mount_options, fs_type, super_options = proc_mounts[0]
    options = mount_options + super_options
    if root != "/" or fs_type != "proc" or any(option.startswith("hidepid=") and option not in {"hidepid=0", "hidepid=off"} for option in options) or any(option.startswith("subset=") for option in options):
        raise ValueError("procfs visibility is restricted")


def _assert_all_task_networks_absent(proc_root: Path, namespace: str) -> None:
    for process in proc_root.iterdir():
        if not process.name.isdecimal():
            continue
        try:
            tasks = list((process / "task").iterdir())
        except FileNotFoundError:
            if process.exists():
                raise ValueError("process task list is unavailable")
            continue
        if not tasks:
            if process.exists():
                raise ValueError("process task list is empty")
            continue
        for task in tasks:
            if not task.name.isdecimal():
                raise ValueError("task identity is malformed")
            try:
                current = os.readlink(task / "ns/net")
            except FileNotFoundError:
                if task.exists():
                    raise ValueError("task network namespace is unavailable")
                continue
            if current == namespace:
                raise HostObservationUnavailable("prior network namespace still has a task")


def assert_prior_network_unreferenced(namespace: str) -> None:
    """Reject any task in the old network namespace on supported host procfs."""
    if not namespace.startswith("net:[") or not namespace.endswith("]") or not namespace[5:-1].isdecimal():
        raise HostObservationUnavailable("invalid prior network namespace")
    try:
        if os.geteuid() != 0 or os.readlink("/proc/self/ns/pid") != os.readlink("/proc/1/ns/pid") or os.readlink("/proc/self/ns/user") != os.readlink("/proc/1/ns/user"):
            raise HostObservationUnavailable("observer is not in the host PID namespace")
        _validate_host_proc_profile(os.readlink("/proc/self/ns/pid"), Path("/proc/self/uid_map").read_text(encoding="ascii"), Path("/proc/self/mountinfo").read_text(encoding="ascii"))
        _assert_all_task_networks_absent(Path("/proc"), namespace)
    except (OSError, ValueError) as exc:
        raise HostObservationUnavailable("network namespace observation unavailable") from exc


def assert_host_listener_absent(port: int) -> None:
    """Check both host TCP tables without opening or reserving the port."""
    if type(port) is not int or not 0 < port <= 65535:
        raise HostObservationUnavailable("invalid listener port")
    try:
        namespace = os.readlink("/proc/self/ns/net")
        if namespace != os.readlink("/proc/1/ns/net") or os.readlink("/proc/self/ns/pid") != os.readlink("/proc/1/ns/pid"):
            raise ValueError("listener observer is not in its owning namespace")
        for name in ("tcp", "tcp6"):
            lines = (Path("/proc/self/net") / name).read_text(encoding="ascii").splitlines()
            if not lines or "local_address" not in lines[0] or "st" not in lines[0]:
                raise ValueError("listener table unavailable")
            for line in lines[1:]:
                fields = line.split()
                if len(fields) < 4:
                    raise ValueError("listener table incomplete")
                address = fields[1].rsplit(":", 1)
                if len(address) != 2:
                    raise ValueError("listener address malformed")
                if int(address[1], 16) == port and fields[3] == "0A":
                    raise ValueError("host listener remains present")
        if os.readlink("/proc/self/ns/net") != namespace:
            raise ValueError("observer network namespace changed")
    except (OSError, ValueError, IndexError, UnicodeError) as exc:
        raise HostObservationUnavailable("host listener evidence unavailable") from exc


def assert_replacement_published_listener(intent: FreshInitializationIntent) -> None:
    """Prove the host listener is Docker's proxy for this exact replacement.

    This intentionally supports only the Docker userland-proxy profile. Kernel
    NAT-only publishing, inaccessible process descriptors, or extra listeners
    are unknown and therefore cannot authorize the one-use grant.
    """
    try:
        if os.readlink("/proc/self/ns/net") != os.readlink("/proc/1/ns/net") or os.readlink("/proc/self/ns/pid") != os.readlink("/proc/1/ns/pid"):
            raise ValueError("observer is not in the host namespaces")
        values = _inspect(intent.new_container_id)
        if values[1] != intent.image_digest or values[2] is not True or values[17] != {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(intent.host_listener_port)}]}:
            raise ValueError("replacement published endpoint differs")
        networks = values[18]
        if not isinstance(networks, dict):
            raise ValueError("replacement networks unavailable")
        addresses = {network.get("IPAddress") for network in networks.values() if isinstance(network, dict) and isinstance(network.get("IPAddress"), str) and network.get("IPAddress")}
        if not addresses:
            raise ValueError("replacement container address unavailable")

        def listener_inodes() -> set[str]:
            found: set[str] = set()
            for table in ("tcp", "tcp6"):
                lines = (Path("/proc/self/net") / table).read_text(encoding="ascii").splitlines()
                if not lines or "local_address" not in lines[0] or "inode" not in lines[0]:
                    raise ValueError("listener table unavailable")
                for line in lines[1:]:
                    fields = line.split()
                    if len(fields) < 10:
                        raise ValueError("listener table incomplete")
                    address, port = fields[1].rsplit(":", 1)
                    if int(port, 16) == intent.host_listener_port and fields[3] == "0A":
                        if table != "tcp" or address != "0100007F" or not fields[9].isdecimal():
                            raise ValueError("unexpected host listener")
                        found.add(fields[9])
            if len(found) != 1:
                raise ValueError("host listener is missing or ambiguous")
            return found

        inodes = listener_inodes()
        daemon = int(Path("/var/run/docker.pid").read_text(encoding="ascii").strip())
        if daemon <= 0 or os.stat(f"/proc/{daemon}/exe") != os.stat("/usr/bin/dockerd"):
            raise ValueError("Docker daemon identity differs")
        expected_prefix = ["/usr/bin/docker-proxy", "-proto", "tcp", "-host-ip", "127.0.0.1", "-host-port", str(intent.host_listener_port), "-container-ip"]
        socket_owners = subprocess.run(["ss", "-ltnpH", f"sport = :{intent.host_listener_port}"], check=True, capture_output=True, text=True, timeout=10).stdout
        candidates = [int(value) for value in re.findall(r'"docker-proxy",pid=(\d+),fd=\d+', socket_owners)]
        if len(candidates) != 1:
            raise ValueError("Docker proxy is missing or ambiguous")
        pid = candidates[0]
        descriptor = os.pidfd_open(pid)
        try:
            proc = Path("/proc") / str(pid)
            fields = (proc / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
            if len(fields) < 20 or int(fields[1]) != daemon or os.stat(proc / "exe") != os.stat("/usr/bin/docker-proxy"):
                raise ValueError("Docker proxy lifetime or executable differs")
            token = fields[19]
            decoded = [item.decode("utf-8") for item in (proc / "cmdline").read_bytes().split(b"\0") if item]
            if decoded[: len(expected_prefix)] != expected_prefix or len(decoded) != 12 or decoded[8] not in addresses or decoded[9:] != ["-container-port", "8080", "-use-listen-fd"]:
                raise ValueError("Docker proxy destination differs")
            descriptors = {os.readlink(path) for path in (proc / "fd").iterdir()}
            if "socket:[" + next(iter(inodes)) + "]" not in descriptors:
                raise ValueError("Docker proxy does not own the listener")
            after = (proc / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
            if len(after) < 20 or after[19] != token:
                raise ValueError("replacement proxy lifetime changed")
            if listener_inodes() != inodes:
                raise ValueError("replacement listener inode changed")
            final_values = _inspect(intent.new_container_id)

            # Docker reports mounts as an unordered collection. Compare every
            # complete mount object, including duplicate entries, independent
            # of the order returned by successive inspections.
            def normalized(items: list[object]) -> list[object]:
                result = list(items)
                mounts = result[11]
                if not isinstance(mounts, list) or not all(isinstance(mount, dict) for mount in mounts):
                    raise ValueError("replacement mount inspection is malformed")
                result[11] = sorted(json.dumps(mount, sort_keys=True, separators=(",", ":")) for mount in mounts)
                return result

            before_values = normalized(values)
            after_values = normalized(final_values)
            if after_values != before_values:
                changed = [str(index) for index, (before, after_value) in enumerate(zip(before_values, after_values)) if before != after_value]
                raise ValueError("replacement container inspection changed at fields " + ",".join(changed))
        finally:
            os.close(descriptor)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, IndexError, UnicodeError) as exc:
        raise HostObservationUnavailable("replacement published listener evidence unavailable") from exc


def _assert_pid_flock(pid: int, fence_descriptor: int, mode: str) -> None:
    if type(pid) is not int or pid <= 0:
        raise HostObservationUnavailable("invalid replacement process")
    try:
        info = os.fstat(fence_descriptor)
        target = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
        for line in Path("/proc/locks").read_text(encoding="ascii").splitlines():
            fields = line.split()
            if len(fields) < 8 or fields[1:4] != ["FLOCK", "ADVISORY", mode]:
                continue
            device = fields[5].split(":")
            if len(device) != 3:
                raise ValueError("malformed kernel lock identity")
            identity = (int(device[0], 16), int(device[1], 16), int(device[2]))
            if fields[4] == str(pid) and identity == target:
                return
        raise HostObservationUnavailable("replacement does not hold the old owner fence")
    except (OSError, ValueError, UnicodeError) as exc:
        raise HostObservationUnavailable("kernel lock evidence unavailable") from exc


def assert_peer_holds_shared_fence(pid: int, fence_descriptor: int) -> None:
    """Confirm the replacement process holds the very same kernel lock."""
    _assert_pid_flock(pid, fence_descriptor, "READ")


def assert_replacement_owner(intent: FreshInitializationIntent, incarnation: str, peer_pid: int, fence_descriptor: int, guard_directory: Path) -> None:
    """Join the requesting host PID to the exact new container and ledger."""
    intent.validate()
    try:
        values = _inspect(intent.new_container_id)
        _, image, running, pid, status, privileged, pid_mode, caps, security, network, devices, mounts = values[:12]
        if image != intent.image_digest or running is not True or type(pid) is not int or pid != peer_pid or status != "running":
            raise ValueError("replacement container identity differs")
        if values[17] != {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(intent.host_listener_port)}]}:
            raise ValueError("replacement published endpoint differs")
        if values[12] is not True or privileged is not False or pid_mode not in ("", None) or caps not in (None, []) or security not in (None, []) or network in ("host", None, "") or devices not in (None, []) or not isinstance(mounts, list):
            raise ValueError("replacement containment is unsupported")
        expected = {intent.state_root: intent.host_state_root, intent.journal_root: intent.host_journal_root, intent.oauth_root: intent.host_oauth_root}
        allowed_destinations = set(expected) | {"/bootstrap", "/management-policy", "/management-versions", "/management-initialization", "/management-prior-owner.lock", "/run/firebase-admin", "/run/secrets/release_worker_bridge", "/run/secrets/beta_worker_bridge"}
        for mount in mounts:
            if isinstance(mount, dict) and mount.get("Type") == "tmpfs" and mount.get("Destination") == "/tmp":
                continue
            if not isinstance(mount, dict) or mount.get("Type") != "bind" or mount.get("Destination") not in allowed_destinations or not isinstance(mount.get("Source"), str):
                raise ValueError("replacement mount boundary is unsupported")
            source = str(mount["Source"])
            if source in {"/", "/proc", "/sys", "/dev", "/run", "/var/run"} or source.startswith(("/proc/", "/sys/", "/dev/", "/run/", "/var/run/")):
                raise ValueError("replacement mount exposes host control")
            if mount["Destination"] not in expected and mount.get("RW") is not False:
                raise ValueError("replacement supporting mount must be read-only")
        guard_mount = [mount for mount in mounts if isinstance(mount, dict) and mount.get("Destination") == "/management-initialization"]
        if len(guard_mount) != 1 or guard_mount[0].get("Source") != str(guard_directory) or guard_mount[0].get("RW") is not False:
            raise ValueError("replacement host guard mount differs")
        guard_info = guard_directory.stat()
        if guard_directory.is_symlink() or (guard_info.st_dev, guard_info.st_ino) != (intent.guard_device, intent.guard_inode):
            raise ValueError("replacement host guard directory changed")
        lock_mount = [mount for mount in mounts if isinstance(mount, dict) and mount.get("Destination") == "/management-prior-owner.lock"]
        if len(lock_mount) != 1 or lock_mount[0].get("RW") is not False:
            raise ValueError("replacement prior owner fence mount differs")
        lock_info = Path(str(lock_mount[0].get("Source"))).stat()
        if (lock_info.st_dev, lock_info.st_ino) != (intent.owner_lock_device, intent.owner_lock_inode):
            raise ValueError("replacement prior fence source differs")
        for destination, source in expected.items():
            matching = [mount for mount in mounts if isinstance(mount, dict) and mount.get("Destination") == destination]
            if len(matching) != 1 or matching[0].get("Type") != "bind" or matching[0].get("Source") != source or matching[0].get("RW") is not True:
                raise ValueError("replacement state mount differs")
            for mount in mounts:
                if not isinstance(mount, dict) or not isinstance(mount.get("Destination"), str):
                    raise ValueError("replacement mount evidence unavailable")
                other = Path(mount["Destination"])
                target = Path(destination)
                if other != target and (other in target.parents or target in other.parents):
                    raise ValueError("replacement state mount is shadowed")
        for root, device, inode in ((intent.host_state_root, intent.state_device, intent.state_inode), (intent.host_journal_root, intent.journal_device, intent.journal_inode), (intent.host_oauth_root, intent.oauth_device, intent.oauth_inode)):
            path = Path(root)
            info = path.stat()
            if path.is_symlink() or (info.st_dev, info.st_ino) != (device, inode):
                raise ValueError("replacement mounted root identity changed")
        fence = os.fstat(fence_descriptor)
        if (fence.st_dev, fence.st_ino) != (intent.owner_lock_device, intent.owner_lock_inode):
            raise ValueError("old owner fence identity differs")
        assert_peer_holds_shared_fence(peer_pid, fence_descriptor)
        ledger = Path(intent.host_state_root) / "management-child.sqlite3"
        if ledger.is_symlink() or not ledger.is_file():
            raise ValueError("replacement ledger unavailable")
        with sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True, timeout=2) as connection:
            rows = connection.execute("SELECT instance_id,incarnation,pid,start_token FROM supervisor_presence").fetchall()
        fields = (Path("/proc") / str(peer_pid) / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        if len(fields) < 20 or rows != [(intent.new_instance, incarnation, 1, fields[19])]:
            raise ValueError("replacement supervisor lifetime differs")
        if _inspect(intent.new_container_id)[2:5] != [True, peer_pid, "running"]:
            raise ValueError("replacement changed during observation")
    except (OSError, sqlite3.Error, ValueError, TypeError, IndexError) as exc:
        raise HostObservationUnavailable("replacement owner evidence unavailable") from exc


def serve_one_fresh_grant(socket_path: Path, intent: FreshInitializationIntent, management: PriorContainer, proxy: PriorContainer, fence_descriptor: int, network_descriptor: int, *, trusted_uid: int | None = None, timeout: float = 120) -> None:
    """Serve one challenge only after fresh host and replacement checks.

    This foreground host adapter does not start containers and never supplies a
    Docker connection or process-control capability to the replacement.
    """
    if trusted_uid is None:
        trusted_uid = os.geteuid()
    intent.validate()
    evidence = json.dumps({"management": asdict(management), "proxy": asdict(proxy)}, sort_keys=True, separators=(",", ":")).encode()
    if (
        management.container_id != intent.old_container_id
        or management.instance_id != intent.old_instance
        or management.published_port != intent.host_listener_port
        or (management.owner_lock_device, management.owner_lock_inode) != (intent.owner_lock_device, intent.owner_lock_inode)
        or hashlib.sha256(evidence).hexdigest() != intent.prior_evidence_sha256
    ):
        raise HostObservationUnavailable("prior observation differs from initialization intent")
    assert_retained_oauth_mount(management, intent)
    assert_prior_network_pin(network_descriptor, management.net_namespace)
    info = socket_path.parent.stat()
    if not socket_path.is_absolute() or socket_path.parent.is_symlink() or info.st_uid != trusted_uid or stat.S_IMODE(info.st_mode) != 0o700 or (info.st_dev, info.st_ino) != (intent.guard_device, intent.guard_inode) or socket_path.name != "fresh-guard.sock":
        raise HostObservationUnavailable("host guard directory is not protected")
    fence = os.fstat(fence_descriptor)
    if (fence.st_dev, fence.st_ino) != (intent.owner_lock_device, intent.owner_lock_inode):
        raise HostObservationUnavailable("host guard fence differs")
    assert_peer_holds_shared_fence(os.getpid(), fence_descriptor)
    assert_known_prior_stack_stopped(management, proxy)
    assert_prior_network_unreferenced(management.net_namespace)
    assert_host_listener_absent(intent.host_listener_port)
    record = Path(intent.host_state_root) / "fresh-initialization.json"
    if record.is_symlink() or not record.is_file():
        raise HostObservationUnavailable("initialization intent is unavailable")
    encoded = record.read_bytes()
    expected = json.dumps(asdict(intent), sort_keys=True, separators=(",", ":")).encode()
    if encoded != expected:
        raise HostObservationUnavailable("initialization intent differs")
    intent_digest = hashlib.sha256(encoded).hexdigest()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        old_umask = os.umask(0o077)
        try:
            listener.bind(str(socket_path))
            os.chmod(socket_path, 0o600, follow_symlinks=False)
        finally:
            os.umask(old_umask)
        listener.listen(1)
        listener.settimeout(timeout)
        print(json.dumps({"status": "awaiting-replacement", "request_id": intent.request_id}), flush=True)
        connection, _ = listener.accept()
        with connection:
            connection.settimeout(10)
            peer_pid, peer_uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            if peer_uid != trusted_uid:
                raise HostObservationUnavailable("replacement peer is not the trusted owner")
            with connection.makefile("rb") as stream:
                line = stream.readline(4097)
            if len(line) > 4096 or not line.endswith(b"\n"):
                raise HostObservationUnavailable("fresh grant request is incomplete")
            request = json.loads(line)
            if not isinstance(request, dict) or set(request) != {"request_id", "intent_sha256", "owner_incarnation", "new_container_id", "challenge"}:
                raise HostObservationUnavailable("fresh grant request fields differ")
            challenge = request["challenge"]
            incarnation = request["owner_incarnation"]
            if (
                request["request_id"] != intent.request_id
                or request["intent_sha256"] != intent_digest
                or request["new_container_id"] != intent.new_container_id
                or not isinstance(challenge, str)
                or len(challenge) != 64
                or any(c not in "0123456789abcdef" for c in challenge)
                or not isinstance(incarnation, str)
                or str(uuid.UUID(incarnation)) != incarnation
            ):
                raise HostObservationUnavailable("fresh grant request identity differs")
            assert_known_prior_stack_stopped(management, proxy)
            assert_prior_network_pin(network_descriptor, management.net_namespace)
            assert_prior_network_unreferenced(management.net_namespace)
            assert_replacement_owner(intent, incarnation, peer_pid, fence_descriptor, socket_path.parent)
            assert_peer_holds_shared_fence(os.getpid(), fence_descriptor)
            assert_known_prior_stack_stopped(management, proxy)
            assert_prior_network_pin(network_descriptor, management.net_namespace)
            assert_prior_network_unreferenced(management.net_namespace)
            assert_replacement_owner(intent, incarnation, peer_pid, fence_descriptor, socket_path.parent)
            assert_replacement_published_listener(intent)
            connection.sendall(json.dumps({**request, "granted": "host-evidence-verified"}, sort_keys=True, separators=(",", ":")).encode() + b"\n")
            print(json.dumps({"status": "grant-delivered", "request_id": intent.request_id}), flush=True)
