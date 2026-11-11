"""Read-only Docker evidence for a never-started isolated NEW container.

Only the trusted host can observe the Docker engine's created state. This
producer deliberately stops short of reserving a start, modifying Docker,
granting first-launch authority or asserting prior-startup exclusion.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .fresh_begin import inspect_new_instance_origin
from .fresh_host_observation import HostObservationUnavailable, _engine_id
from .fresh_new_profile import inspect_new_instance_profile
from .fresh_preflight import _file


@dataclass(frozen=True)
class CreatedNewDockerSnapshot:
    request_id: str
    origin_sha256: str
    engine_id: str
    container_id: str
    image_id: str
    created_at: str
    entrypoint: tuple[str, ...]
    command: tuple[str, ...]
    mounts_sha256: str
    network_id: str = ""
    published_host_port: int = 0
    status: str = "docker-created-snapshot-only"
    launch_authorized: bool = False


_FIELDS = (
    ".Id",
    ".Image",
    ".Created",
    ".State.Status",
    ".State.Running",
    ".State.Pid",
    ".State.StartedAt",
    ".State.FinishedAt",
    ".RestartCount",
    ".HostConfig.RestartPolicy",
    ".HostConfig.NetworkMode",
    ".HostConfig.PidMode",
    ".HostConfig.Privileged",
    ".HostConfig.ReadonlyRootfs",
    ".HostConfig.CapAdd",
    ".HostConfig.Devices",
    ".HostConfig.PortBindings",
    ".HostConfig.SecurityOpt",
    ".Config.User",
    ".Config.Entrypoint",
    ".Config.Cmd",
    ".Mounts",
    ".HostConfig.PublishAllPorts",
    ".NetworkSettings.Networks",
    ".NetworkSettings.Ports",
)


def _docker_created_fields(container_id: str) -> tuple[object, ...]:
    if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
        raise ValueError("exact immutable Docker container ID required")
    template = "\\n".join("{{json " + field + "}}" for field in _FIELDS)
    try:
        output = subprocess.run(
            ["docker", "inspect", "--type", "container", "--format", template, container_id],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        values = tuple(json.loads(item) for item in output.split("\\n"))
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise HostObservationUnavailable("NEW Docker engine evidence unavailable") from error
    if len(values) != len(_FIELDS) or values[0] != container_id:
        raise HostObservationUnavailable("NEW Docker container identity changed")
    return values


def _dedicated_network_id(name: str, *, expected_container: str | None = None) -> str:
    """Verify a private test bridge with no outward IP masquerading."""
    try:
        output = subprocess.run(
            ["docker", "network", "inspect", "--format", "{{json .}}", name],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        fields = json.loads(output)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise HostObservationUnavailable("NEW dedicated Docker network evidence unavailable") from error
    members = fields.get("Containers")
    expected_members: set[str] = set() if expected_container is None else {expected_container}
    if (
        fields.get("Name") != name
        or re.fullmatch(r"[0-9a-f]{64}", str(fields.get("Id"))) is None
        or fields.get("Driver") != "bridge"
        or fields.get("Scope") != "local"
        or fields.get("Internal") is not False
        or fields.get("Attachable") is not False
        or fields.get("Ingress") is not False
        or fields.get("Options") != {"com.docker.network.bridge.enable_ip_masquerade": "false"}
        or not isinstance(members, dict)
        or set(members) != expected_members
    ):
        raise HostObservationUnavailable("NEW dedicated Docker network scope differs")
    return fields["Id"]


def _network_ports(container_port: int, host_port: int) -> dict[str, list[dict[str, str]]]:
    return {f"{container_port}/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(host_port)}]}


def assert_running_new_network(request_file: Path, created: CreatedNewDockerSnapshot, fields: tuple[object, ...]) -> None:
    """Tie the actual Docker PID-1 namespace to the original network identity."""
    if not created.network_id:
        if created.published_host_port != 0 or fields[10] != "none" or fields[16] not in (None, {}):
            raise HostObservationUnavailable("NEW network-none running scope differs")
        return
    from .bootstrap_supervisor import BootstrapConfig

    request = json.loads(_file(request_file, private=True, limit=65536)[1])
    bootstrap = BootstrapConfig.read(Path(request["bootstrap_config"]))
    name = bootstrap.new_docker_network
    port = urlsplit(bootstrap.listener_url).port
    if name is None or port is None or bootstrap.new_docker_host_port != created.published_host_port:
        raise HostObservationUnavailable("NEW published listener differs from the original selection")
    actual_networks = fields[-2]
    network = actual_networks.get(name) if isinstance(actual_networks, dict) else None
    expected_ports = _network_ports(port, created.published_host_port)
    if (
        fields[10] != name
        or fields[16] != expected_ports
        or fields[-1] != expected_ports
        or not isinstance(actual_networks, dict)
        or set(actual_networks) != {name}
        or not isinstance(network, dict)
        or network.get("NetworkID") != created.network_id
        or not network.get("EndpointID")
        or not network.get("IPAddress")
        or _dedicated_network_id(name, expected_container=created.container_id) != created.network_id
    ):
        raise HostObservationUnavailable("NEW running Docker network or host publication differs")


def _inspect_created_scope(request_file: Path, container_id: str, image_id: str) -> CreatedNewDockerSnapshot:
    """Reject a previously started, unsafe, differently mounted Docker scope."""
    if os.geteuid() == 0:
        raise PermissionError("NEW Docker created-scope observation requires a non-root host operator")
    origin = inspect_new_instance_origin(request_file)
    profile = inspect_new_instance_profile(request_file)
    if origin.request_id != profile.request_id or origin.record_sha256 != profile.origin_sha256:
        raise ValueError("NEW original profile differs")
    request = json.loads(_file(request_file, private=True, limit=65536)[1])
    from .bootstrap_supervisor import BootstrapConfig

    bootstrap = BootstrapConfig.read(Path(request["bootstrap_config"]))
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        raise ValueError("immutable Docker image ID required")
    engine = _engine_id()
    fields = _docker_created_fields(container_id)
    (
        actual_id,
        actual_image,
        created,
        status,
        running,
        pid,
        started,
        finished,
        restarts,
        restart_policy,
        network,
        pid_mode,
        privileged,
        read_only,
        caps,
        devices,
        ports,
        security,
        user,
        entrypoint,
        command,
        mounts,
        publish_all,
        network_settings,
        runtime_ports,
    ) = fields
    if (
        actual_id != container_id
        or actual_image != image_id
        or not isinstance(created, str)
        or not created
        or status != "created"
        or running is not False
        or type(pid) is not int
        or pid != 0
        or started != "0001-01-01T00:00:00Z"
        or finished != "0001-01-01T00:00:00Z"
        or type(restarts) is not int
        or restarts != 0
    ):
        raise HostObservationUnavailable("NEW Docker container was started, replaced or unavailable")
    if (
        restart_policy != {"Name": "no", "MaximumRetryCount": 0}
        or network != (bootstrap.new_docker_network or "none")
        or pid_mode not in ("", None)
        or privileged is not False
        or read_only is not True
        or caps not in (None, [])
        or devices not in (None, [])
        or publish_all is not False
        or security != ["no-new-privileges"]
        or user != f"{os.geteuid()}:{os.getegid()}"
    ):
        raise HostObservationUnavailable("NEW Docker containment or startup policy is unsupported")
    network_id = ""
    host_port = 0
    if bootstrap.new_docker_network is None:
        if ports not in (None, {}) or runtime_ports not in (None, {}):
            raise HostObservationUnavailable("NEW network-none Docker port exposure differs")
    else:
        assert bootstrap.new_docker_host_port is not None
        host_port = bootstrap.new_docker_host_port
        container_port = urlsplit(bootstrap.listener_url).port
        if container_port is None or ports != _network_ports(container_port, host_port) or not isinstance(network_settings, dict) or set(network_settings) != {bootstrap.new_docker_network} or runtime_ports not in (None, {}):
            raise HostObservationUnavailable("NEW Docker published-loopback scope differs")
        selected = network_settings[bootstrap.new_docker_network]
        if not isinstance(selected, dict) or selected.get("IPAMConfig") is not None or selected.get("Links") is not None or selected.get("DriverOpts") is not None or selected.get("NetworkID") or selected.get("EndpointID") or selected.get("IPAddress"):
            raise HostObservationUnavailable("NEW Docker created-network identity differs")
        network_id = _dedicated_network_id(bootstrap.new_docker_network)
    if not isinstance(entrypoint, list) or not entrypoint or not isinstance(command, list) or not command or any(not isinstance(item, str) or not item or len(item) > 4096 for item in [*entrypoint, *command]):
        raise HostObservationUnavailable("NEW Docker executable command is unavailable")
    # Every source is opened independently by the original NEW producer. A
    # Docker bind source/destination pair must match those exact host paths.
    writable = ("state_root", "operation_root", "guard_root", "oauth_root")
    readonly = ("bootstrap_config", "management_config", "executable", "application_root")
    expected = sorted([(request[key], request[key], True) for key in writable] + [(request[key], request[key], False) for key in readonly] + [(str(request_file), str(request_file), False)])
    if not isinstance(mounts, list) or len(mounts) != len(expected):
        raise HostObservationUnavailable("NEW Docker mount scope differs")
    actual: list[tuple[str, str, bool]] = []
    for mount in mounts:
        if not isinstance(mount, dict) or mount.get("Type") != "bind" or not isinstance(mount.get("Source"), str) or not isinstance(mount.get("Destination"), str) or type(mount.get("RW")) is not bool:
            raise HostObservationUnavailable("NEW Docker mount type or identity is unsupported")
        actual.append((mount["Source"], mount["Destination"], mount["RW"]))
    if sorted(actual) != expected:
        raise HostObservationUnavailable("NEW Docker mounts differ from protected original inputs")
    digest = hashlib.sha256(json.dumps(expected, separators=(",", ":")).encode()).hexdigest()
    # This is a bounded read-only Docker-state observation. The caller must
    # still acquire continuous host exclusion and a durable first-launch claim.
    if (
        _docker_created_fields(container_id) != fields
        or _engine_id() != engine
        or (bootstrap.new_docker_network is not None and _dedicated_network_id(bootstrap.new_docker_network) != network_id)
        or inspect_new_instance_origin(request_file) != origin
        or inspect_new_instance_profile(request_file) != profile
    ):
        raise HostObservationUnavailable("NEW Docker or original scope changed during observation")
    return CreatedNewDockerSnapshot(origin.request_id, origin.record_sha256, engine, container_id, image_id, created, tuple(entrypoint), tuple(command), digest, network_id, host_port)


def inspect_created_new_docker(request_file: Path, container_id: str, image_id: str) -> dict[str, object]:
    """Return strictly non-authorizing, independently rechecked Docker facts."""
    return asdict(_inspect_created_scope(request_file, container_id, image_id))
