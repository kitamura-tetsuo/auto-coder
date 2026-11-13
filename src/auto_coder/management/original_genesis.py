"""Prospective ordinary-deployment originals, never recovery or launch authority.

A separately authorized capture observes two never-started local Docker objects.
The existing ordinary Supervisor records native store and first-launch boundaries
under its own original exclusive lock. Neither entry point converts old roots.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from .original_attempts import HistoryBinding, OriginalEvidenceUnavailable, _create, _lifetime, _sync, canonical, history_lock, identity, read_original, source_identity, validate_original_lifetime

if TYPE_CHECKING:
    from .bootstrap_supervisor import BootstrapConfig, ManagementBootstrapSupervisor

PROFILE = "ordinary-original-genesis-v1"
HISTORY_PROFILE = "ordinary-original-history-v1"
GENESIS_NAME = "original-genesis.json"
NATIVE_NAME = "original-genesis-stores.json"
LAUNCH_NAME = "original-genesis-first-launch.json"
HISTORY_MOUNT = Path("/management-original-history")
ROOTS_PROFILE = "ordinary-original-roots-v1"
ROOTS_NAME = "original-genesis-roots.json"
ROOT_CLAIM_NAME = "original-genesis-root-claim.json"


@dataclass(frozen=True)
class GenesisRootAuthorization:
    """Select prospective paths before the first Docker object exists."""

    profile: str
    purpose: str
    deployment_id: str
    authorization_id: str
    bootstrap_path: str
    management_path: str
    proxy_config_path: str
    state_root: str
    journal_root: str
    oauth_root: str
    history_root: str


@dataclass(frozen=True)
class GenesisSelection:
    """The protected separate new-deployment authorization, not a verdict."""

    profile: str
    purpose: str
    deployment_id: str
    authorization_id: str
    management_id: str
    proxy_id: str
    history_root: str
    bootstrap_path: str
    management_path: str
    proxy_config_path: str
    root_authority_path: str


@dataclass(frozen=True)
class GenesisFile:
    role: str
    path: str
    device: int
    inode: int
    sha256: str


@dataclass(frozen=True)
class GenesisRoot:
    role: str
    host_path: str
    container_path: str
    device: int
    inode: int


@dataclass(frozen=True)
class GenesisAnchor:
    profile: str
    binding: HistoryBinding
    authorization_id: str
    instance_id: str
    engine_id: str
    management_id: str
    management_image_id: str
    management_inspection_sha256: str
    proxy_id: str
    proxy_image_id: str
    proxy_inspection_sha256: str
    files: tuple[GenesisFile, ...]
    roots: tuple[GenesisRoot, ...]
    lifetime_json: str

    def file(self, role: str) -> GenesisFile:
        matches = [item for item in self.files if item.role == role]
        if len(matches) != 1:
            raise OriginalEvidenceUnavailable("original genesis file role is incomplete")
        return matches[0]

    def root(self, role: str) -> GenesisRoot:
        matches = [item for item in self.roots if item.role == role]
        if len(matches) != 1:
            raise OriginalEvidenceUnavailable("original genesis root role is incomplete")
        return matches[0]


def _file(role: str, path: Path) -> GenesisFile:
    encoded, (device, inode) = read_original(path)
    return GenesisFile(role, str(path), device, inode, hashlib.sha256(encoded).hexdigest())


def _require_host_root() -> None:
    if os.geteuid() != 0:
        raise OriginalEvidenceUnavailable("ordinary original genesis producer requires host root")


def prepare_original_genesis_roots(authorization_path: Path) -> Path:
    """Create the authorized original roots once, without any Docker effects.

    Preexisting paths are never enrolled. A partial claim remains permanently
    unavailable, including when the directories it created still look empty.
    """
    _require_host_root()
    encoded, _ = read_original(authorization_path)
    value = json.loads(encoded)
    if not isinstance(value, dict) or set(value) != set(GenesisRootAuthorization.__dataclass_fields__) or canonical(value) != encoded or any(not isinstance(item, str) for item in value.values()):
        raise OriginalEvidenceUnavailable("original root authorization fields differ")
    request = GenesisRootAuthorization(**value)
    if request.profile != ROOTS_PROFILE or request.purpose != "separately-authorized-new-deployment" or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request.deployment_id) or str(uuid.UUID(request.authorization_id)) != request.authorization_id:
        raise OriginalEvidenceUnavailable("explicit new-root authorization required")
    bootstrap = Path(request.bootstrap_path).parent
    identity(bootstrap, directory=True)
    configurations = tuple(_file(role, Path(selected)) for role, selected in (("bootstrap", request.bootstrap_path), ("management", request.management_path), ("proxy", request.proxy_config_path)))
    paths = tuple((role, Path(getattr(request, role + "_root")), container) for role, container in (("state", "/management-child-state"), ("journal", "/management-operations"), ("oauth", "/management-oauth"), ("history", str(HISTORY_MOUNT))))
    if any(a == b or a in b.parents or b in a.parents for index, a in enumerate([bootstrap, *(path for _, path, _ in paths)]) for b in [bootstrap, *(path for _, path, _ in paths)][index + 1 :]):
        raise OriginalEvidenceUnavailable("authorized original root paths overlap")
    for _, path, _ in paths:
        if not path.is_absolute() or path != path.resolve() or path.exists() or path.is_symlink():
            raise OriginalEvidenceUnavailable("original root already exists; no empty-root conversion")
        identity(path.parent, directory=True)
    original_authorization = _file("root-authorization", authorization_path)
    claim = {"profile": ROOTS_PROFILE, "authorization": asdict(original_authorization), "source": source_identity(), "request": value, "lifetime": _lifetime(), "configurations": [asdict(item) for item in configurations]}
    _create(bootstrap, ROOT_CLAIM_NAME, claim)
    roots = []
    for role, path, container in paths:
        path.mkdir(mode=0o700)
        _sync(path)
        _sync(path.parent)
        roots.append(GenesisRoot(role, str(path), container, *identity(path, directory=True)))
    if tuple(_file(item.role, Path(item.path)) for item in configurations) != configurations or _file("root-authorization", authorization_path) != original_authorization:
        raise OriginalEvidenceUnavailable("original root authorization changed during creation")
    _create(bootstrap, ROOTS_NAME, {"profile": ROOTS_PROFILE, "claim": asdict(_file("root-claim", bootstrap / ROOT_CLAIM_NAME)), "roots": [asdict(item) for item in roots]})
    _read_root_authority(bootstrap / ROOTS_NAME)
    logger.bind(original_boundary="genesis-roots", evidence_only=True).info("Original separately authorized deployment roots created: deployment={}", request.deployment_id)
    return bootstrap / ROOTS_NAME


def _read_root_authority(path: Path) -> tuple[GenesisRootAuthorization, tuple[GenesisRoot, ...], tuple[GenesisFile, ...]]:
    encoded, _ = read_original(path)
    value = json.loads(encoded)
    if not isinstance(value, dict) or set(value) != {"profile", "claim", "roots"} or value["profile"] != ROOTS_PROFILE or canonical(value) != encoded:
        raise OriginalEvidenceUnavailable("original root creation receipt incomplete")
    claim_file = GenesisFile(**value["claim"])
    if claim_file.role != "root-claim" or Path(claim_file.path) != path.parent / ROOT_CLAIM_NAME or _file("root-claim", Path(claim_file.path)) != claim_file:
        raise OriginalEvidenceUnavailable("original root creation claim differs")
    claim_encoded, _ = read_original(Path(claim_file.path))
    claim = json.loads(claim_encoded)
    if not isinstance(claim, dict) or set(claim) != {"profile", "authorization", "source", "request", "lifetime", "configurations"} or claim["profile"] != ROOTS_PROFILE or canonical(claim) != claim_encoded or claim["source"] != source_identity():
        raise OriginalEvidenceUnavailable("original root producer source or fields differ")
    validate_original_lifetime(claim["lifetime"])
    authorization = GenesisFile(**claim["authorization"])
    if authorization.role != "root-authorization" or _file("root-authorization", Path(authorization.path)) != authorization:
        raise OriginalEvidenceUnavailable("original root authorization changed")
    request = GenesisRootAuthorization(**claim["request"])
    if read_original(Path(authorization.path))[0] != canonical(asdict(request)) or path != Path(request.bootstrap_path).parent / ROOTS_NAME:
        raise OriginalEvidenceUnavailable("original root authorization scope differs")
    roots = tuple(GenesisRoot(**item) for item in value["roots"])
    if len(roots) != 4 or {item.role for item in roots} != {"state", "journal", "oauth", "history"} or len({(item.device, item.inode) for item in roots}) != 4:
        raise OriginalEvidenceUnavailable("original root creation set is incomplete or aliased")
    destinations = {"state": "/management-child-state", "journal": "/management-operations", "oauth": "/management-oauth", "history": str(HISTORY_MOUNT)}
    for root in roots:
        if root.host_path != getattr(request, root.role + "_root") or root.container_path != destinations[root.role] or identity(Path(root.host_path), directory=True) != (root.device, root.inode):
            raise OriginalEvidenceUnavailable("original created root changed")
    configurations = tuple(GenesisFile(**item) for item in claim["configurations"])
    if len(configurations) != 3 or {item.role for item in configurations} != {"bootstrap", "management", "proxy"}:
        raise OriginalEvidenceUnavailable("original root configuration set is incomplete")
    for item in configurations:
        selected = request.proxy_config_path if item.role == "proxy" else getattr(request, item.role + "_path")
        if item.path != selected or _file(item.role, Path(item.path)) != item:
            raise OriginalEvidenceUnavailable("original root configuration changed")
    return request, roots, (authorization, claim_file, _file("roots", path))


def read_selection(path: Path) -> GenesisSelection:
    encoded, _ = read_original(path)
    value = json.loads(encoded)
    if not isinstance(value, dict) or set(value) != set(GenesisSelection.__dataclass_fields__) or canonical(value) != encoded or any(not isinstance(item, str) for item in value.values()):
        raise OriginalEvidenceUnavailable("genesis authorization fields or encoding differ")
    selection = GenesisSelection(**value)
    if selection.profile != PROFILE or selection.purpose != "separately-authorized-new-deployment" or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", selection.deployment_id) or str(uuid.UUID(selection.authorization_id)) != selection.authorization_id:
        raise OriginalEvidenceUnavailable("explicit prospective new-deployment authorization required")
    if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in (selection.management_id, selection.proxy_id)) or selection.management_id == selection.proxy_id:
        raise OriginalEvidenceUnavailable("distinct exact original Docker IDs required")
    for selected in (selection.history_root, selection.bootstrap_path, selection.management_path, selection.proxy_config_path, selection.root_authority_path):
        candidate = Path(selected)
        if not candidate.is_absolute() or candidate != candidate.resolve(strict=True):
            raise OriginalEvidenceUnavailable("original genesis selection is not canonical")
    return selection


def read_genesis(path: Path) -> GenesisAnchor:
    encoded, _ = read_original(path)
    raw = json.loads(encoded)
    if not isinstance(raw, dict) or set(raw) != set(GenesisAnchor.__dataclass_fields__) or canonical(raw) != encoded or raw.get("profile") != PROFILE:
        raise OriginalEvidenceUnavailable("original genesis schema or encoding differs")
    if not isinstance(raw["binding"], dict) or set(raw["binding"]) != set(HistoryBinding.__dataclass_fields__) or any(not isinstance(value, str) for value in raw["binding"].values()):
        raise OriginalEvidenceUnavailable("original genesis history binding differs")
    binding = HistoryBinding(**raw["binding"])
    if str(uuid.UUID(binding.history)) != binding.history or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", binding.deployment) or not re.fullmatch(r"sha256:[0-9a-f]{64}", binding.source):
        raise OriginalEvidenceUnavailable("original genesis source or history identity differs")
    for key in ("management_id", "proxy_id", "management_inspection_sha256", "proxy_inspection_sha256"):
        if not isinstance(raw[key], str) or not re.fullmatch(r"[0-9a-f]{64}", raw[key]):
            raise OriginalEvidenceUnavailable("original genesis Docker identity differs")
    for key in ("management_image_id", "proxy_image_id"):
        if not isinstance(raw[key], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", raw[key]):
            raise OriginalEvidenceUnavailable("original genesis image identity differs")
    if raw["management_id"] == raw["proxy_id"] or any(not isinstance(raw[key], str) or not raw[key] or len(raw[key]) > 128 for key in ("instance_id", "engine_id")) or str(uuid.UUID(raw["authorization_id"])) != raw["authorization_id"]:
        raise OriginalEvidenceUnavailable("original genesis deployment identity differs")
    files = []
    if not isinstance(raw["files"], list) or len(raw["files"]) != 7:
        raise OriginalEvidenceUnavailable("original genesis original files are incomplete")
    for record in raw["files"]:
        if not isinstance(record, dict) or set(record) != set(GenesisFile.__dataclass_fields__):
            raise OriginalEvidenceUnavailable("original genesis original file fields differ")
        if any(type(record[key]) is not int or record[key] < (1 if key == "inode" else 0) for key in ("device", "inode")) or not isinstance(record["path"], str) or not Path(record["path"]).is_absolute() or not isinstance(record["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"]):
            raise OriginalEvidenceUnavailable("original genesis original file identity differs")
        files.append(GenesisFile(**record))
    roots = []
    if not isinstance(raw["roots"], list) or len(raw["roots"]) != 5:
        raise OriginalEvidenceUnavailable("original genesis root set is incomplete")
    for record in raw["roots"]:
        if not isinstance(record, dict) or set(record) != set(GenesisRoot.__dataclass_fields__):
            raise OriginalEvidenceUnavailable("original genesis root fields differ")
        if any(type(record[key]) is not int or record[key] < (1 if key == "inode" else 0) for key in ("device", "inode")) or any(not isinstance(record[key], str) or not Path(record[key]).is_absolute() for key in ("host_path", "container_path")):
            raise OriginalEvidenceUnavailable("original genesis root identity differs")
        roots.append(GenesisRoot(**record))
    if {item.role for item in files} != {"authorization", "bootstrap", "management", "proxy", "root-authorization", "root-claim", "roots"} or {item.role for item in roots} != {"bootstrap", "state", "journal", "oauth", "history"}:
        raise OriginalEvidenceUnavailable("original genesis roles are incomplete or duplicated")
    if len({(item.device, item.inode) for item in files}) != len(files) or len({(item.device, item.inode) for item in roots}) != len(roots):
        raise OriginalEvidenceUnavailable("original genesis identities alias")
    if not isinstance(raw["lifetime_json"], str):
        raise OriginalEvidenceUnavailable("original genesis producer lifetime missing")
    validate_original_lifetime(json.loads(raw["lifetime_json"]))
    return GenesisAnchor(**{**raw, "binding": binding, "files": tuple(files), "roots": tuple(roots)})


def _created_pair(selection: GenesisSelection):
    """Read the full local engine and the exact finite ordinary command profile."""
    from .containment_observation import ProposedScope, _docker
    from .exclusion_host import _mount
    from .fresh_host_observation import _supported_prior_command
    from .installed_bootstrap_mount_profile import _inspect, _mounts, _unshadowed_bootstrap_execution
    from .recovery_source_profile import read_image_source_identity

    scope = ProposedScope(selection.deployment_id, selection.authorization_id, selection.authorization_id, "ordinary-v1", selection.management_id, selection.proxy_id)
    observed = _docker(scope)
    selected = {item.container_id: item for item in observed if item.container_id in (selection.management_id, selection.proxy_id)}
    if any(item.management_route_roles for item in observed if item.container_id not in selected):
        raise OriginalEvidenceUnavailable("another original management/proxy Docker route is present")
    for item in selected.values():
        if (item.status, item.running, item.pid, item.started_at, item.finished_at, item.restart_count, item.restart_policy, item.maximum_retry_count) != ("created", False, 0, "0001-01-01T00:00:00Z", "0001-01-01T00:00:00Z", 0, "no", 0):
            raise OriginalEvidenceUnavailable("genesis requires exact created and never-started Docker objects")
    management, proxy = _inspect(selection.management_id), _inspect(selection.proxy_id)
    for object_id, inspected in ((selection.management_id, management), (selection.proxy_id, proxy)):
        if hashlib.sha256(canonical([inspected])).hexdigest() != selected[object_id].inspection_sha256:
            raise OriginalEvidenceUnavailable("Docker object changed during original capture")
        host, config = inspected.get("HostConfig"), inspected.get("Config")
        if not isinstance(host, dict) or not isinstance(config, dict) or host.get("Privileged") is not False or host.get("PidMode") not in ("", None) or host.get("CapAdd") not in (None, []) or host.get("Devices") not in (None, []) or config.get("User") != "":
            raise OriginalEvidenceUnavailable("ordinary genesis container execution profile differs")
    config = management["Config"]
    assert isinstance(config, dict)
    if not _supported_prior_command(config.get("Entrypoint"), config.get("Cmd")):
        raise OriginalEvidenceUnavailable("genesis requires the ordinary bootstrap command")
    mounts = _mounts(management)
    _unshadowed_bootstrap_execution(config, mounts)
    raw_mounts = management["Mounts"]
    assert isinstance(raw_mounts, list)
    if _mount(raw_mounts, "/bootstrap") / "config.json" != Path(selection.bootstrap_path) or _mount(raw_mounts, str(HISTORY_MOUNT)) != Path(selection.history_root):
        raise OriginalEvidenceUnavailable("original genesis bootstrap or history mount differs")
    if Path(selection.management_path) != Path(selection.bootstrap_path).parent / "management.yaml":
        raise OriginalEvidenceUnavailable("original genesis management file is not the fixed bootstrap member")
    proxy_config, proxy_host = proxy["Config"], proxy["HostConfig"]
    assert isinstance(proxy_config, dict) and isinstance(proxy_host, dict)
    if proxy_config.get("Entrypoint") != ["caddy"] or proxy_config.get("Cmd") != ["run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"] or proxy_host.get("NetworkMode") != "container:" + selection.management_id:
        raise OriginalEvidenceUnavailable("original genesis proxy is not the exact ordinary paired command")
    proxy_mounts = proxy["Mounts"]
    assert isinstance(proxy_mounts, list)
    if _mount(proxy_mounts, "/etc/caddy/Caddyfile") != Path(selection.proxy_config_path):
        raise OriginalEvidenceUnavailable("original genesis proxy configuration mount differs")
    actual_source = read_image_source_identity(selection.management_id)
    if actual_source != source_identity():
        raise OriginalEvidenceUnavailable("created original bootstrap source differs from executing producer")
    return observed, management, actual_source


def _validate_management_settings(config: BootstrapConfig, management_path: Path, bootstrap_path: Path) -> None:
    """Bind the service's effective stores to the native original receipts."""
    from .oauth_config import load_management_settings, management_listener_binding_matches

    settings = load_management_settings(management_path)
    if settings.bootstrap_config_path != bootstrap_path or settings.state_root != config.oauth_root or settings.operation_state_root != config.journal_root:
        raise OriginalEvidenceUnavailable("ordinary genesis effective management roots differ")
    if not management_listener_binding_matches(config, settings):
        raise OriginalEvidenceUnavailable("ordinary genesis effective management listener differs")


