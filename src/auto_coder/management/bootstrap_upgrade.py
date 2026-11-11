"""Operator-only, durable orchestration of an immutable management cutover.

This module is deliberately a host CLI, not a management MCP action. All
container effects have fixed verbs and exact, previously observed IDs. A lost
effect response cannot authorize a second dispatch.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import io
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

import yaml
from loguru import logger

from auto_coder.operating_policy import MANAGEMENT_REPOSITORY
from auto_coder.tested_source_refs import TestedSourceError, TestedSourcePublisher, verify_source_shard_attempt
from auto_coder.util.gh_cache import GitHubGitDataClient

from .bootstrap_supervisor import BootstrapConfig
from .exclusion_cutover import hold_claim_and_guard
from .exclusion_host import check_host_archives, required_exclusions, validate_successor
from .fresh_host import _evidence, _protected_file
from .fresh_host_observation import (
    HostObservationUnavailable,
    _assert_pid_flock,
    _inspect,
    assert_retained_journal_source,
    assert_retained_oauth_mount,
    capture_prior_container,
)
from .fresh_initialization import FreshInitializationIntent, parse_fresh_intent, read_fresh_status
from .operation_store import ACTIVE_PHASES, TERMINAL_PHASES
from .proxy_reconnect import DockerProxyObservation
from .retained_journal import RetainedJournal
from .source_evidence import SourceEvidenceError
from .source_evidence_config import REPOSITORY
from .source_evidence_config import _protected as protected_credential

OPERATOR_ROOT = Path("/var/lib/auto-coder/management-upgrades")
SOURCE_PROOF_ROOT = Path("/var/lib/auto-coder/management-upgrade-source-evidence")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_STAGES = frozenset(
    {
        "prepared",
        "prerequisite-blocked",
        "stopping",
        "successor-launch-uncertain",
        "application-ready",
        "beta-deploy-prerequisites-verified",
        "needs-attention",
    }
)
_EFFECTS = ("stop-proxy", "stop-management", "start-management", "start-proxy", "start-watcher")
# Verify the current Supervisor producer topology, not historical fixed matrices.
# The original aggregate owns the immutable plans and native case receipts;
# these required successful native steps independently bind its execution.
_SUPERVISOR_FIXED_STEPS = {
    "Worker processes and bridge": ("Test real worker processes and management bridge", "Bind native worker result to the producing job"),
    "Prepare Supervisor plan (management)": ("Plan complete management collection",),
    "Prepare Supervisor plan (fresh host)": ("Plan complete privileged collection",),
    "Prepare Supervisor plan (containers)": ("Plan complete container collection",),
    "Operator upgrade production-origin Docker": ("Verify operator prepare/apply/status with disposable Docker", "Bind native operator upgrade receipt"),
    "Supervisor Integration Tests": ("Require every integration group to succeed",),
}
_SUPERVISOR_MATRIX_STEPS = {
    "management-self-update": ("Verify authenticated management self-update", "Bind native management receipt"),
    "fresh-host": ("Verify fresh initialization with host evidence", "Bind native fresh-host receipt"),
    "isolated-containers": ("Verify isolated Supervisor containers", "Bind native container receipt"),
}
_SUPERVISOR_MATRIX_NAME = re.compile(r"Supervisor (management-self-update|fresh-host|isolated-containers) \(shard ([1-9][0-9]*)\)\Z")


class UpgradeUnavailable(RuntimeError):
    """A typed refusal; the caller must not substitute a different effect."""

    def __init__(self, stage: str, reason: str) -> None:
        super().__init__(reason)
        self.stage = stage
        self.reason = reason


@dataclass(frozen=True)
class UpgradeProfile:
    evidence_file: str
    intent_file: str
    bootstrap_config: str
    management_config: str
    prior_lock: str
    guard_dir: str
    mounts_file: str
    successor_mounts_file: str
    successor_proxy_id: str
    successor_proxy_image: str
    successor_proxy_config: str
    old_proxy_config: str
    old_watcher_config: str
    new_watcher_config: str
    source_sha: str

    def validate(self) -> None:
        for name in (
            "evidence_file",
            "intent_file",
            "bootstrap_config",
            "management_config",
            "prior_lock",
            "guard_dir",
            "mounts_file",
            "successor_mounts_file",
            "successor_proxy_config",
            "old_proxy_config",
            "old_watcher_config",
            "new_watcher_config",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) > 4096 or "\x00" in value or not Path(value).is_absolute() or Path(value) != Path(value).resolve(strict=True):
                raise UpgradeUnavailable("prerequisite-blocked", f"noncanonical {name}")
        if not _SHA.fullmatch(self.successor_proxy_id) or not _IMAGE.fullmatch(self.successor_proxy_image) or not _REVISION.fullmatch(self.source_sha):
            raise UpgradeUnavailable("prerequisite-blocked", "exact successor/source identities required")


@dataclass(frozen=True)
class UpgradePlan:
    version: int
    profile: UpgradeProfile
    predecessor_management: str
    predecessor_proxy: str
    predecessor_management_image: str
    predecessor_proxy_image: str
    predecessor_started_at: str
    predecessor_proxy_started_at: str
    predecessor_evidence_sha256: str
    input_sha256: dict[str, str]
    image_source_identity: str
    tested_source_identity: str
    supervisor_run_id: int
    supervisor_run_attempt: int
    worker_baseline: dict[str, dict[str, str]]
    worker_registry_sha256: str
    oauth_authority_sha256: str
    oauth_grant_sha256: dict[str, str]
    ancestor_identities: tuple[tuple[str, str, int, int, str, int, int], ...]
    endpoint_port: int
    successor_management: str
    successor_image: str
    successor_instance: str
    request_id: str


@dataclass(frozen=True)
class UpgradeOperation:
    plan_sha256: str
    operation_id: str
    stage: str
    effect: str
    effect_target: str
    completed: tuple[str, ...]
    reason: str = ""
    watcher_pid: int = 0
    watcher_start_token: str = ""
    effect_receipts: dict[str, str] = field(default_factory=dict)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _protected_root(root: Path) -> None:
    if not root.is_absolute() or root.is_symlink() or root != root.resolve(strict=True):
        raise UpgradeUnavailable("prerequisite-blocked", "operator root is not canonical")
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise UpgradeUnavailable("prerequisite-blocked", "operator root is not owner-only")


def _read_record(path: Path) -> bytes:
    return _protected_file(path)


def _create_record(root: Path, name: str, data: bytes) -> None:
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        file = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
        with os.fdopen(file, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_record(root: Path, name: str, data: bytes) -> None:
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".upgrade-" + os.urandom(12).hex()
    try:
        file = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
        with os.fdopen(file, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _exclusive(root: Path) -> Iterator[None]:
    _protected_root(root)
    descriptor = os.open(root / "upgrade.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise UpgradeUnavailable("prerequisite-blocked", "upgrade controller lock is not private")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise UpgradeUnavailable("needs-attention", "this upgrade already has a live operator") from error
        yield
    finally:
        os.close(descriptor)


def _read_profile(path: Path) -> UpgradeProfile:
    raw = json.loads(_read_record(path))
    if not isinstance(raw, dict) or set(raw) != set(UpgradeProfile.__dataclass_fields__):
        raise UpgradeUnavailable("prerequisite-blocked", "unsupported upgrade profile")
    profile = UpgradeProfile(**raw)
    profile.validate()
    return profile


def _input_paths(profile: UpgradeProfile) -> dict[str, Path]:
    paths = {name: Path(getattr(profile, name)) for name in ("evidence_file", "intent_file", "bootstrap_config", "management_config", "mounts_file", "successor_mounts_file", "successor_proxy_config", "old_proxy_config", "old_watcher_config", "new_watcher_config")}
    paths["source_evidence_extension"] = Path(profile.management_config + ".source-evidence.json")
    paths["source_evidence_credential"] = Path(profile.management_config).parent / "source_evidence_read"
    return paths


def _input_hashes(profile: UpgradeProfile) -> dict[str, str]:
    return {name: hashlib.sha256(_read_record(path)).hexdigest() for name, path in _input_paths(profile).items()}


def _fresh_intent(profile: UpgradeProfile) -> FreshInitializationIntent:
    raw = json.loads(_read_record(Path(profile.intent_file)))
    try:
        return parse_fresh_intent(raw)
    except ValueError as error:
        if str(error) in {"fresh intent fields differ", "partial fresh intent extension"}:
            raise UpgradeUnavailable("prerequisite-blocked", "fresh initialization intent differs") from error
        raise


def _mount_profile(profile: UpgradeProfile) -> dict[str, tuple[tuple[str, str, bool], ...]]:
    raw = json.loads(_read_record(Path(profile.mounts_file)))
    if not isinstance(raw, dict) or set(raw) != {"management", "proxy"}:
        raise ValueError("old mount profile is incomplete")
    result: dict[str, tuple[tuple[str, str, bool], ...]] = {}
    for name, entries in raw.items():
        if not isinstance(entries, list) or any(not isinstance(item, list) or len(item) != 3 or not isinstance(item[0], str) or not isinstance(item[1], str) or type(item[2]) is not bool for item in entries):
            raise ValueError("old mount profile is malformed")
        result[name] = tuple((item[0], item[1], item[2]) for item in entries)
    return result


def _docker(*arguments: str, timeout: float = 30) -> str:
    result = subprocess.run(["docker", *arguments], check=True, text=True, capture_output=True, timeout=timeout)
    return result.stdout.strip()


def _candidate_image_source(container_id: str, image_id: str) -> str:
    """Bind immutable image identity to both built-in provenance observations."""
    if not _SHA.fullmatch(container_id) or not _IMAGE.fullmatch(image_id):
        raise ValueError("exact image container identity required")
    metadata = json.loads(_docker("image", "inspect", "--format", "{{json .}}", image_id))
    if not isinstance(metadata, dict) or metadata.get("Id") != image_id:
        raise ValueError("candidate image digest differs")
    labels = metadata.get("Config", {}).get("Labels")
    revision = labels.get("org.opencontainers.image.revision") if isinstance(labels, dict) else None
    archive = subprocess.run(
        ["docker", "cp", container_id + ":/opt/auto-coder/.venv/lib/python3.12/site-packages/auto_coder/build_provenance.json", "-"],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as extracted:
        members = extracted.getmembers()
        if len(members) != 1 or not members[0].isfile() or members[0].size > 4096:
            raise ValueError("installed source record archive differs")
        stream = extracted.extractfile(members[0])
        if stream is None:
            raise ValueError("installed source record is missing")
        record = json.load(stream)
    embedded = record.get("source_revision") if isinstance(record, dict) and record.get("schema_version") == 1 else None
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision) or embedded != revision:
        raise ValueError("image label and installed source provenance disagree")
    return revision


def _source_authority(token_file: Path) -> TestedSourcePublisher:
    descriptor = protected_credential(token_file, mode=0o600)
    with os.fdopen(descriptor, "rb") as stream:
        token = stream.read(513).decode("utf-8").strip()
    if not 20 <= len(token) <= 512 or "\n" in token:
        raise ValueError("source evidence credential is invalid")
    return TestedSourcePublisher(GitHubGitDataClient(token, MANAGEMENT_REPOSITORY), MANAGEMENT_REPOSITORY)


def _source_proof_inputs(source_sha: str, run_id: int, attempt: int) -> tuple[Path, Path]:
    """Read only operator-provisioned original source plan and native receipts."""
    if _REVISION.fullmatch(source_sha) is None or type(run_id) is not int or run_id <= 0 or type(attempt) is not int or attempt <= 0:
        raise UpgradeUnavailable("prerequisite-blocked", "invalid exact-source producing identity")
    source_root = SOURCE_PROOF_ROOT / source_sha
    attempt_root = source_root / f"{run_id}-{attempt}"
    artifacts_root = attempt_root / "artifacts"
    try:
        for directory in (SOURCE_PROOF_ROOT, source_root, attempt_root, artifacts_root):
            _protected_root(directory)
        plan_path = attempt_root / "plan.json"
        _read_record(plan_path)
        for shard_dir in artifacts_root.iterdir():
            if not re.fullmatch(rf"source-test-shard-{run_id}-{attempt}-[1-9][0-9]*", shard_dir.name):
                raise ValueError("unexpected native source artifact")
            _protected_root(shard_dir)
            _read_record(shard_dir / "receipt.json")
    except (OSError, ValueError) as error:
        raise UpgradeUnavailable("prerequisite-blocked", "protected original source plan/receipts are unavailable") from error
    return plan_path, artifacts_root


def _supervisor_native_jobs_proven(client: GitHubGitDataClient, run_id: int, attempt: int) -> bool:
    """Independently check every current producer job and mandatory native step.

    The successful Supervisor aggregate remains responsible for validating
    original plan and receipt bytes against the physical producing jobs.
    """
    total: int | None = None
    jobs: list[dict[str, object]] = []
    seen_ids: set[int] = set()
    for page in range(1, 21):
        response = client.request("GET", f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}")
        data = response.data if response.status == 200 else None
        if not isinstance(data, dict) or type(data.get("total_count")) is not int or data["total_count"] < 1:
            return False
        if total is None:
            total = data["total_count"]
        if total != data["total_count"]:
            return False
        batch = data.get("jobs")
        if not isinstance(batch, list) or not batch or len(batch) > 100:
            return False
        for raw in batch:
            if not isinstance(raw, dict) or type(raw.get("id")) is not int or raw["id"] in seen_ids:
                return False
            seen_ids.add(raw["id"])
            jobs.append(raw)
        if len(jobs) >= total:
            break
    if total is None or len(jobs) != total:
        return False

    fixed_seen: set[str] = set()
    matrix_seen: dict[str, set[int]] = {profile: set() for profile in _SUPERVISOR_MATRIX_STEPS}
    for job in jobs:
        name = job.get("name")
        if not isinstance(name, str):
            return False
        if name in _SUPERVISOR_FIXED_STEPS:
            if name in fixed_seen:
                return False
            fixed_seen.add(name)
            required_steps = _SUPERVISOR_FIXED_STEPS[name]
        else:
            match = _SUPERVISOR_MATRIX_NAME.fullmatch(name)
            if match is None:
                return False
            profile, number = match.group(1), int(match.group(2))
            if number in matrix_seen[profile]:
                return False
            matrix_seen[profile].add(number)
            required_steps = _SUPERVISOR_MATRIX_STEPS[profile]
        if any(
            job.get(key) != value
            for key, value in {
                "run_id": run_id,
                "run_attempt": attempt,
                "status": "completed",
                "conclusion": "success",
            }.items()
        ):
            return False
        steps = job.get("steps")
        if not isinstance(steps, list):
            return False
        for step_name in required_steps:
            matches = [step for step in steps if isinstance(step, dict) and step.get("name") == step_name]
            if len(matches) != 1 or matches[0].get("status") != "completed" or matches[0].get("conclusion") != "success":
                return False
    if fixed_seen != set(_SUPERVISOR_FIXED_STEPS):
        return False
    return all(nodes == set(range(1, len(nodes) + 1)) for nodes in matrix_seen.values() if nodes) and all(matrix_seen.values())


def _source_proof(publisher: TestedSourcePublisher, source_sha: str) -> tuple[str, int, int]:
    proof = publisher.read(source_sha)
    if proof is None:
        raise UpgradeUnavailable("prerequisite-blocked", "tested source ref is unavailable")
    publisher.verify_run(proof.record, recorded_attempt=True)
    plan_path, artifacts_root = _source_proof_inputs(source_sha, proof.record.run_id, proof.record.run_attempt)
    try:
        verify_source_shard_attempt(
            publisher.client,
            proof.record.run_id,
            proof.record.run_attempt,
            source_sha,
            plan_path=plan_path,
            artifacts_root=artifacts_root,
        )
    except TestedSourceError as error:
        raise UpgradeUnavailable("prerequisite-blocked", "original exact-source native qualification refused") from error
    response = publisher.client.request("GET", f"actions/workflows/supervisor-integration-tests.yml/runs?head_sha={source_sha}&per_page=100")
    runs = response.data.get("workflow_runs") if response.status == 200 and isinstance(response.data, dict) else None
    if not isinstance(runs, list):
        raise UpgradeUnavailable("prerequisite-blocked", "fresh-host integration run evidence unavailable")
    for run in runs:
        if not isinstance(run, dict) or run.get("head_sha") != source_sha or run.get("status") != "completed" or run.get("conclusion") != "success" or type(run.get("id")) is not int or type(run.get("run_attempt")) is not int:
            continue
        attempt = run["run_attempt"]
        if attempt < 1 or run.get("path") != ".github/workflows/supervisor-integration-tests.yml":
            continue
        # A successful native aggregate is necessary but never the sole proof.
        # Require its current producer topology, every shard and the real
        # operator and fresh-host pytest plus original receipt binding steps.
        if _supervisor_native_jobs_proven(publisher.client, run["id"], attempt):
            return proof.tag_object_oid, run["id"], attempt
    raise UpgradeUnavailable("prerequisite-blocked", "required fresh-host/exclusion integration cases are not proven successful")


def _watcher_binding(path: Path, manager: str, manager_image: str, proxy: str, proxy_image: str, config: Path) -> Path:
    raw = json.loads(_read_record(path))
    expected = {"state_root", "manager_id", "manager_image", "proxy_id", "proxy_image", "config_sha256"}
    if not isinstance(raw, dict) or set(raw) != expected or (raw["manager_id"], raw["manager_image"], raw["proxy_id"], raw["proxy_image"]) != (manager, manager_image, proxy, proxy_image):
        raise ValueError("watcher binding is not the exact pinned stack")
    if raw["config_sha256"] != hashlib.sha256(_read_record(config)).hexdigest():
        raise ValueError("watcher proxy configuration digest differs")
    root = Path(raw["state_root"])
    _protected_root(root)
    return root


def _candidate_stack(profile: UpgradeProfile, intent: FreshInitializationIntent) -> None:
    manager = _inspect(intent.new_container_id)
    proxy = DockerProxyObservation.inspect(profile.successor_proxy_id)
    if manager[1] != intent.image_digest or manager[2:5] not in ([False, 0, "created"], [False, 0, "exited"]) or manager[12] is not True:
        raise ValueError("successor is not the exact immutable stopped management container")
    if manager[5] is not False or manager[6] not in ("", None) or manager[7] not in (None, []) or manager[8] not in (None, []) or manager[10] not in (None, []):
        raise ValueError("successor has an unsupported container authority")
    if manager[15:17] not in ([["python"], ["-m", "auto_coder.management.bootstrap_supervisor", "--config", "/bootstrap/config.json", "--fresh-initialization"]], [["python", "-m", "auto_coder.management.bootstrap_supervisor"], ["--config", "/bootstrap/config.json", "--fresh-initialization"]]):
        raise ValueError("successor does not use one-shot fresh initialization")
    if manager[17] != {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(intent.host_listener_port)}]}:
        raise ValueError("successor host endpoint differs")
    if proxy["image"] != profile.successor_proxy_image or proxy["running"] is not False or proxy["network"] != "container:" + intent.new_container_id:
        raise ValueError("successor proxy is not stopped and attached to its manager")
    if proxy["entrypoint"] != ["caddy"] or proxy["command"] != ["run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]:
        raise ValueError("successor proxy command differs")
    if DockerProxyObservation.config_digest(profile.successor_proxy_id) != hashlib.sha256(_read_record(Path(profile.successor_proxy_config))).hexdigest():
        raise ValueError("successor proxy configuration differs")
    raw_mounts = json.loads(_read_record(Path(profile.successor_mounts_file)))
    if not isinstance(raw_mounts, dict) or set(raw_mounts) != {"management", "proxy"}:
        raise ValueError("successor mount profile is incomplete")
    expected: dict[str, tuple[tuple[str, str, bool], ...]] = {}
    for name in ("management", "proxy"):
        rows = raw_mounts[name]
        if not isinstance(rows, list) or any(not isinstance(item, list) or len(item) != 3 or not isinstance(item[0], str) or not isinstance(item[1], str) or type(item[2]) is not bool for item in rows):
            raise ValueError("successor mount profile is malformed")
        expected[name] = tuple(sorted((item[0], item[1], item[2]) for item in rows))
    required_mounts = {
        (str(Path(profile.bootstrap_config).parent), "/bootstrap", False),
        (intent.host_state_root, intent.state_root, True),
        (intent.host_journal_root, intent.journal_root, True),
        (intent.host_oauth_root, intent.oauth_root, True),
        (profile.guard_dir, "/management-initialization", False),
        (profile.prior_lock, "/management-prior-owner.lock", False),
    }
    if intent.host_prior_journal_root:
        required_mounts.add((intent.host_prior_journal_root, intent.prior_journal_root, False))
    if not required_mounts <= set(expected["management"]) or (str(Path(profile.successor_proxy_config)), "/etc/caddy/Caddyfile", False) not in expected["proxy"]:
        raise ValueError("successor omits an essential retained or immutable mount")

    def checked_mounts(observed: object) -> tuple[tuple[str, str, bool], ...]:
        if not isinstance(observed, list) or any(not isinstance(item, dict) or item.get("Type") != "bind" or not isinstance(item.get("Source"), str) or not isinstance(item.get("Destination"), str) or type(item.get("RW")) is not bool for item in observed):
            raise ValueError("successor includes an unsupported mount")
        return tuple(sorted((item["Source"], item["Destination"], item["RW"]) for item in observed))

    if checked_mounts(manager[11]) != expected["management"]:
        raise ValueError("successor management mounts differ from approved exact profile")
    proxy_mounts = json.loads(_docker("inspect", "--type", "container", "--format", "{{json .Mounts}}", profile.successor_proxy_id))
    if checked_mounts(proxy_mounts) != expected["proxy"]:
        raise ValueError("successor proxy mounts differ from approved exact profile")
    _watcher_binding(Path(profile.new_watcher_config), intent.new_container_id, intent.image_digest, profile.successor_proxy_id, profile.successor_proxy_image, Path(profile.successor_proxy_config))


def _prepared_roots(profile: UpgradeProfile, intent: FreshInitializationIntent) -> None:
    for root, device, inode, empty in (
        (Path(intent.host_state_root), intent.state_device, intent.state_inode, True),
        (Path(intent.host_journal_root), intent.journal_device, intent.journal_inode, True),
        (Path(intent.host_oauth_root), intent.oauth_device, intent.oauth_inode, False),
        (Path(profile.guard_dir), intent.guard_device, intent.guard_inode, False),
    ):
        _protected_root(root)
        info = root.stat()
        if (info.st_dev, info.st_ino) != (device, inode) or (empty and any(root.iterdir())):
            raise ValueError("fresh successor root is nonempty or differs from pinned identity")
    if (Path(profile.guard_dir) / "prior-evidence.json") != Path(profile.evidence_file):
        raise ValueError("fresh host guard does not bind captured evidence")
    if Path(profile.prior_lock).stat().st_ino != intent.owner_lock_inode or Path(profile.prior_lock).stat().st_dev != intent.owner_lock_device:
        raise ValueError("predecessor owner fence changed")


def _installed_candidate_preflight(profile: UpgradeProfile) -> None:
    """Run the production installed READY validator in a disposable RO probe."""
    intent = _fresh_intent(profile)
    raw = json.loads(_read_record(Path(profile.successor_mounts_file)))
    mounts = raw["management"]
    script = """
