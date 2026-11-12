"""Protected fixed recovery authorization and independently checked READY.

An authorization selects a finite scope. It neither establishes original
history nor replaces the issuer's current Docker, kernel and custody checks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from auto_coder.operating_policy import MANAGEMENT_REPOSITORY

from .archived_exclusions import unique_pairs
from .bootstrap_supervisor import BootstrapConfig
from .bootstrap_upgrade import _source_authority
from .containment_enrollment import _approved_directory
from .original_attempts import HistoryBinding, canonical, read_original
from .recovery_containment_model import RecoveryEnrollment, RecoveryFile
from .recovery_epoch_state import EpochInventory, EpochUnavailable
from .self_update_handoff import runtime_for_receipt
from .version_staging import ManagementReadyReceipt, ManagementRuntimeConfig, ManagementVersionStager

AUTHORIZATION_ROOT = Path("/etc/auto-coder/recovery-epoch")
AUTHORIZATION_FILE = AUTHORIZATION_ROOT / "authorization.json"
PEER_ROOT = Path("/var/lib/auto-coder/recovery-epoch-peer")
PEER_SOCKET = PEER_ROOT / "issuer.sock"
AUTHORIZATION_SCHEMA = "management-recovery-authorization-v1"
_SHA = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z", re.ASCII)


@dataclass(frozen=True)
class EpochAuthorization:
    schema: str
    deployment_id: str
    authorization_id: str
    request_id: str
    preparation_id: str
    target_id: str
    enrollment_id: str
    history: HistoryBinding
    consumer_config: RecoveryFile
    ready_receipt: RecoveryFile
    source_repository: str
    source_token_file: str
    python_executable: str
    uv_executable: str
    uv_version: str
    source_sha: str
    socket_directory_device: int
    socket_directory_inode: int

    @classmethod
    def parse(cls, value: object) -> EpochAuthorization:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise EpochUnavailable("complete fixed recovery authorization required")
        raw = dict(value)
        history = raw["history"]
        if not isinstance(history, dict) or set(history) != set(HistoryBinding.__dataclass_fields__) or any(not isinstance(item, str) for item in history.values()):
            raise EpochUnavailable("original history authorization is incomplete")
        raw["history"] = HistoryBinding(**history)
        raw["consumer_config"] = RecoveryFile.parse(raw["consumer_config"])
        raw["ready_receipt"] = RecoveryFile.parse(raw["ready_receipt"])
        result = cls(**raw)
        if result.schema != AUTHORIZATION_SCHEMA:
            raise EpochUnavailable("unsupported recovery authorization schema")
        for field in ("deployment_id", "authorization_id", "request_id", "preparation_id", "enrollment_id"):
            token = getattr(result, field)
            if not isinstance(token, str) or not _TOKEN.fullmatch(token):
                raise EpochUnavailable("invalid recovery authorization " + field)
        if not isinstance(result.target_id, str) or not re.fullmatch(r"[0-9a-f]{64}", result.target_id) or not isinstance(result.source_sha, str) or not _SHA.fullmatch(result.source_sha):
            raise EpochUnavailable("exact recovery target and source required")
        if result.history.deployment != result.deployment_id:
            raise EpochUnavailable("history names another recovery deployment")
        for path in (result.history.root, result.source_repository, result.source_token_file, result.python_executable, result.uv_executable):
            _path(path)
        if not isinstance(result.uv_version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", result.uv_version):
            raise EpochUnavailable("exact configured installer version required")
        for field in ("socket_directory_device", "socket_directory_inode"):
            if type(getattr(result, field)) is not int or getattr(result, field) <= 0:
                raise EpochUnavailable("fixed recovery peer directory identity required")
        return result


@dataclass(frozen=True)
class AuthorizationObservation:
    authorization: EpochAuthorization
    original: RecoveryFile


@dataclass(frozen=True)
class TargetReadyObservation:
    receipt: ManagementReadyReceipt
    bootstrap_config: RecoveryFile
    management_config: RecoveryFile
    consumer_config: RecoveryFile
    ready_receipt: RecoveryFile
    runtime_sha256: str


def _path(value: object) -> Path:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise EpochUnavailable("invalid recovery authority path")
    path = Path(value)
    if not path.is_absolute() or str(path) != value or path == Path("/") or ".." in path.parts or "\x00" in value:
        raise EpochUnavailable("recovery authority path is not canonical")
    return path


def read_bound_file(bound: RecoveryFile) -> bytes:
    """Read the original protected file, retaining its actual inode and bytes."""
    path = _path(bound.path)
    _approved_directory(path.parent, uid=os.geteuid())
    data, identity = read_original(path)
    if identity != (bound.device, bound.inode) or hashlib.sha256(data).hexdigest() != bound.sha256:
        raise EpochUnavailable("recovery original file changed: " + bound.path)
    return data


def _load_authorization(path: Path, inventory: EpochInventory) -> AuthorizationObservation:
    """Explicit component seam; the production factory uses one fixed path."""
    if inventory.schema_version != 2 or not inventory.authorization_sha256:
        raise EpochUnavailable("complete issuer requires the fixed version-two inventory")
    data, (device, inode) = read_original(path)
    digest = hashlib.sha256(data).hexdigest()
    if digest != inventory.authorization_sha256:
        raise EpochUnavailable("recovery authorization differs from fixed inventory")
    raw = json.loads(data, object_pairs_hook=unique_pairs)
    if canonical(raw) != data:
        raise EpochUnavailable("recovery authorization is not canonical")
    authorization = EpochAuthorization.parse(raw)
    for field in ("deployment_id", "authorization_id", "request_id", "preparation_id", "target_id"):
        if getattr(authorization, field) != getattr(inventory, field):
            raise EpochUnavailable("recovery authorization differs from inventory: " + field)
    if inventory.enrollment_ids != (authorization.enrollment_id,):
        raise EpochUnavailable("recovery enrollment differs from complete fixed scope")
    return AuthorizationObservation(authorization, RecoveryFile(str(path), digest, device, inode))


def load_authorization(inventory: EpochInventory) -> AuthorizationObservation:
    """Read the deployment-fixed authorization; no CLI or environment root."""
    _approved_directory(AUTHORIZATION_ROOT, uid=0)
    result = _load_authorization(AUTHORIZATION_FILE, inventory)
    check_authorization(result, inventory)
    return result


def check_authorization(previous: AuthorizationObservation, inventory: EpochInventory) -> None:
    if _load_authorization(Path(previous.original.path), inventory) != previous:
        raise EpochUnavailable("recovery authorization changed after observation")
    if Path(previous.original.path) != AUTHORIZATION_FILE:
        raise EpochUnavailable("recovery authorization did not use the fixed production path")
    _approved_directory(PEER_ROOT, uid=0)
    peer = PEER_ROOT.stat()
    if (peer.st_dev, peer.st_ino) != (previous.authorization.socket_directory_device, previous.authorization.socket_directory_inode):
        raise EpochUnavailable("fixed recovery peer directory was replaced")


def _same_path_version_mount(enrollment: RecoveryEnrollment, config: BootstrapConfig) -> None:
    version = config.management_version_root
    if version is None:
        raise EpochUnavailable("recovery target has no installed runtime root")
    selected = [mount for mount in enrollment.target.mounts if mount.container_id == enrollment.target.management_id and mount.destination == str(version)]
    if len(selected) != 1 or selected[0].source != str(version) or selected[0].writable:
        raise EpochUnavailable("recovery READY requires its exact original same-path read-only version mount")


def observe_target_ready(authorization: EpochAuthorization, enrollment: RecoveryEnrollment) -> TargetReadyObservation:
    """Invoke the actual stager verifier against the retained installed READY."""
    target = enrollment.target
    if (enrollment.deployment_id, enrollment.authorization_id, enrollment.enrollment_id, target.management_id) != (authorization.deployment_id, authorization.authorization_id, authorization.enrollment_id, authorization.target_id):
        raise EpochUnavailable("current target enrollment differs from recovery authorization")
    read_bound_file(target.bootstrap_config)
    read_bound_file(target.management_config)
    read_bound_file(authorization.consumer_config)
    ready_bytes = read_bound_file(authorization.ready_receipt)
    config = BootstrapConfig.read(Path(target.bootstrap_config.path))
    if config.instance_id != target.instance_id or config.management_config is None or config.management_version_root is None or config.recovery_epoch_config != Path("/bootstrap/recovery-epoch.json"):
        raise EpochUnavailable("exact recovery bootstrap profile is unavailable")
    _same_path_version_mount(enrollment, config)
    mounts = {mount.destination: mount for mount in target.mounts}
    consumer_mount = mounts.get("/bootstrap/recovery-epoch.json")
    if consumer_mount is None or consumer_mount.source != authorization.consumer_config.path or consumer_mount.writable:
        raise EpochUnavailable("target recovery consumer configuration mount differs")
    runtime = next(item for item in config.runtimes if item.reference == config.initial_runtime)
    if runtime.execution_kind != "management-installed" or runtime.ready_receipt != Path(authorization.ready_receipt.path):
        raise EpochUnavailable("recovery target requires the authorized original installed READY")
    # Host verification and the exact same-path RO mount must refer to the
    # same complete interpreter distribution. A host-only symlink target can
    # otherwise produce valid host READY while the Docker child is unusable.
    resolved_interpreter = runtime.executable.resolve(strict=True)
    version_root = config.management_version_root.resolve(strict=True)
    if not resolved_interpreter.is_relative_to(version_root) or resolved_interpreter != Path(authorization.python_executable).resolve(strict=True):
        raise EpochUnavailable("recovery interpreter escapes the exactly mounted runtime closure")
    safe_python = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONUNBUFFERED": "1"}
    if any(name.startswith("LD_") or (name.startswith("PYTHON") and safe_python.get(name) != value) for name, value in runtime.environment):
        raise EpochUnavailable("recovery target runtime contains unsupported executable hooks")
    authority = _source_authority(Path(authorization.source_token_file))
    staging = ManagementVersionStager(
        ManagementRuntimeConfig(
            config.instance_id,
            MANAGEMENT_REPOSITORY,
            Path(authorization.source_repository),
            config.management_version_root,
            Path(authorization.python_executable),
            Path(authorization.uv_executable),
            authorization.uv_version,
            protected_roots=(config.state_root, config.oauth_root, config.journal_root),
            management_config=Path(target.management_config.path),
            source_authority=authority,
        )
    )
    receipt = staging.lookup(Path(authorization.ready_receipt.path))
    if receipt.source_sha != authorization.source_sha or receipt.instance_id != target.instance_id or json.loads(ready_bytes, object_pairs_hook=unique_pairs) != asdict(receipt):
        raise EpochUnavailable("genuine installed READY differs from exact authorization")
    if Path(receipt.environment_path).parent.name != authorization.preparation_id:
        raise EpochUnavailable("installed runtime belongs to another exact preparation")
    expected = runtime_for_receipt(config, receipt)
    if runtime != expected:
        raise EpochUnavailable("target invocation differs from its original installed runtime")
    for original in (target.bootstrap_config, target.management_config, authorization.consumer_config, authorization.ready_receipt):
        read_bound_file(original)
    runtime_bytes = json.dumps(asdict(runtime), sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode()
    return TargetReadyObservation(receipt, target.bootstrap_config, target.management_config, authorization.consumer_config, authorization.ready_receipt, hashlib.sha256(runtime_bytes).hexdigest())


def verify_target_ready(authorization: EpochAuthorization, enrollment: RecoveryEnrollment, previous: TargetReadyObservation) -> TargetReadyObservation:
    current = observe_target_ready(authorization, enrollment)
    if current != previous:
        raise EpochUnavailable("target source, configuration or READY changed before grant")
    return current