def capture_original_genesis(authorization_path: Path) -> GenesisAnchor:
    """Capture only; this operation never starts, configures or stops Docker."""
    from .bootstrap_supervisor import BootstrapConfig
    from .exclusion_host import _mount

    _require_host_root()
    selection = read_selection(authorization_path)
    request, created_roots, root_files = _read_root_authority(Path(selection.root_authority_path))
    if any(getattr(request, name) != getattr(selection, name) for name in ("purpose", "deployment_id", "authorization_id", "history_root", "bootstrap_path", "management_path", "proxy_config_path")):
        raise OriginalEvidenceUnavailable("original root authorization differs from selected deployment")
    config = BootstrapConfig.read(Path(selection.bootstrap_path))
    if config.original_genesis != Path("/bootstrap") / GENESIS_NAME or config.fresh_guard_path is not None or config.new_instance_initialization or config.archived_exclusions:
        raise OriginalEvidenceUnavailable("ordinary bootstrap did not explicitly select original genesis")
    if (str(config.state_root), str(config.journal_root), str(config.oauth_root)) != ("/management-child-state", "/management-operations", "/management-oauth"):
        raise OriginalEvidenceUnavailable("ordinary genesis retained root mapping differs")
    runtime = next(item for item in config.runtimes if item.reference == config.initial_runtime)
    if runtime.executable != Path("/opt/auto-coder/.venv/bin/auto-coder") or runtime.arguments != ("management", "serve", "--config", "/bootstrap/management.yaml") or runtime.execution_kind != "source-script" or config.management_config not in (None, Path("/bootstrap/management.yaml")):
        raise OriginalEvidenceUnavailable("ordinary genesis management runtime differs")
    _validate_management_settings(config, Path(selection.management_path), Path("/bootstrap/config.json"))
    files = (*tuple(_file(role, path) for role, path in (("authorization", authorization_path), ("bootstrap", Path(selection.bootstrap_path)), ("management", Path(selection.management_path)), ("proxy", Path(selection.proxy_config_path)))), *root_files)
    observed, management, source = _created_pair(selection)
    mounts = management["Mounts"]
    assert isinstance(mounts, list)
    roots = tuple(
        GenesisRoot(role, str(host), str(container), *identity(host, directory=True))
        for role, host, container in (
            ("bootstrap", Path(selection.bootstrap_path).parent, Path("/bootstrap")),
            ("state", _mount(mounts, str(config.state_root), rw=True), config.state_root),
            ("journal", _mount(mounts, str(config.journal_root), rw=True), config.journal_root),
            ("oauth", _mount(mounts, str(config.oauth_root), rw=True), config.oauth_root),
            ("history", Path(selection.history_root), HISTORY_MOUNT),
        )
    )
    host_roots = [Path(item.host_path) for item in roots]
    if tuple(item for item in roots if item.role != "bootstrap") != created_roots:
        raise OriginalEvidenceUnavailable("Docker mounts differ from affirmative original root creation")
    if any(a == b or a in b.parents or b in a.parents for index, a in enumerate(host_roots) for b in host_roots[index + 1 :]):
        raise OriginalEvidenceUnavailable("original genesis roots overlap")
    for root in roots:
        if root.role != "bootstrap" and any(Path(root.host_path).iterdir()):
            raise OriginalEvidenceUnavailable("new separately authorized genesis roots must be empty")
    genesis_path = Path(selection.bootstrap_path).parent / GENESIS_NAME
    if genesis_path.exists() or genesis_path.is_symlink():
        raise OriginalEvidenceUnavailable("original genesis already captured or interrupted; no reenrollment")
    history_root = Path(selection.history_root)
    # The first durable claim serializes capture and permanently poisons a partial
    # publication. Existing roots never acquire a replacement original authority.
    _create(history_root, "history.lock", {})
    with history_lock(history_root, exclusive=True):
        binding = HistoryBinding(str(history_root), selection.deployment_id, str(uuid.uuid4()), source)
        selected = {item.container_id: item for item in observed}
        manager, proxy = selected[selection.management_id], selected[selection.proxy_id]
        anchor = GenesisAnchor(PROFILE, binding, selection.authorization_id, config.instance_id, manager.engine_id, manager.container_id, manager.image_id, manager.inspection_sha256, proxy.container_id, proxy.image_id, proxy.inspection_sha256, files, roots, canonical(_lifetime()).decode())
        if _created_pair(selection) != (observed, management, source) or tuple(_file(item.role, Path(item.path)) for item in files) != files:
            raise OriginalEvidenceUnavailable("original genesis inputs changed during capture")
        for root in roots:
            if identity(Path(root.host_path), directory=True) != (root.device, root.inode):
                raise OriginalEvidenceUnavailable("original genesis root changed during capture")
            allowed = {"history.lock"} if root.role == "history" else set()
            if root.role != "bootstrap" and {item.name for item in Path(root.host_path).iterdir()} != allowed:
                raise OriginalEvidenceUnavailable("original genesis root acquired earlier records")
        _create(genesis_path.parent, genesis_path.name, asdict(anchor))
        original = _file("genesis", genesis_path)
        _create(history_root, "authority.json", {"binding": asdict(binding), "profile": HISTORY_PROFILE, "root": identity(history_root, directory=True), "lock": identity(history_root / "history.lock"), "genesis": asdict(original)})
        _create(history_root, "frontier.json", {"count": 0, "digest": ""})
        validate_history_genesis(history_root)
        logger.bind(original_history=str(history_root), original_boundary="genesis", evidence_only=True).info("Original ordinary deployment genesis captured: deployment={} history={}", binding.deployment, binding.history)
        return anchor