import json
from pathlib import Path
from auto_coder.management.bootstrap_supervisor import BootstrapConfig
from auto_coder.management.fresh_initialization import FreshInitializationIntent
from auto_coder.management.fresh_runtime_binding import verify_installed_fresh_binding
from auto_coder.management.oauth_config import load_management_settings
from auto_coder.tested_source_refs import TestedSourcePublisher
from auto_coder.util.gh_cache import GitHubGitDataClient
bootstrap=Path('/bootstrap/config.json')
config=BootstrapConfig.read(bootstrap)
settings=load_management_settings(config.management_config)
intent=FreshInitializationIntent(**json.loads(Path('/upgrade-intent.json').read_text()))
token=settings.source_evidence_token_file.read_text().strip()
authority=TestedSourcePublisher(GitHubGitDataClient(token, settings.source_repository), settings.source_repository)
verify_installed_fresh_binding(config, bootstrap, intent, settings, source_authority=authority)
print('INSTALLED_CANDIDATE_VERIFIED')
"""
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "bridge",
        "--read-only",
        "--user",
        f"{os.geteuid()}:{os.getegid()}",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
    ]
    for source, destination, _writable in mounts:
        command.extend(("--mount", f"type=bind,src={source},dst={destination},readonly"))
    command.extend(("--mount", f"type=bind,src={profile.intent_file},dst=/upgrade-intent.json,readonly"))
    command.extend(("--entrypoint", "/opt/auto-coder/.venv/bin/python", intent.image_digest, "-I", "-B", "-c", script))
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=240)
    if not result.stdout.strip().endswith("INSTALLED_CANDIDATE_VERIFIED"):
        raise ValueError("installed candidate did not prove its actual READY")


def _configured_source_credential(config: Path) -> Path:
    """Validate the operator extension's immutable YAML and container-path binding."""
    raw = yaml.safe_load(_read_record(config))
    if not isinstance(raw, dict) or "source_repository" in raw or "source_evidence_token_file" in raw:
        raise ValueError("source evidence must use the protected extension")
    extension = json.loads(_read_record(Path(str(config) + ".source-evidence.json")))
    expected = {
        "schema_version": 1,
        "management_config_sha256": hashlib.sha256(_read_record(config)).hexdigest(),
        "source_repository": REPOSITORY,
        "source_evidence_token_file": "/bootstrap/source_evidence_read",
    }
    if extension != expected or not isinstance(extension, dict) or set(extension) != set(expected):
        raise ValueError("source evidence extension differs from immutable YAML")
    token = config.parent / "source_evidence_read"
    descriptor = protected_credential(token, mode=0o600)
    os.close(descriptor)
    return token


