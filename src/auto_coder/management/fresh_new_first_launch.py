"""Protected first-NEW-launch originals; not a recovery or retry path.

The trusted host owns the Docker start claim. The selected Supervisor owns
the separate one-use child-launch claim. No missing file authorizes either.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .fresh_begin import NewOriginReceipt, _canonical, inspect_new_instance_origin
from .fresh_initialization import _read_record, _write_once, application_tree_sha256, request_fresh_host_grant
from .fresh_new_docker import CreatedNewDockerSnapshot
from .fresh_new_profile import NewProfileReceipt, inspect_new_instance_profile
from .fresh_preflight import _file, _open

HOST_FIRST_START = "new-docker-first-start.json"
CHILD_FIRST_CLAIM = "new-first-launch-claimed.json"
CHILD_LAUNCHED = "new-first-launched.json"
CHILD_OUTCOME = "new-first-outcome.json"
SOCKET = "fresh-guard.sock"


@dataclass(frozen=True)
class NewFirstStart:
    request_file: Path
    origin: NewOriginReceipt
    profile: NewProfileReceipt
    created: CreatedNewDockerSnapshot
    host_claim_sha256: str
    launch_nonce: str


@dataclass(frozen=True)
class NewReadinessIntent:
    request_id: str
    runtime_reference: str
    management_config_sha256: str
    application_sha256: str


def new_readiness_intent(start: NewFirstStart) -> NewReadinessIntent:
    """Bind the original selection to the normal authenticated reader."""
    request = json.loads(_file(start.request_file, private=True, limit=65536)[1])
    management = _file(Path(request["management_config"]), private=True)[0]
    return NewReadinessIntent(
        start.origin.request_id,
        start.profile.runtime_reference,
        management.sha256,
        application_tree_sha256(Path(request["application_root"])),
    )


def _verify_guard_socket(path: Path) -> None:
    with _open(path.parent, directory=True) as directory:
        info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("NEW host guard socket identity is unsupported")


def load_new_first_start(
    request_file: Path,
    *,
    state_entries: frozenset[str] | None = frozenset(),
    operation_entries: frozenset[str] | None = frozenset(),
) -> NewFirstStart:
    """Reopen the original claimed Docker start and separately verify inputs.

    This does not make a submitted claim truthful. The host peer must still
    prove its own current Docker/namespace evidence in a fresh challenge.
    """
    guard_entries = frozenset({HOST_FIRST_START, SOCKET})
    origin = inspect_new_instance_origin(request_file, guard_entries=guard_entries, state_entries=state_entries, operation_entries=operation_entries)
    profile = inspect_new_instance_profile(request_file, guard_entries=guard_entries, state_entries=state_entries, operation_entries=operation_entries)
    request = json.loads(_file(request_file, private=True, limit=65536)[1])
    guard = Path(request["guard_root"])
    _verify_guard_socket(guard / SOCKET)
    before, encoded = _file(guard / HOST_FIRST_START, private=True, limit=65536)
    record = json.loads(encoded)
    if not isinstance(record, dict) or set(record) != {"schema_version", "profile", "created", "launch_nonce", "producer_lifetime"} or record["schema_version"] != 1 or record["profile"] != "genuinely-new-docker-v1" or encoded != _canonical(record):
        raise ValueError("NEW Docker host first-start claim is not canonical")
    from .fresh_begin import _verify_origin_lifetime

    _verify_origin_lifetime(record["producer_lifetime"])
    nonce = record["launch_nonce"]
    if not isinstance(nonce, str) or len(nonce) != 64 or any(char not in "0123456789abcdef" for char in nonce):
        raise ValueError("NEW first-start host nonce is invalid")
    raw_created = record["created"]
    if not isinstance(raw_created, dict) or set(raw_created) != set(CreatedNewDockerSnapshot.__dataclass_fields__):
        raise ValueError("NEW Docker created-state claim fields differ")
    created = CreatedNewDockerSnapshot(**raw_created)
    from .bootstrap_supervisor import BootstrapConfig

    bootstrap = BootstrapConfig.read(Path(request["bootstrap_config"]))
    if (
        created.request_id != origin.request_id
        or created.origin_sha256 != origin.record_sha256
        or created.status != "docker-created-snapshot-only"
        or created.launch_authorized is not False
        or not isinstance(created.engine_id, str)
        or not isinstance(created.container_id, str)
        or len(created.container_id) != 64
        or any(char not in "0123456789abcdef" for char in created.container_id)
        or not isinstance(created.image_id, str)
        or len(created.image_id) != 71
        or not created.image_id.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in created.image_id[7:])
        or not isinstance(created.mounts_sha256, str)
        or len(created.mounts_sha256) != 64
        or any(char not in "0123456789abcdef" for char in created.mounts_sha256)
        or not isinstance(created.entrypoint, list)
        or not isinstance(created.command, list)
        or not isinstance(created.network_id, str)
        or type(created.published_host_port) is not int
        or ((created.network_id != "" or created.published_host_port != 0) if bootstrap.new_docker_network is None else (re.fullmatch(r"[0-9a-f]{64}", created.network_id) is None or created.published_host_port != bootstrap.new_docker_host_port))
    ):
        raise ValueError("NEW Docker host created-state claim differs")
    # The original dataclass contains tuples, but canonical JSON stores lists.
    created = CreatedNewDockerSnapshot(**{**raw_created, "entrypoint": tuple(created.entrypoint), "command": tuple(created.command)})
    if _file(guard / HOST_FIRST_START, private=True, limit=65536)[0] != before:
        raise ValueError("NEW host first-start original changed")
    return NewFirstStart(request_file, origin, profile, created, before.sha256, nonce)


def claim_new_child_first_launch(start: NewFirstStart, owner_incarnation: str, state_root: Path) -> str:
    """Require the one-shot host challenge and durably consume child launch."""
    if str(uuid.UUID(owner_incarnation)) != owner_incarnation:
        raise ValueError("NEW owner incarnation is invalid")
    request = json.loads(_file(start.request_file, private=True, limit=65536)[1])
    if state_root != Path(request["state_root"]):
        raise ValueError("NEW first-launch state root differs")
    if (state_root / CHILD_FIRST_CLAIM).exists() or (state_root / CHILD_FIRST_CLAIM).is_symlink():
        raise FileExistsError("NEW child first launch already claimed or unresolved")
    challenge = request_fresh_host_grant(
        Path(request["guard_root"]) / SOCKET,
        request_id=start.origin.request_id,
        intent_sha256=start.origin.record_sha256,
        owner_incarnation=owner_incarnation,
        new_container_id=start.created.container_id,
    )
    value = {"schema_version": 1, "host_claim_sha256": start.host_claim_sha256, "host_launch_nonce": start.launch_nonce, **challenge}
    _write_once(state_root, CHILD_FIRST_CLAIM, value)
    return hashlib.sha256(_canonical(value)).hexdigest()


def record_new_child_launch(start: NewFirstStart, owner_incarnation: str, state_root: Path, *, child_identity: str, verification_timeout: float) -> dict[str, object]:
    """Record a single observed OS child without claiming readiness."""
    claim = _read_record(state_root, CHILD_FIRST_CLAIM)
    challenge = claim.get("challenge")
    if (
        set(claim) != {"schema_version", "host_claim_sha256", "host_launch_nonce", "request_id", "intent_sha256", "owner_incarnation", "new_container_id", "challenge"}
        or claim.get("schema_version") != 1
        or claim.get("host_claim_sha256") != start.host_claim_sha256
        or claim.get("host_launch_nonce") != start.launch_nonce
        or claim.get("request_id") != start.origin.request_id
        or claim.get("intent_sha256") != start.origin.record_sha256
        or claim.get("owner_incarnation") != owner_incarnation
        or claim.get("new_container_id") != start.created.container_id
        or not isinstance(challenge, str)
        or len(challenge) != 64
        or any(char not in "0123456789abcdef" for char in challenge)
        or not isinstance(child_identity, str)
        or not child_identity
        or len(child_identity) > 512
        or not 0 < verification_timeout <= 3600
    ):
        raise ValueError("NEW first-launch child claim or identity differs")
    now = time.time()
    observation: dict[str, object] = {
        "request_id": start.origin.request_id,
        "owner_incarnation": owner_incarnation,
        "child_identity": child_identity,
        "observation_id": secrets.token_hex(16),
        "started": now,
        "deadline": now + verification_timeout,
        "host_claim_sha256": start.host_claim_sha256,
    }
    _write_once(state_root, CHILD_LAUNCHED, observation)
    return observation


def record_new_child_outcome(state_root: Path, observation: dict[str, object], *, status: str) -> None:
    """Keep an unknown or succeeded receipt; never reissue a first launch."""
    if status not in {"unknown", "succeeded"} or _read_record(state_root, CHILD_LAUNCHED) != observation:
        raise ValueError("NEW child outcome does not match its original launch")
    _write_once(state_root, CHILD_OUTCOME, {"status": status, "request_id": observation.get("request_id"), "observation_id": observation.get("observation_id")})