def validate_history_genesis(root: Path, *, mounted_genesis: Path | None = None) -> GenesisAnchor:
    """Read the original authority and its exact genesis, with no store creation."""
    encoded, _ = read_original(root / "authority.json")
    value = json.loads(encoded)
    if not isinstance(value, dict) or set(value) != {"binding", "profile", "root", "lock", "genesis"} or value.get("profile") != HISTORY_PROFILE or canonical(value) != encoded:
        raise OriginalEvidenceUnavailable("ordinary original history authority is incomplete")
    original = value["genesis"]
    if not isinstance(original, dict) or set(original) != set(GenesisFile.__dataclass_fields__) or original.get("role") != "genesis":
        raise OriginalEvidenceUnavailable("ordinary original genesis pointer is incomplete")
    path = mounted_genesis or Path(original["path"])
    actual = _file("genesis", path)
    if (actual.device, actual.inode, actual.sha256) != (original["device"], original["inode"], original["sha256"]) or mounted_genesis is None and actual.path != original["path"]:
        raise OriginalEvidenceUnavailable("original genesis inode or bytes changed")
    anchor = read_genesis(path)
    if value["binding"] != asdict(anchor.binding) or value["root"] != list(identity(root, directory=True)) or value["lock"] != list(identity(root / "history.lock")):
        raise OriginalEvidenceUnavailable("original genesis/history identities differ")
    history = anchor.root("history")
    if (history.device, history.inode) != identity(root, directory=True) or history.host_path != anchor.binding.root:
        raise OriginalEvidenceUnavailable("original genesis history root differs")
    if mounted_genesis is None and (str(root) != anchor.binding.root or str(path) != str(Path(anchor.file("bootstrap").path).parent / GENESIS_NAME)):
        raise OriginalEvidenceUnavailable("original genesis authority path differs")
    if mounted_genesis is None:
        _, created_roots, _ = _read_root_authority(Path(anchor.file("roots").path))
        if tuple(item for item in anchor.roots if item.role != "bootstrap") != created_roots:
            raise OriginalEvidenceUnavailable("original genesis roots differ from creation authority")
        for original_file in anchor.files:
            if _file(original_file.role, Path(original_file.path)) != original_file:
                raise OriginalEvidenceUnavailable("original genesis configuration or authorization changed")
        for original_root in anchor.roots:
            if identity(Path(original_root.host_path), directory=True) != (original_root.device, original_root.inode):
                raise OriginalEvidenceUnavailable("original genesis protected root changed")
    return anchor