def _worker_registry(config: BootstrapConfig) -> dict[str, object]:
    runtime = next((item for item in config.runtimes if item.reference == config.initial_runtime), None)
    if runtime is None:
        raise ValueError("selected management runtime is unavailable")
    environment = dict(runtime.environment)
    raw = environment.get("AUTO_CODER_WORKER_REGISTRY")
    if raw is None:
        raise ValueError("configured worker registry is not available")
    registry = json.loads(raw)
    workers = registry.get("workers") if isinstance(registry, dict) else None
    if not isinstance(workers, list) or not workers or any(not isinstance(item, dict) or not isinstance(item.get("target_id"), str) for item in workers):
        raise ValueError("worker registry is incomplete")
    if len({item["target_id"] for item in workers}) != len(workers):
        raise ValueError("worker targets are not distinct")
    return registry


def _worker_targets(config: BootstrapConfig) -> tuple[str, ...]:
    workers = _worker_registry(config)["workers"]
    assert isinstance(workers, list)
    return tuple(item["target_id"] for item in workers)


def _oauth_snapshot(root: Path) -> tuple[str, dict[str, str]]:
    path = root / "oauth.sqlite3"
    if path.is_symlink() or not path.is_file():
        raise ValueError("retained OAuth database is unavailable")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as connection:
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("retained OAuth store integrity unavailable")
        authority = connection.execute("SELECT issuer,resource,project FROM authority WHERE singleton=1").fetchall()
        if len(authority) != 1:
            raise ValueError("retained OAuth authority is incomplete")
        grants = connection.execute("SELECT * FROM grants ORDER BY id").fetchall()
    if any(not isinstance(row[0], str) or not row[0] for row in grants):
        raise ValueError("retained grants contain invalid identities")
    return hashlib.sha256(_canonical(authority[0])).hexdigest(), {row[0]: hashlib.sha256(_canonical(row)).hexdigest() for row in grants}