def native_paths(anchor: GenesisAnchor, *, host: bool) -> dict[str, Path]:
    state, journal = anchor.root("state"), anchor.root("journal")
    state_path = Path(state.host_path if host else state.container_path)
    journal_path = Path(journal.host_path if host else journal.container_path)
    return {"child": state_path / "management-child.sqlite3", "handoff": state_path / "management-self-update.sqlite3", "recovery": state_path / "management-recovery.sqlite3", "operations": journal_path / "management-operations.sqlite3"}


def validate_native_genesis(anchor: GenesisAnchor, genesis_path: Path, *, host: bool) -> tuple[dict[str, object], dict[str, object]]:
    """Check original native identities; an unknown outcome is not missing history."""
    state = Path(anchor.root("state").host_path if host else anchor.root("state").container_path)
    encoded, _ = read_original(state / NATIVE_NAME)
    native = json.loads(encoded)
    expected_keys = {"profile", "history", "source", "genesis_sha256", "instance_id", "lifetime", "owner_fence", "stores"}
    if not isinstance(native, dict) or set(native) != expected_keys or canonical(native) != encoded:
        raise OriginalEvidenceUnavailable("native genesis original receipt is incomplete")
    validate_original_lifetime(native["lifetime"])
    if (native["profile"], native["history"], native["source"], native["genesis_sha256"], native["instance_id"]) != (PROFILE, anchor.binding.history, anchor.binding.source, _file("genesis", genesis_path).sha256, anchor.instance_id):
        raise OriginalEvidenceUnavailable("native genesis source/history identity differs")
    if native["owner_fence"] != list(identity(state / ".supervisor-owner.lock")) or native["stores"] != {name: list(identity(path)) for name, path in native_paths(anchor, host=host).items()}:
        raise OriginalEvidenceUnavailable("native genesis original owner/store inode differs")
    launch_encoded, _ = read_original(state / LAUNCH_NAME)
    launch = json.loads(launch_encoded)
    if not isinstance(launch, dict) or set(launch) != {"profile", "history", "genesis_sha256", "native_sha256", "operation_id", "attempt_id", "lifetime"} or canonical(launch) != launch_encoded:
        raise OriginalEvidenceUnavailable("original genesis first-launch boundary is incomplete")
    if launch["profile"] != PROFILE or launch["history"] != anchor.binding.history or launch["genesis_sha256"] != native["genesis_sha256"] or launch["native_sha256"] != hashlib.sha256(encoded).hexdigest() or launch["lifetime"] != native["lifetime"]:
        raise OriginalEvidenceUnavailable("original genesis first-launch boundary differs")
    if not isinstance(launch["operation_id"], str) or not launch["operation_id"].startswith("bootstrap-") or str(uuid.UUID(launch["operation_id"][10:])) != launch["operation_id"][10:] or str(uuid.UUID(launch["attempt_id"])) != launch["attempt_id"]:
        raise OriginalEvidenceUnavailable("original genesis first-launch identity differs")
    return native, launch