def _mcp_call(port: int, token: str, tool: str, arguments: dict[str, object]) -> dict[str, object]:
    body = _canonical({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}})
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Authorization": "Bearer " + token, "Host": "127.0.0.1:8080"}
    # A fixed direct loopback connection must never follow an HTTP redirect
    # that could forward the separately supplied operator bearer elsewhere.
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        client.request("POST", "/mcp", body=body, headers=headers)
        response = client.getresponse()
        if response.status != 200 or "application/json" not in (response.getheader("Content-Type") or ""):
            raise ValueError("authenticated loopback response is unavailable")
        encoded = response.read(65537)
        if len(encoded) > 65536:
            raise ValueError("authenticated loopback response exceeds the fixed limit")
        data = json.loads(encoded)
    finally:
        client.close()
    result = data.get("result") if isinstance(data, dict) else None
    content = result.get("structuredContent") if isinstance(result, dict) else None
    if not isinstance(content, dict):
        raise ValueError("authenticated management response unavailable")
    return content


def _request_fresh_readback(port: int, credential: Path, fresh: dict[str, object]) -> None:
    """Request the existing child's observation-bound proof, never infer success."""
    observation_id = fresh.get("observation_id")
    if not isinstance(observation_id, str) or len(observation_id) != 32 or any(char not in "0123456789abcdef" for char in observation_id):
        raise ValueError("fresh observation identity is incomplete")
    token = _read_record(credential).decode("utf-8").strip()
    if not token or "\n" in token:
        raise ValueError("fresh authenticated client credential invalid")
    identity = _mcp_call(port, token, "auto_coder_whoami", {"observation_id": observation_id})
    if not isinstance(identity.get("uid"), str) or not identity["uid"]:
        raise ValueError("fresh observation-bound authenticated identity unavailable")


def _authenticated_baseline(port: int, targets: tuple[str, ...], credential: Path) -> dict[str, dict[str, str]]:
    token = _read_record(credential).decode("utf-8").strip()
    if not token or "\n" in token:
        raise ValueError("fresh authenticated client credential invalid")
    identity = _mcp_call(port, token, "auto_coder_whoami", {})
    if not isinstance(identity.get("uid"), str) or not identity["uid"]:
        raise ValueError("authenticated management identity is unavailable")
    baseline: dict[str, dict[str, str]] = {}
    for target in targets:
        result = _mcp_call(port, token, "auto_coder_deployment_status", {"target_id": target})
        worker = result.get("worker_observation")
        if result.get("status") != "OK" or result.get("alive") is not True or not isinstance(worker, dict):
            raise ValueError("configured worker readback unavailable")
        values = {"supervisor_generation": result.get("supervisor_generation"), "process_run_id": worker.get("process_run_id"), "deployment_generation": worker.get("deployment_generation")}
        if any(not isinstance(item, str) or not item for item in values.values()):
            raise ValueError("worker lifetime evidence incomplete")
        baseline[target] = values  # type: ignore[assignment]
    return baseline


def _load_plan(root: Path, sha: str) -> UpgradePlan:
    if not _SHA.fullmatch(sha):
        raise UpgradeUnavailable("prerequisite-blocked", "exact prepared plan identity required")
    lane = root / sha
    _protected_root(lane)
    encoded = _read_record(lane / "upgrade-plan.json")
    if hashlib.sha256(encoded).hexdigest() != sha:
        raise UpgradeUnavailable("needs-attention", "immutable plan differs from approved identity")
    raw = json.loads(encoded)
    if not isinstance(raw, dict) or set(raw) != set(UpgradePlan.__dataclass_fields__):
        raise UpgradeUnavailable("needs-attention", "plan schema differs")
    raw["profile"] = UpgradeProfile(**raw["profile"])
    raw["ancestor_identities"] = tuple(tuple(item) for item in raw["ancestor_identities"])
    result = UpgradePlan(**raw)
    if result.version != 1:
        raise UpgradeUnavailable("needs-attention", "plan version unsupported")
    result.profile.validate()
    return result


def _operation(root: Path, sha: str) -> UpgradeOperation:
    if not _SHA.fullmatch(sha):
        raise UpgradeUnavailable("prerequisite-blocked", "invalid upgrade operation identity")
    lane = root / sha
    _protected_root(lane)
    raw = json.loads(_read_record(lane / "upgrade-operation.json"))
    if not isinstance(raw, dict) or set(raw) != set(UpgradeOperation.__dataclass_fields__):
        raise UpgradeUnavailable("needs-attention", "upgrade operation schema differs")
    raw["completed"] = tuple(raw["completed"])
    result = UpgradeOperation(**raw)
    if (
        result.plan_sha256 != sha
        or result.operation_id != sha
        or result.stage not in _STAGES
        or result.effect not in ("", *_EFFECTS)
        or result.completed != _EFFECTS[: len(result.completed)]
        or (result.effect and (len(result.completed) == len(_EFFECTS) or result.effect != _EFFECTS[len(result.completed)]))
        or bool(result.effect) != bool(result.effect_target)
        or type(result.watcher_pid) is not int
        or result.watcher_pid < 0
        or not isinstance(result.watcher_start_token, str)
        or not isinstance(result.effect_receipts, dict)
        or any(name not in {"start-management", "start-proxy"} or not isinstance(value, str) or not value for name, value in result.effect_receipts.items())
        or any(name not in result.completed for name in result.effect_receipts)
        or any(name in result.completed and name not in result.effect_receipts for name in ("start-management", "start-proxy"))
        or ("start-watcher" in result.completed and (result.watcher_pid <= 0 or not result.watcher_start_token))
    ):
        raise UpgradeUnavailable("needs-attention", "upgrade operation identity differs")
    return result


def _record_operation(root: Path, old: UpgradeOperation, *, stage: str, effect: str = "", target: str = "", completed: tuple[str, ...] | None = None, reason: str = "") -> UpgradeOperation:
    updated = UpgradeOperation(old.plan_sha256, old.operation_id, stage, effect, target, old.completed if completed is None else completed, reason, old.watcher_pid, old.watcher_start_token, old.effect_receipts)
    _replace_record(root, "upgrade-operation.json", _canonical(asdict(updated)))
    return updated


def _immutable_inputs(plan: UpgradePlan) -> None:
    if _input_hashes(plan.profile) != plan.input_sha256:
        raise UpgradeUnavailable("prerequisite-blocked", "effect-relevant protected file changed; prepare a new reviewed plan")
    management, proxy, digest = _evidence(Path(plan.profile.evidence_file))
    if (management.container_id, proxy.container_id, management.image_id, proxy.image_id, digest, management.started_at, proxy.started_at) != (
        plan.predecessor_management,
        plan.predecessor_proxy,
        plan.predecessor_management_image,
        plan.predecessor_proxy_image,
        plan.predecessor_evidence_sha256,
        plan.predecessor_started_at,
        plan.predecessor_proxy_started_at,
    ):
        raise UpgradeUnavailable("prerequisite-blocked", "captured predecessor identity changed")
    intent = _fresh_intent(plan.profile)
    if (intent.new_container_id, intent.image_digest, intent.new_instance, intent.request_id, intent.host_listener_port) != (
        plan.successor_management,
        plan.successor_image,
        plan.successor_instance,
        plan.request_id,
        plan.endpoint_port,
    ):
        raise UpgradeUnavailable("prerequisite-blocked", "prepared successor differs")
    if tuple(item.identity() for item in required_exclusions(management)) != plan.ancestor_identities:
        raise UpgradeUnavailable("prerequisite-blocked", "inherited ancestor binding changed")
    if validate_successor(management, intent, Path(plan.profile.bootstrap_config)) != tuple(required_exclusions(management)):
        raise UpgradeUnavailable("prerequisite-blocked", "successor exclusion ancestry differs")
    assert_retained_oauth_mount(management, intent)
    assert_retained_journal_source(management, intent)
    for expected_id, expected_image, expected_started_at in (
        (management.container_id, management.image_id, plan.predecessor_started_at),
        (proxy.container_id, proxy.image_id, plan.predecessor_proxy_started_at),
        (intent.new_container_id, intent.image_digest, None),
        (plan.profile.successor_proxy_id, plan.profile.successor_proxy_image, None),
    ):
        observed = _inspect(expected_id)
        if observed[1] != expected_image:
            raise UpgradeUnavailable("prerequisite-blocked", "pinned container image changed")
        # A stopped predecessor retains its original Docker StartedAt. Reject
        # any restarted generation before touching the saved host effects.
        if expected_started_at is not None and observed[13] != expected_started_at:
            raise UpgradeUnavailable("prerequisite-blocked", "pinned predecessor process generation changed")
    if _candidate_image_source(plan.successor_management, plan.successor_image) != plan.image_source_identity:
        raise UpgradeUnavailable("prerequisite-blocked", "pinned installed candidate differs")
    candidate = BootstrapConfig.read(Path(plan.profile.bootstrap_config))
    if set(_worker_targets(candidate)) != set(plan.worker_baseline):
        raise UpgradeUnavailable("prerequisite-blocked", "candidate worker target identities changed")
    if hashlib.sha256(_canonical(_worker_registry(candidate))).hexdigest() != plan.worker_registry_sha256:
        raise UpgradeUnavailable("prerequisite-blocked", "candidate worker target configuration changed")
    if _source_proof(_source_authority(_configured_source_credential(Path(plan.profile.management_config))), plan.profile.source_sha) != (plan.tested_source_identity, plan.supervisor_run_id, plan.supervisor_run_attempt):
        raise UpgradeUnavailable("prerequisite-blocked", "tested-source admission evidence changed")


def prepare(root: Path, profile_file: Path, authenticated_client_file: Path, *, authorize_source_read: bool) -> str:
    """Write a reviewable, immutable plan without any live Docker effect."""
    if not authorize_source_read:
        raise UpgradeUnavailable("prerequisite-blocked", "source credential use requires explicit operator acceptance")
    with _exclusive(root):
        for entry in root.iterdir():
            if entry.name in {"upgrade.lock", "upgrade-audit.log"}:
                continue
            if not _SHA.fullmatch(entry.name) or entry.is_symlink():
                raise UpgradeUnavailable("needs-attention", "unrecognized operator history; retain it for inspection")
            old = _operation(root, entry.name)
            if old.stage not in {"application-ready", "beta-deploy-prerequisites-verified"} or old.effect or old.completed != _EFFECTS:
                raise UpgradeUnavailable("needs-attention", "earlier host upgrade is unresolved; no replacement plan is authorized")
        profile = _read_profile(profile_file)
        management, proxy, digest = _evidence(Path(profile.evidence_file))
        intent = _fresh_intent(profile)
        mount = _mount_profile(profile)
        current = capture_prior_container(management.container_id, management.image_id, expected_mounts=mount["management"], require_owner_lock=True, require_deployment_identity=True)
        captured_proxy = capture_prior_container(proxy.container_id, proxy.image_id, expected_mounts=mount["proxy"])
        if current != management or captured_proxy != proxy or proxy.network_mode != "container:" + management.container_id or proxy.net_namespace != management.net_namespace:
            raise UpgradeUnavailable("prerequisite-blocked", "predecessor changed after capture")
        if intent.old_instance != management.instance_id or intent.old_container_id != management.container_id or intent.prior_evidence_sha256 != digest or intent.host_listener_port != management.published_port:
            raise UpgradeUnavailable("prerequisite-blocked", "fresh intent and captured predecessor differ")
        if not intent.host_prior_journal_root:
            raise UpgradeUnavailable("prerequisite-blocked", "complete retained predecessor journal is required")
        assert_retained_oauth_mount(management, intent)
        assert_retained_journal_source(management, intent)
        bindings = validate_successor(management, intent, Path(profile.bootstrap_config))
        check_host_archives(bindings)
        config = BootstrapConfig.read(Path(profile.bootstrap_config))
        if Path(profile.bootstrap_config).parent != Path(profile.management_config).parent:
            raise UpgradeUnavailable("prerequisite-blocked", "candidate configuration files must share the sealed bootstrap directory")
        if config.instance_id != intent.new_instance or config.fresh_runtime_binding is None or config.management_config != Path("/bootstrap") / Path(profile.management_config).name:
            raise UpgradeUnavailable("prerequisite-blocked", "successor installed-runtime profile is incomplete")
        if hashlib.sha256(_read_record(Path(profile.bootstrap_config))).hexdigest() != intent.bootstrap_config_sha256 or hashlib.sha256(_read_record(Path(profile.management_config))).hexdigest() != intent.management_config_sha256:
            raise UpgradeUnavailable("prerequisite-blocked", "candidate immutable configuration bytes changed")
        _candidate_stack(profile, intent)
        _prepared_roots(profile, intent)
        old_watcher_root = _watcher_binding(Path(profile.old_watcher_config), management.container_id, management.image_id, proxy.container_id, proxy.image_id, Path(profile.old_proxy_config))
        new_watcher_root = _watcher_binding(Path(profile.new_watcher_config), intent.new_container_id, intent.image_digest, profile.successor_proxy_id, profile.successor_proxy_image, Path(profile.successor_proxy_config))
        if old_watcher_root == new_watcher_root or old_watcher_root in new_watcher_root.parents or new_watcher_root in old_watcher_root.parents:
            raise UpgradeUnavailable("prerequisite-blocked", "replacement watcher must use a separate protected root")
        if DockerProxyObservation.config_digest(proxy.container_id) != hashlib.sha256(_read_record(Path(profile.old_proxy_config))).hexdigest():
            raise UpgradeUnavailable("prerequisite-blocked", "predecessor proxy configuration differs")
        source = _candidate_image_source(intent.new_container_id, intent.image_digest)
        if source != profile.source_sha:
            raise UpgradeUnavailable("prerequisite-blocked", "installed candidate image differs from exact tested source")
        token_file = _configured_source_credential(Path(profile.management_config))
        authority = _source_authority(token_file)
        source_identity, run_id, attempt = _source_proof(authority, source)
        _installed_candidate_preflight(profile)
        predecessor_config = BootstrapConfig.read(Path(management.bootstrap_source))
        targets = _worker_targets(predecessor_config)
        if _worker_targets(config) != targets or _worker_registry(config) != _worker_registry(predecessor_config):
            raise UpgradeUnavailable("prerequisite-blocked", "worker target registry differs across generations")
        workers = _authenticated_baseline(intent.host_listener_port, targets, authenticated_client_file)
        oauth_authority, grants = _oauth_snapshot(Path(intent.host_oauth_root))
        plan = UpgradePlan(
            1,
            profile,
            management.container_id,
            proxy.container_id,
            management.image_id,
            proxy.image_id,
            management.started_at,
            proxy.started_at,
            digest,
            _input_hashes(profile),
            source,
            source_identity,
            run_id,
            attempt,
            workers,
            hashlib.sha256(_canonical(_worker_registry(config))).hexdigest(),
            oauth_authority,
            grants,
            tuple(item.identity() for item in bindings),
            intent.host_listener_port,
            intent.new_container_id,
            intent.image_digest,
            intent.new_instance,
            intent.request_id,
        )
        encoded = _canonical(asdict(plan))
        sha = hashlib.sha256(encoded).hexdigest()
        lane = root / sha
        lane.mkdir(mode=0o700)
        _create_record(lane, "upgrade-plan.json", encoded)
        operation = UpgradeOperation(sha, sha, "prepared", "", "", ())
        _create_record(lane, "upgrade-operation.json", _canonical(asdict(operation)))
        return sha