def _validate_bootstrap_inputs(config: BootstrapConfig, bootstrap_path: Path) -> GenesisAnchor:
    """Validate only original inputs while the caller holds history SH."""
    if config.original_genesis is None:
        raise OriginalEvidenceUnavailable("ordinary bootstrap genesis selection missing")
    anchor = validate_history_genesis(HISTORY_MOUNT, mounted_genesis=config.original_genesis)
    if anchor.binding.source != source_identity() or anchor.instance_id != config.instance_id:
        raise OriginalEvidenceUnavailable("ordinary bootstrap source or instance differs from genesis")
    for root in anchor.roots:
        if identity(Path(root.container_path), directory=True) != (root.device, root.inode):
            raise OriginalEvidenceUnavailable("ordinary bootstrap original mounted root differs")
    for role, path in (("bootstrap", bootstrap_path), ("management", Path("/bootstrap/management.yaml"))):
        original = anchor.file(role)
        actual = _file(role, path)
        if (actual.device, actual.inode, actual.sha256) != (original.device, original.inode, original.sha256):
            raise OriginalEvidenceUnavailable("ordinary bootstrap original configuration differs")
    if (config.state_root, config.journal_root, config.oauth_root) != tuple(Path(anchor.root(role).container_path) for role in ("state", "journal", "oauth")):
        raise OriginalEvidenceUnavailable("ordinary bootstrap retained paths differ from genesis")
    _validate_management_settings(config, Path(anchor.root("bootstrap").container_path) / "management.yaml", bootstrap_path)
    from .bootstrap_supervisor import _verify_fresh_console_entrypoint

    runtime = next(item for item in config.runtimes if item.reference == config.initial_runtime)
    if not runtime.verify(initial=True):
        raise OriginalEvidenceUnavailable("ordinary bootstrap original console receipt differs")
    _verify_fresh_console_entrypoint(runtime)
    return anchor