def _effect(root: Path, operation: UpgradeOperation, plan: UpgradePlan, action: str, target: str) -> UpgradeOperation:
    if action not in _EFFECTS[:4] or action in operation.completed or operation.effect:
        raise UpgradeUnavailable("needs-attention", "effect was already sent or completed")
    _immutable_inputs(plan)
    next_stage = "stopping" if action.startswith("stop-") else "successor-launch-uncertain"
    observed = _inspect(target)
    expected_image = plan.predecessor_proxy_image if action == "stop-proxy" else plan.predecessor_management_image if action == "stop-management" else plan.successor_image if action == "start-management" else plan.profile.successor_proxy_image
    if observed[1] != expected_image or observed[2] is not (action.startswith("stop-")):
        raise UpgradeUnavailable("needs-attention", "exact physical target changed before dispatch")
    if action.startswith("stop-") and observed[13] != (plan.predecessor_proxy_started_at if action == "stop-proxy" else plan.predecessor_started_at):
        raise UpgradeUnavailable("needs-attention", "predecessor process generation changed")
    operation = _record_operation(root, operation, stage=next_stage, effect=action, target=target)
    if action.startswith("stop-"):
        _docker("kill", "--signal=SIGTERM", target)
        expected_started = plan.predecessor_proxy_started_at if action == "stop-proxy" else plan.predecessor_started_at
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            value = _inspect(target)
            if value[1] != expected_image or value[13] != expected_started:
                raise UpgradeUnavailable("needs-attention", "exact predecessor changed during graceful stop")
            if value[2] is False and value[3] == 0:
                break
            time.sleep(0.1)
        else:
            raise UpgradeUnavailable("needs-attention", "graceful stop timed out; no forced termination")
    else:
        _docker("start", target)
        if _inspect(target)[2] is not True:
            raise UpgradeUnavailable("needs-attention", "successor start outcome not authoritatively observed")
    receipts = dict(operation.effect_receipts)
    if action.startswith("start-"):
        observed = _inspect(target)
        if observed[1] != expected_image or observed[2] is not True or type(observed[3]) is not int or observed[3] <= 0 or not isinstance(observed[13], str) or not observed[13]:
            raise UpgradeUnavailable("needs-attention", "successor process lifetime receipt unavailable")
        receipts[action] = observed[13]
    completed_operation = UpgradeOperation(operation.plan_sha256, operation.operation_id, next_stage, "", "", (*operation.completed, action), "", operation.watcher_pid, operation.watcher_start_token, receipts)
    _replace_record(root, "upgrade-operation.json", _canonical(asdict(completed_operation)))
    return completed_operation


def _watcher_lifetime(pid: int) -> str:
    if type(pid) is not int or pid <= 0:
        raise ValueError("new watcher process identity unavailable")
    fields = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
    if len(fields) < 20 or not fields[19].isdecimal() or int(fields[19]) <= 0 or fields[0] in {"Z", "X"}:
        raise ValueError("new watcher lifetime missing or exited")
    return fields[19]


def _watcher_is_owned(root: Path, operation: UpgradeOperation, path: Path) -> bool:
    if operation.watcher_pid <= 0 or not operation.watcher_start_token:
        return False
    try:
        if _watcher_lifetime(operation.watcher_pid) != operation.watcher_start_token:
            return False
        command = (Path("/proc") / str(operation.watcher_pid) / "cmdline").read_bytes().strip(b"\0").split(b"\0")
        if command[-4:] != [b"-m", b"auto_coder.management.proxy_watch_cli", b"--config", os.fsencode(path)]:
            return False
        descriptor = os.open(root / "proxy-watch.lock", os.O_RDONLY | os.O_NOFOLLOW)
        try:
            _assert_pid_flock(operation.watcher_pid, descriptor, "WRITE")
            return True
        finally:
            os.close(descriptor)
    except (OSError, ValueError, IndexError):
        return False


def _start_watcher(root: Path, operation: UpgradeOperation, plan: UpgradePlan) -> UpgradeOperation:
    """Start only the prepared replacement watcher; never retry uncertain Popen."""
    if operation.effect or operation.completed != _EFFECTS[:4]:
        raise UpgradeUnavailable("needs-attention", "watcher start ordering or earlier effect differs")
    _immutable_inputs(plan)
    manager = _inspect(plan.successor_management)
    proxy = DockerProxyObservation.inspect(plan.profile.successor_proxy_id)
    proxy_pid = proxy.get("pid")
    if (
        manager[1] != plan.successor_image
        or manager[2] is not True
        or manager[13] != operation.effect_receipts.get("start-management")
        or proxy.get("image") != plan.profile.successor_proxy_image
        or proxy.get("running") is not True
        or proxy.get("started") != operation.effect_receipts.get("start-proxy")
        or type(proxy_pid) is not int
        or DockerProxyObservation.namespace(proxy_pid) != os.readlink(f"/proc/{manager[3]}/ns/net")
    ):
        raise UpgradeUnavailable("needs-attention", "replacement proxy is not attached to the exact launched manager")
    config = Path(plan.profile.new_watcher_config)
    watcher_root = _watcher_binding(config, plan.successor_management, plan.successor_image, plan.profile.successor_proxy_id, plan.profile.successor_proxy_image, Path(plan.profile.successor_proxy_config))
    lock = os.open(watcher_root / "proxy-watch.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(lock)
        raise UpgradeUnavailable("needs-attention", "replacement watcher is already active or unavailable") from None
    fcntl.flock(lock, fcntl.LOCK_UN)
    os.close(lock)
    operation = _record_operation(root, operation, stage="successor-launch-uncertain", effect="start-watcher", target=str(config))
    output = os.open(root / "upgrade-watcher.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-B", "-m", "auto_coder.management.proxy_watch_cli", "--config", str(config)],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            close_fds=True,
            start_new_session=True,
        )
    finally:
        os.close(output)
    token = _watcher_lifetime(proc.pid)
    observed = UpgradeOperation(operation.plan_sha256, operation.operation_id, operation.stage, operation.effect, operation.effect_target, operation.completed, operation.reason, proc.pid, token, operation.effect_receipts)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if proc.poll() is not None or _watcher_lifetime(proc.pid) != token:
            raise UpgradeUnavailable("needs-attention", "replacement watcher failed to retain its process lifetime")
        if _watcher_is_owned(watcher_root, observed, config):
            final = UpgradeOperation(operation.plan_sha256, operation.operation_id, "successor-launch-uncertain", "", "", (*operation.completed, "start-watcher"), "", proc.pid, token, operation.effect_receipts)
            _replace_record(root, "upgrade-operation.json", _canonical(asdict(final)))
            return final
        time.sleep(0.1)
    raise UpgradeUnavailable("needs-attention", "replacement watcher did not acquire its controller lock")


def _observe_uncertain_effect(operation: UpgradeOperation, plan: UpgradePlan) -> str:
    """Join a lost response to the original physical ID without re-dispatch."""
    if operation.effect == "start-watcher":
        return "unresolved-watcher-launch"
    if operation.effect not in _EFFECTS[:4]:
        return "no-dispatched-effect"
    target = operation.effect_target
    expected_target = {
        "stop-proxy": plan.predecessor_proxy,
        "stop-management": plan.predecessor_management,
        "start-management": plan.successor_management,
        "start-proxy": plan.profile.successor_proxy_id,
    }[operation.effect]
    if target != expected_target:
        return "effect-target-conflict"
    try:
        observed = _inspect(target)
        expected_image = {
            "stop-proxy": plan.predecessor_proxy_image,
            "stop-management": plan.predecessor_management_image,
            "start-management": plan.successor_image,
            "start-proxy": plan.profile.successor_proxy_image,
        }[operation.effect]
        if observed[1] != expected_image:
            return "container-image-conflict"
        if operation.effect.startswith("stop-"):
            started = plan.predecessor_proxy_started_at if operation.effect == "stop-proxy" else plan.predecessor_started_at
            if observed[13] != started:
                return "predecessor-generation-conflict"
            return "same-predecessor-stopped" if observed[2] is False and observed[3] == 0 else "predecessor-not-proven-stopped"
        return "successor-running" if observed[2] is True and type(observed[3]) is int and observed[3] > 0 else "successor-not-proven-running"
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return "physical-effect-observation-unavailable"


def _refuse_known_unresolved_work_before_stop(intent: FreshInitializationIntent) -> None:
    """Reject live nonterminal work without trying to take the incumbent's lock.

    This read-only snapshot is a pre-stop refusal, not admission evidence. A
    concurrent operation can still arrive; _current_journal_clear must recheck
    the complete authoritative retained journal after the predecessor is fenced.
    """
    database = Path(intent.host_prior_journal_root) / "management-operations.sqlite3"
    info = database.lstat()
    expected = (intent.prior_journal_device, intent.prior_journal_inode)
    if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != expected:
        raise UpgradeUnavailable("prerequisite-blocked", "predecessor journal identity changed before stop")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        phases = {row[0] for row in connection.execute("SELECT DISTINCT phase FROM operations")}
        if not phases <= (ACTIVE_PHASES | TERMINAL_PHASES):
            raise UpgradeUnavailable("prerequisite-blocked", "predecessor operation phase cannot be validated")
        if phases & ACTIVE_PHASES:
            raise UpgradeUnavailable("prerequisite-blocked", "predecessor has unresolved operations; incumbent remains running")
        connection.rollback()
    checked = database.lstat()
    if not stat.S_ISREG(checked.st_mode) or (checked.st_dev, checked.st_ino) != expected:
        raise UpgradeUnavailable("prerequisite-blocked", "predecessor journal identity changed during pre-stop check")


def _current_journal_clear(intent: FreshInitializationIntent, old_instance: str, prior_lock: Path) -> None:
    database = Path(intent.host_prior_journal_root) / "management-operations.sqlite3"
    with RetainedJournal(database, prior_lock, database_identity=(intent.prior_journal_device, intent.prior_journal_inode), fence_identity=(intent.owner_lock_device, intent.owner_lock_inode), prior_management_instance=old_instance) as journal:
        if any(record.phase in ACTIVE_PHASES for record in journal.read()):
            raise UpgradeUnavailable("needs-attention", "predecessor has unresolved operations; retain its original history")


def _watcher_lock(path: Path, plan: UpgradePlan) -> int:
    root = _watcher_binding(path, plan.predecessor_management, plan.predecessor_management_image, plan.predecessor_proxy, plan.predecessor_proxy_image, Path(plan.profile.old_proxy_config))
    descriptor = os.open(root / "proxy-watch.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise UpgradeUnavailable("prerequisite-blocked", "watcher lock is not private")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def apply(root: Path, sha: str, authenticated_client_file: Path, *, authorize_source_read: bool) -> UpgradeOperation:
    """One accepted operation, with no effect replay after uncertainty."""
    if not authorize_source_read:
        raise UpgradeUnavailable("prerequisite-blocked", "source credential use requires separate explicit operator acceptance")
    with _exclusive(root):
        plan = _load_plan(root, sha)
        operation = _operation(root, sha)
        lane = root / sha
        if operation.stage != "prepared" or operation.effect or operation.completed:
            raise UpgradeUnavailable("needs-attention", "prior apply has already begun; reconcile the original effect")
        _immutable_inputs(plan)
        intent = _fresh_intent(plan.profile)
        management, _, _ = _evidence(Path(plan.profile.evidence_file))
        mount = _mount_profile(plan.profile)
        observed = capture_prior_container(management.container_id, management.image_id, expected_mounts=mount["management"], require_owner_lock=True, require_deployment_identity=True)
        if observed != management:
            raise UpgradeUnavailable("prerequisite-blocked", "predecessor changed before first effect")
        _candidate_stack(plan.profile, intent)
        if _candidate_image_source(intent.new_container_id, intent.image_digest) != plan.image_source_identity:
            raise UpgradeUnavailable("prerequisite-blocked", "candidate source binding changed")
        authority = _source_authority(_configured_source_credential(Path(plan.profile.management_config)))
        if _source_proof(authority, plan.profile.source_sha) != (plan.tested_source_identity, plan.supervisor_run_id, plan.supervisor_run_attempt):
            raise UpgradeUnavailable("prerequisite-blocked", "tested source evidence changed")
        _installed_candidate_preflight(plan.profile)
        if _authenticated_baseline(plan.endpoint_port, tuple(plan.worker_baseline), authenticated_client_file) != plan.worker_baseline:
            raise UpgradeUnavailable("prerequisite-blocked", "worker baseline changed before effect")
        # Refuse known active work *before* dispatching any stop, then retain
        # the fenced post-stop complete-journal validation.
        _refuse_known_unresolved_work_before_stop(intent)
        watcher = _watcher_lock(Path(plan.profile.old_watcher_config), plan)
        network = -1
        try:
            network = os.open(f"/proc/{management.pid}/ns/net", os.O_RDONLY | os.O_CLOEXEC)

            def on_fenced() -> None:
                nonlocal operation
                operation = _effect(lane, operation, plan, "stop-proxy", plan.predecessor_proxy)
                operation = _effect(lane, operation, plan, "stop-management", plan.predecessor_management)

            def on_stopped() -> None:
                _current_journal_clear(intent, management.instance_id, Path(plan.profile.prior_lock))

            def on_listening() -> None:
                nonlocal operation
                operation = _effect(lane, operation, plan, "start-management", plan.successor_management)

            hold_claim_and_guard(intent, Path(plan.profile.evidence_file), Path(plan.profile.bootstrap_config), Path(plan.profile.prior_lock), network, Path(plan.profile.guard_dir), on_fenced=on_fenced, on_stopped=on_stopped, on_listening=on_listening)
            operation = _effect(lane, operation, plan, "start-proxy", plan.profile.successor_proxy_id)
            return _start_watcher(lane, operation, plan)
        except BaseException as error:
            # The effect journal is authoritative: _effect may have persisted a
            # dispatch before its callback returned to update this local value.
            # Reload under the same operator lock and never erase an uncertain
            # target, completed prefix, or launch receipt on an exception.
            latest = _operation(root, sha)
            if latest.effect or latest.completed:
                _record_operation(
                    lane,
                    latest,
                    stage="needs-attention",
                    effect=latest.effect,
                    target=latest.effect_target,
                    reason=type(error).__name__,
                )
                # A command may lose the Docker response after a real physical
                # effect. Its CLI return must reflect the persisted uncertainty,
                # not classify a post-dispatch timeout as a preflight refusal.
                # Preserve KeyboardInterrupt/SystemExit for host interruption.
                if isinstance(error, Exception):
                    raise UpgradeUnavailable("needs-attention", "original effect or host fence requires inspection: " + type(error).__name__) from error
            raise
        finally:
            if network >= 0:
                os.close(network)
            os.close(watcher)


def status(root: Path, sha: str, *, authenticated_client_file: Path | None = None, authorize_source_read: bool = False) -> dict[str, object]:
    """Reconcile read-only. A former controller's lost fence is never rebuilt."""
    with _exclusive(root):
        plan = _load_plan(root, sha)
        operation = _operation(root, sha)
        lane = root / sha
        answer: dict[str, object] = {"operation_id": operation.operation_id, "plan_sha256": sha, "stage": operation.stage, "completed_effects": list(operation.completed), "uncertain_effect": operation.effect, "uncertain_target": operation.effect_target, "reason": operation.reason}
        if operation.stage == "prepared":
            try:
                _immutable_inputs(plan)
            except (OSError, ValueError, UpgradeUnavailable, HostObservationUnavailable):
                answer["stage"] = "prerequisite-blocked"
                answer["reason"] = "effect-relevant input unavailable; prepare a separately reviewed plan"
            return answer
        if len(operation.completed) < len(_EFFECTS):
            answer["stage"] = "needs-attention"
            answer["reason"] = "original effect or continuous host fence requires operator inspection; no automatic replay"
            answer["physical_effect_observation"] = _observe_uncertain_effect(operation, plan)
            return answer
        try:
            intent = _fresh_intent(plan.profile)
            fresh = read_fresh_status(Path(intent.host_state_root), host=True)
            manager = _inspect(intent.new_container_id)
            proxy = DockerProxyObservation.inspect(plan.profile.successor_proxy_id)
            proxy_pid = proxy.get("pid")
            new_watcher_root = _watcher_binding(Path(plan.profile.new_watcher_config), plan.successor_management, plan.successor_image, plan.profile.successor_proxy_id, plan.profile.successor_proxy_image, Path(plan.profile.successor_proxy_config))
            if (
                manager[1] != intent.image_digest
                or manager[2] is not True
                or manager[13] != operation.effect_receipts.get("start-management")
                or proxy.get("image") != plan.profile.successor_proxy_image
                or proxy["running"] is not True
                or proxy.get("started") != operation.effect_receipts.get("start-proxy")
                or type(proxy_pid) is not int
                or DockerProxyObservation.namespace(proxy_pid) != os.readlink(f"/proc/{manager[3]}/ns/net")
                or not _watcher_is_owned(new_watcher_root, operation, Path(plan.profile.new_watcher_config))
            ):
                answer["stage"] = "successor-launch-uncertain"
                answer["reason"] = "authenticated fresh-child or proxy attachment not proven"
                return answer
            # The fresh observer only seals "succeeded" after a real
            # observation-bound authenticated whoami read. A newly launched
            # successor has not yet seen that read: status must initiate it
            # over the pinned loopback endpoint instead of waiting for a
            # separate test-only/client-side call. This is read-only and must
            # never substitute the observer's independently sealed outcome.
            if fresh["status"] == "launched" and authenticated_client_file is not None:
                _request_fresh_readback(plan.endpoint_port, authenticated_client_file, fresh)
                fresh = read_fresh_status(Path(intent.host_state_root), host=True)
            if fresh["status"] != "succeeded":
                answer["stage"] = "successor-launch-uncertain"
                answer["reason"] = "fresh authenticated child outcome has not been sealed"
                return answer
            management, _, _ = _evidence(Path(plan.profile.evidence_file))
            assert_retained_oauth_mount(management, intent)
            assert_retained_journal_source(management, intent)
            if authenticated_client_file is None:
                answer["stage"] = "successor-launch-uncertain"
                answer["missing_prerequisites"] = ["fresh authorized authenticated client readback"]
                return answer
            baseline = _authenticated_baseline(plan.endpoint_port, tuple(plan.worker_baseline), authenticated_client_file)
            if baseline != plan.worker_baseline:
                raise ValueError("retained worker lifetime changed")
            original_authority, original_grants = _oauth_snapshot(Path(intent.host_oauth_root))
            if original_authority != plan.oauth_authority_sha256 or any(original_grants.get(key) != value for key, value in plan.oauth_grant_sha256.items()):
                raise ValueError("retained OAuth or grant authority changed")
            answer["stage"] = "application-ready"
            # A verified beta prerequisite is a current observation, not a
            # permanent authorization. Demote the durable stage before every
            # new source read, including absent consent or a moved selection.
            if operation.stage != "application-ready":
                operation = _record_operation(lane, operation, stage="application-ready")
            if not authorize_source_read:
                answer["missing_prerequisites"] = ["explicit source credential read approval"]
                return answer
            workers = _worker_registry(BootstrapConfig.read(Path(plan.profile.bootstrap_config)))["workers"]
            if not isinstance(workers, list):
                answer["missing_prerequisites"] = ["configured beta worker target"]
                return answer
            beta_targets = [item["target_id"] for item in workers if item.get("channel") == "beta"]
            if not beta_targets or any(target not in baseline for target in beta_targets):
                answer["missing_prerequisites"] = ["configured beta worker target readback"]
                return answer
            # The host's GitHub access cannot prove that the upgraded child can
            # read its configured credential, source proof and beta selection.
            # Ask the authenticated child to run the existing production reader.
            token = _read_record(authenticated_client_file).decode("utf-8").strip()
            if not token or "\n" in token:
                answer["missing_prerequisites"] = ["fresh authorized authenticated client"]
                return answer
            selected: dict[str, object] | None = None
            for target in beta_targets:
                evidence = _mcp_call(
                    plan.endpoint_port,
                    token,
                    "auto_coder_beta_source_readback",
                    {"target_id": target},
                )
                if evidence.get("status") != "OK":
                    reason = evidence.get("reason")
                    safe_reason = reason if isinstance(reason, str) and len(reason) <= 128 else "unavailable"
                    answer["missing_prerequisites"] = [f"{target}: upgraded-service beta source readback {safe_reason}"]
                    return answer
                selected_identity = (
                    evidence.get("target_id"),
                    evidence.get("repository"),
                    evidence.get("source_sha"),
                    evidence.get("tested_source_oid"),
                    evidence.get("proof_run_id"),
                    evidence.get("proof_run_attempt"),
                )
                selection_oid = evidence.get("selection_oid")
                selection_operation_id = evidence.get("selection_operation_id")
                if (
                    selected_identity[0] != target
                    or selected_identity[1] != MANAGEMENT_REPOSITORY
                    or not isinstance(selected_identity[2], str)
                    or _REVISION.fullmatch(selected_identity[2]) is None
                    or not isinstance(selected_identity[3], str)
                    or _REVISION.fullmatch(selected_identity[3]) is None
                    or type(selected_identity[4]) is not int
                    or selected_identity[4] <= 0
                    or type(selected_identity[5]) is not int
                    or selected_identity[5] <= 0
                    or not isinstance(selection_oid, str)
                    or _REVISION.fullmatch(selection_oid) is None
                    or not isinstance(selection_operation_id, str)
                    or not selection_operation_id
                ):
                    answer["missing_prerequisites"] = [f"{target}: upgraded-service source/selection identity conflict"]
                    return answer
                if selected is not None and any(selected.get(key) != evidence.get(key) for key in ("source_sha", "selection_oid", "selection_operation_id", "selected_run_id", "selected_run_attempt")):
                    answer["missing_prerequisites"] = ["beta targets disagree on the current selection"]
                    return answer
                selected = evidence
            assert selected is not None
            answer["stage"] = "beta-deploy-prerequisites-verified"
            answer["selected_source_sha"] = selected["source_sha"]
            answer["selection_operation_id"] = selected["selection_operation_id"]
            answer["worker_targets"] = sorted(baseline)
            _record_operation(lane, operation, stage="beta-deploy-prerequisites-verified")
            return answer
        except SourceEvidenceError as error:
            answer["stage"] = "application-ready" if answer.get("stage") == "application-ready" else "needs-attention"
            answer["missing_prerequisites"] = [str(error)[:256]]
            return answer
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError, urllib.error.URLError, TestedSourceError) as error:
            answer["stage"] = "application-ready" if answer.get("stage") == "application-ready" else "needs-attention"
            answer["missing_prerequisites"] = [type(error).__name__]
            return answer