def validate_bootstrap_genesis(config: BootstrapConfig, bootstrap_path: Path) -> GenesisAnchor:
    """Refuse missing/replaced originals before even a diagnostic directory."""
    with history_lock(HISTORY_MOUNT, exclusive=False):
        return _validate_bootstrap_inputs(config, bootstrap_path)


def prepare_original_genesis(supervisor: ManagementBootstrapSupervisor, owner_start_token: str) -> tuple[str, str] | None:
    """Called only by the actual ordinary bootstrap under its own exclusive lock."""
    from .fresh_host_observation import _assert_pid_flock
    from .operation_store import ManagementOperationStore
    from .recovery_mailbox import RecoveryMailbox
    from .self_update_handoff import HandoffStore

    config = supervisor.config
    if config.original_genesis is None or supervisor.bootstrap_config_path is None:
        raise OriginalEvidenceUnavailable("ordinary bootstrap genesis selection missing")
    descriptor = os.open(config.state_root / ".supervisor-owner.lock", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        _assert_pid_flock(os.getpid(), descriptor, "WRITE")
    finally:
        os.close(descriptor)
    with history_lock(HISTORY_MOUNT, exclusive=False):
        anchor = _validate_bootstrap_inputs(config, supervisor.bootstrap_config_path)
        native_path, launch_path = config.state_root / NATIVE_NAME, config.state_root / LAUNCH_NAME
        if native_path.exists() or native_path.is_symlink() or launch_path.exists() or launch_path.is_symlink():
            validate_native_genesis(anchor, config.original_genesis, host=False)
            return None
        frontier, _ = read_original(HISTORY_MOUNT / "frontier.json")
        if frontier != canonical({"count": 0, "digest": ""}) or {path.name for path in HISTORY_MOUNT.iterdir()} != {"history.lock", "authority.json", "frontier.json"}:
            raise OriginalEvidenceUnavailable("ordinary genesis first launch has an advanced or partial history")
        allowed = {".supervisor-owner.lock", "logs"}
        if any(path.name not in allowed or path.is_symlink() for path in config.state_root.iterdir()) or any(config.journal_root.iterdir()) or any(config.oauth_root.iterdir()):
            raise OriginalEvidenceUnavailable("ordinary genesis cannot initialize earlier or interrupted roots")
        logs = config.state_root / "logs"
        if logs.exists() and (not logs.is_dir() or logs.lstat().st_uid != os.geteuid()):
            raise OriginalEvidenceUnavailable("ordinary genesis diagnostics directory differs")
        previous_umask = os.umask(0o077)
        try:
            supervisor.store.publish_supervisor(supervisor.incarnation, os.getpid(), owner_start_token)
            supervisor.store.require_existing = True
            with closing(ManagementOperationStore(config.journal_root)._connect()):
                pass
            with closing(HandoffStore(config.state_root, config.instance_id)._connect()):
                pass
            RecoveryMailbox(config.state_root, config.instance_id).initialize()
            for path in native_paths(anchor, host=False).values():
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            _sync(config.state_root)
            _sync(config.journal_root)
            lifetime = _lifetime()
            native = {
                "profile": PROFILE,
                "history": anchor.binding.history,
                "source": anchor.binding.source,
                "genesis_sha256": _file("genesis", config.original_genesis).sha256,
                "instance_id": anchor.instance_id,
                "lifetime": lifetime,
                "owner_fence": identity(config.state_root / ".supervisor-owner.lock"),
                "stores": {name: identity(path) for name, path in native_paths(anchor, host=False).items()},
            }
            _create(config.state_root, NATIVE_NAME, native)
            logger.bind(original_history=anchor.binding.root, original_boundary="genesis-stores", evidence_only=True).info("Original ordinary bootstrap stores captured: history={}", anchor.binding.history)
            operation, attempt = "bootstrap-" + str(uuid.uuid4()), str(uuid.uuid4())
            _create(config.state_root, LAUNCH_NAME, {"profile": PROFILE, "history": anchor.binding.history, "genesis_sha256": native["genesis_sha256"], "native_sha256": hashlib.sha256(canonical(native)).hexdigest(), "operation_id": operation, "attempt_id": attempt, "lifetime": lifetime})
            validate_native_genesis(anchor, config.original_genesis, host=False)
            logger.bind(original_history=anchor.binding.root, original_boundary="genesis-first-launch", evidence_only=True).info("Original ordinary bootstrap first-launch boundary recorded: history={}", anchor.binding.history)
            return operation, attempt
        finally:
            os.umask(previous_umask)


def main() -> int:
    from auto_coder.logger_config import setup_logger

    parser = argparse.ArgumentParser(description="Capture separately authorized ordinary genesis originals; no Docker effects or recovery authority")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--authorization", type=Path)
    mode.add_argument("--prepare-roots", type=Path)
    parser.add_argument("--log-file", required=True, type=Path)
    args = parser.parse_args()
    setup_logger(stream=sys.stdout, log_file=str(args.log_file))
    try:
        if args.prepare_roots is not None:
            print(json.dumps({"original_root_authority": str(prepare_original_genesis_roots(args.prepare_roots))}, sort_keys=True))
        else:
            print(json.dumps(asdict(capture_original_genesis(args.authorization)), sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        parser.exit(1, "original ordinary genesis unavailable: " + str(error) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