def main() -> int:
    parser = argparse.ArgumentParser(description="Operator-approved immutable management/proxy upgrade; never deploys beta")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_command = commands.add_parser("prepare")
    prepare_command.add_argument("--profile", required=True, type=Path)
    prepare_command.add_argument("--auth-token-file", required=True, type=Path)
    prepare_command.add_argument("--authorize-source-read", action="store_true")
    apply_command = commands.add_parser("apply")
    apply_command.add_argument("--plan-sha256", required=True)
    apply_command.add_argument("--auth-token-file", required=True, type=Path)
    apply_command.add_argument("--authorize-source-read", action="store_true")
    status_command = commands.add_parser("status")
    status_command.add_argument("--plan-sha256", required=True)
    status_command.add_argument("--auth-token-file", type=Path)
    status_command.add_argument("--authorize-source-read", action="store_true")
    args = parser.parse_args()
    audit_enabled = False
    try:
        if OPERATOR_ROOT.exists():
            _protected_root(OPERATOR_ROOT)
            descriptor = os.open(OPERATOR_ROOT / "upgrade-audit.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                os.close(descriptor)
                raise UpgradeUnavailable("prerequisite-blocked", "operator audit sink is not private")
            audit_file = os.fdopen(descriptor, "a", encoding="utf-8")
            logger.remove()
            logger.add(sys.stdout, format="{message}", filter=lambda record: record["extra"].get("operator_result") is True)
            logger.add(audit_file, format="{time:YYYY-MM-DDTHH:mm:ssZ} {level} {message}")
            audit_enabled = True
        if args.command == "prepare":
            sha = prepare(OPERATOR_ROOT, args.profile, args.auth_token_file, authorize_source_read=args.authorize_source_read)
            result: dict[str, object] = {"stage": "prepared", "plan_sha256": sha}
        elif args.command == "apply":
            operation = apply(OPERATOR_ROOT, args.plan_sha256, args.auth_token_file, authorize_source_read=args.authorize_source_read)
            result = {"stage": operation.stage, "operation_id": operation.operation_id, "completed_effects": operation.completed}
        else:
            result = status(OPERATOR_ROOT, args.plan_sha256, authenticated_client_file=args.auth_token_file, authorize_source_read=args.authorize_source_read)
    except UpgradeUnavailable as error:
        result = {"stage": error.stage, "reason": error.reason}
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError, urllib.error.URLError, TestedSourceError, SourceEvidenceError, HostObservationUnavailable) as error:
        logger.warning("Trusted host upgrade unavailable type={}", type(error).__name__)
        result = {"stage": "prerequisite-blocked", "reason": type(error).__name__}
    report = json.dumps(result, sort_keys=True)
    if audit_enabled:
        logger.bind(operator_result=True).info(report)
    else:
        print(report)
    return 0 if result.get("stage") in {"prepared", "application-ready", "beta-deploy-prerequisites-verified", "successor-launch-uncertain"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
