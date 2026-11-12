"""Actual recovery ownership issuance on a separately authorized disposable VM.

The driver uses original producers, the ordinary local Docker engine, systemd,
real namespace and flock custody, authenticated source publication and installed
READY verification. It never supplies an observation verdict or patches a
production function. The target stops at the explicit prelaunch custody mode;
consumer launch/recovery after this grant belongs to the separate consumer issue.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable

from scripts.recovery_epoch_containment_fixture import CapturedGeneration, capture_generation, enroll_recovery_scope, selected_file, start_watcher, target_from_container
from scripts.recovery_epoch_fault_tracer import BOUNDARY_SCENARIOS, PUBLICATIONS, KernelPublicationBarrier, PublicationObservation, parse_boundary
from scripts.recovery_epoch_native_negatives import REQUIRED_NEGATIVE_CASES, REQUIRED_REPORT_CASES, NativeNegativeContext, _replacement, assert_unreserved_epoch, run_native_negatives, verify_native_containment_reports
from scripts.recovery_epoch_native_peer import LATE_MUTATION_SCENARIOS, REQUIRED_PEER_CASES, mutate_after_intent, observe_queued_grant, pause_target_before_grant, run_native_peer_cases
from scripts.test_management_containment_closed_host import InhibitedStockRoutes, WatcherFixture, require_disposable_host
from scripts.test_management_containment_enrollment_host import DisposableSystemdRoutes, _private_directory, _write_private

from auto_coder.logger_config import get_logger, setup_logger
from auto_coder.management.bootstrap_supervisor import BootstrapConfig
from auto_coder.management.bootstrap_upgrade import _source_authority
from auto_coder.management.fresh_initialization import FreshInitializationIntent, claim_fresh_initialization, read_fresh_status
from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.operations import ActionParameter, ManagedTarget, OperationIntent, OperationPhase
from auto_coder.management.original_attempt_evidence import derive_lineage, observe, verify_lineage
from auto_coder.management.original_attempts import OriginalEvidenceUnavailable, canonical, source_identity
from auto_coder.management.recovery_containment_model import RecoveryWatcher
from auto_coder.management.recovery_epoch_authorization import AUTHORIZATION_FILE, AUTHORIZATION_ROOT, AUTHORIZATION_SCHEMA, PEER_ROOT, PEER_SOCKET, EpochAuthorization
from auto_coder.management.recovery_epoch_consumer import GRANT_SCHEMA, ConsumerConfiguration, OriginalFence, TargetOwner, TransferredResource
from auto_coder.management.recovery_epoch_state import EPOCH_ROOT, EpochInventory
from auto_coder.management.self_update_handoff import runtime_for_receipt
from auto_coder.management.version_staging import ManagementPrepareIntent, ManagementReadyReceipt, ManagementRuntimeConfig, ManagementVersionStager
from auto_coder.operating_policy import MANAGEMENT_REPOSITORY
from tests.test_management_archived_cutover_container import Chain, Generation, diagnostic_log_lines

logger = get_logger(__name__)
FIXTURE_ROOT = Path("/var/lib/auto-coder/recovery-epoch-fixtures")
RESULT_SCHEMA = "auto-coder-native-recovery-epoch-result-v1"
ORIGINAL_NEGATIVE_CASES = frozenset({"original-journal-absence-refused", "changed-installed-ready-refused", "changed-target-management-config-refused"})
LINEAGE_REPORT_CASES = frozenset({"forged-original-lineage-refused", "stale-original-lineage-refused", "cross-deployment-original-lineage-refused"})
FACTORY_OBSERVATION_CASES = frozenset(case for case in REQUIRED_NEGATIVE_CASES if case.startswith("factory-"))
TASK_OBSERVATION_CASES = frozenset(case for case in REQUIRED_NEGATIVE_CASES | REQUIRED_REPORT_CASES if "nonleader" in case)
ACCEPTANCE_SCENARIOS = ("grant", "lost-reply", "retarget-after-reservation", *BOUNDARY_SCENARIOS, *LATE_MUTATION_SCENARIOS)
SCENARIOS = (*ACCEPTANCE_SCENARIOS, "kill-after-intent", "kill-after-consume")
REQUIRED_CASES = frozenset(
    {
        "affirmative-original-genesis",
        "native-successor-ready",
        "native-failed-branch-retained",
        "native-unresolved-successor-retained",
        "missing-original-refused",
        "genuine-installed-ready-in-target",
        "independent-issuer-contender-refused",
        "reservation-barrier-contender-refused",
        "complete-native-epoch-granted",
        "old-owner-and-controller-contenders-refused",
        "issuer-death-ended-current-custody",
        "consumed-epoch-replay-refused",
        "originals-oauth-and-workers-preserved",
        "no-consumer-child-or-admission",
    }
)


def required_cases(scenario: str) -> frozenset[str]:
    if scenario not in SCENARIOS:
        raise ValueError("unsupported native recovery fixture scenario")
    if scenario == "grant":
        return REQUIRED_CASES | REQUIRED_NEGATIVE_CASES | REQUIRED_REPORT_CASES | REQUIRED_PEER_CASES | ORIGINAL_NEGATIVE_CASES | LINEAGE_REPORT_CASES
    return (REQUIRED_CASES - {"complete-native-epoch-granted", "old-owner-and-controller-contenders-refused", "issuer-death-ended-current-custody", "consumed-epoch-replay-refused", "reservation-barrier-contender-refused"}) | {"native-" + scenario, "interrupted-epoch-replay-refused"}


def _fixture_directory() -> Path:
    """Select a unique private leaf that fits the actual Linux socket paths."""
    root = FIXTURE_ROOT / uuid.uuid4().hex[:16]
    sockets = (PEER_SOCKET, *(root / "deployment" / name / "guard" / "fresh-guard.sock" for name in ("A", "B", "C", "D")))
    if any(len(os.fsencode(path)) > 107 for path in sockets):
        raise ValueError("the native fixture's original Unix socket pathname exceeds the Linux limit")
    return root


class WorkerFixture(Chain):
    """Reuse the real worker producer in roots unrelated to management."""

    def __init__(self, root: Path, image: str):
        self.root = root
        self.image = image
        self.worker_compose = None
        self.worker_environment = None
        self.worker_baseline = None
        self.start_workers()
        code = "from auto_coder.management.worker_network_client import WorkerNetworkClient,WorkerEndpointConfig;c=WorkerNetworkClient(WorkerEndpointConfig.environment('beta-agent-table-worker'));assert c.inspect_supervisor().result_code=='OK';assert c.observe_worker().result_code=='OK'"
        self.docker("exec", self.worker_manager, "python", "-B", "-c", code)

    def close(self) -> None:
        if self.worker_compose is not None:
            self.compose("down", "--volumes", "--remove-orphans")

    def preserved(self, expected: dict[str, object], posts: dict[str, int], claims: str) -> None:
        assert self.workers() == expected, "an unrelated real worker lifetime changed"
        assert self.effect_posts() == posts, "an unrelated real worker received a new effect POST"
        assert _worker_claims(self.root / "workers") == claims, "an unrelated worker claim changed"

    def effect_posts(self) -> dict[str, int]:
        return {channel: len(path.read_text().splitlines()) if path.exists() else 0 for channel in ("beta", "release", "agent-table") for path in (self.root / "workers" / channel / "fixture-posts",)}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(roots: tuple[Path, ...], *, semantic: bool = False) -> str:
    """Fingerprint protected original bytes/objects without exporting their data."""
    entries = []
    for root in roots:
        for path in (root, *sorted(root.rglob("*"))):
            info = path.lstat()
            if path.is_symlink():
                content = os.readlink(path)
            elif path.is_file():
                content = _digest(path)
            elif path.is_dir():
                content = "directory"
            elif stat.S_ISSOCK(info.st_mode):
                content = "socket"
            else:
                raise AssertionError("unsupported original fixture object")
            if semantic:
                entries.append((str(path), info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_size if path.is_file() else 0, info.st_mtime_ns if path.is_file() else 0, content))
            else:
                entries.append((str(path), info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_size, info.st_mtime_ns, info.st_ctime_ns, content))
    return hashlib.sha256(canonical(entries)).hexdigest()


def _worker_claims(root: Path) -> str:
    # The actual supervisor refreshes its observation timestamp and atomically
    # replaces state.json during a GET. Compare its stable operation/ownership
    # claims, not the expected volatile observation timestamp or file inode.
    claims = []
    for channel in ("beta", "release", "agent-table"):
        path = root / channel / "supervisor" / "state.json"
        state = json.loads(path.read_bytes())
        worker = state.get("worker")
        if state.get("schema_version") != 1 or not isinstance(worker, dict) or not worker.get("process_run_id"):
            raise AssertionError("actual worker native ownership evidence is absent")
        claims.append(
            {
                "path": str(path),
                "supervisor": {name: state.get(name) for name in ("schema_version", "operations", "selected_runtime_id", "launch_intent", "stopped", "stop_verified", "last_worker")},
                "worker": {name: worker.get(name) for name in ("pid", "start_token", "process_run_id", "deployment_generation", "runtime_id", "started_at", "stop_requested", "ready", "ready_path")},
            }
        )
    configurations = tuple(root / name for name in ("compose.yml", "beta-secret", "release-secret", "agent-table-secret"))
    return hashlib.sha256(canonical({"claims": claims, "configuration": [(str(path), path.stat().st_dev, path.stat().st_ino, _digest(path)) for path in configurations]})).hexdigest()


def _wait(predicate: Callable[[], bool], description: str, *, timeout: float = 90, process: subprocess.Popen | None = None) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if process is not None and process.poll() is not None:
            raise AssertionError(description + ": the original process exited with " + str(process.returncode))
        if time.monotonic() >= deadline:
            raise AssertionError(description + ": the bounded native barrier timed out")
        time.sleep(0.05)


def _command_failure(error: BaseException) -> dict[str, object] | None:
    """Retain bounded child stderr/stdout without exporting command inputs."""
    if not isinstance(error, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
        return None
    lines = []
    for value in (error.output, error.stderr):
        if isinstance(value, bytes):
            value = value.decode(errors="replace")
        if isinstance(value, str):
            # The prefix keeps plain Compose diagnostics in the same existing
            # credential/header/body filter used for all exported fixture logs.
            lines.extend(diagnostic_log_lines("\n".join("RuntimeError: " + line for line in value.splitlines()[-60:])))
    return {"type": type(error).__name__, "returncode": getattr(error, "returncode", None), "diagnostics": lines[-60:]}


def _checkpoint_stopped(generation: Generation) -> None:
    """The stopped fixture producer settles its own stores before inspection."""
    for directory in (generation.path("state"), generation.path("journal")):
        for database in directory.glob("*.sqlite3"):
            Chain.checkpoint(database)


def _no_target_child(target: Generation, *, running: bool) -> None:
    assert not set(os.listdir(target.path("state"))) - {".supervisor-owner.lock", "logs"}, "prelaunch target created native consumer state"
    assert not os.listdir(target.path("journal")), "prelaunch target admitted an operation"
    if running:
        state = json.loads(Chain.docker("inspect", "--format", "{{json .State}}", target.container))
        assert state["Running"] is True and state["Pid"] > 1
        pid = state["Pid"]
        assert not Path(f"/proc/{pid}/task/{pid}/children").read_text().strip(), "prelaunch target has a child process"


def _require_target_absent(target_id: str) -> None:
    """Prove exact object absence in the actual complete local inventory."""
    assert re.fullmatch(r"[0-9a-f]{64}", target_id), "the selected target must be an exact Docker ID"
    identifiers = Chain.docker("ps", "--all", "--quiet", "--no-trunc").splitlines()
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in identifiers) and len(set(identifiers)) == len(identifiers), "the complete Docker inventory is malformed"
    assert target_id not in identifiers, "the deliberately removed target was recreated or remains present"


def _native_refused_branch(chain: Chain, prior: Generation) -> Generation:
    candidate = chain.prepare("D", prior)
    assert candidate.intent is not None and candidate.proxy is not None
    selected = FreshInitializationIntent(**json.loads(candidate.intent.read_bytes()))
    bad = replace(selected, oauth_inode=selected.oauth_inode + 1)
    # This is the real original admission producer. Its failure is retained in
    # the prospective history; no native store/owner or host grant is fabricated.
    try:
        claim_fresh_initialization(candidate.path("state"), bad)
    except ValueError as error:
        assert str(error) == "retained OAuth root identity differs"
    else:
        raise AssertionError("the original producer accepted the mismatched OAuth inode")
    assert not os.listdir(candidate.path("state")) and not os.listdir(candidate.path("journal"))
    for identifier in (candidate.container, candidate.proxy):
        state = json.loads(chain.docker("inspect", "--format", "{{json .State}}", identifier))
        assert state["Status"] == "created" and state["Pid"] == 0 and state["StartedAt"].startswith("0001-")
    # Only these never-started physical objects are removed. Their original
    # attempt/terminal boundaries and exact empty roots remain authoritative.
    chain.docker("rm", candidate.proxy, candidate.container)
    return candidate


def _unresolved_successor(chain: Chain, prior: Generation) -> Generation:
    candidate = chain.prepare("C", prior)
    host = chain.host(candidate)
    chain.status(host, "ancestors-fenced-awaiting-manual-stop")
    chain.stop(prior)
    _checkpoint_stopped(prior)
    chain.status(host, "awaiting-replacement")
    chain.docker("start", candidate.container)
    out, err = host.communicate(timeout=90)
    assert host.returncode == 0, (out, err)
    chain.ensure_proxy(candidate)

    def launched() -> bool:
        try:
            return read_fresh_status(candidate.path("state"), host=True)["status"] == "launched" and chain.child(candidate)["phase"] == "running"
        except (OSError, ValueError, sqlite3.Error, TypeError, KeyError):
            return False

    _wait(launched, "actual unresolved first launch")
    # Withholding the authenticated readiness request leaves the genuine
    # native outcome unresolved. No outcome file or SQLite row is rewritten.
    store = ManagementOperationStore(candidate.path("journal"))
    intent = OperationIntent(ManagedTarget(candidate.name, "management", MANAGEMENT_REPOSITORY), "management-service-self-update", (ActionParameter("source_sha", "a" * 40),), "native-retained-unknown")
    admitted = store.admit(intent, chain.authority).record
    assert admitted is not None
    changed = store.transition(admitted.operation_id, 0, OperationPhase.NEEDS_ATTENTION, safe_error_category="UNKNOWN:fixture-effect-not-retried")
    assert changed.record is not None
    return candidate


def _check_lineage(chain: Chain, captures: tuple[CapturedGeneration, ...]) -> dict[str, str]:
    assert chain.history_binding is not None
    with ExitStack() as stack:
        descriptors = []
        for captured in captures:
            assert captured.original.owner is not None
            stream = stack.enter_context(Path(captured.original.owner.path).open("rb"))
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            descriptors.append(stream.fileno())
        history = stack.enter_context(observe(chain.history_binding))
        complete = derive_lineage(chain.history_binding, history, tuple(descriptors))
        assert not complete.unavailable, complete.unavailable
        assert verify_lineage(complete, chain.history_binding, history, tuple(descriptors)) == complete
        try:
            verify_lineage(replace(complete, generations=complete.generations[:-1]), chain.history_binding, history, tuple(descriptors))
        except OriginalEvidenceUnavailable:
            pass
        else:
            raise AssertionError("a truncated original lineage was accepted as complete")
        foreign = replace(chain.history_binding, deployment="foreign-deployment-" + uuid.uuid4().hex, history=str(uuid.uuid4()))
        try:
            verify_lineage(replace(complete, binding=foreign), chain.history_binding, history, tuple(descriptors))
        except OriginalEvidenceUnavailable:
            pass
        else:
            raise AssertionError("a lineage report from another deployment/history was accepted")
        outcomes = {item.instance_id: item.outcome for item in complete.generations}
        assert set(outcomes) == {"A", "B", "C", "D"}
        assert outcomes["B"] == "succeeded" and outcomes["C"] in {"launched", "unknown"} and outcomes["D"] == "never-created"
        # A true missing original is distinct from a captured unknown. Restore
        # only this disposable original pathname before taking the baseline.
        database = chain.generations[0].path("journal") / "management-operations.sqlite3"
        hidden = database.with_name("temporarily-absent-original")
        database.rename(hidden)
        try:
            missing = derive_lineage(chain.history_binding, history, tuple(descriptors))
            assert missing.unavailable and any(database.name in reason for reason in missing.unavailable)
            try:
                verify_lineage(complete, chain.history_binding, history, tuple(descriptors))
            except OriginalEvidenceUnavailable:
                pass
            else:
                raise AssertionError("a saved positive lineage survived a missing original")
        finally:
            hidden.rename(database)
        restored = derive_lineage(chain.history_binding, history, tuple(descriptors))
        assert not restored.unavailable and {item.instance_id: item.outcome for item in restored.generations} == outcomes
        return outcomes


@dataclass(frozen=True)
class PreparedTarget:
    generation: Generation
    receipt: ManagementReadyReceipt
    python: Path
    consumer: Path
    request_id: str
    preparation_id: str
    enrollment_id: str
    published_port: int


def _target(
    chain: Chain,
    captures: tuple[CapturedGeneration, ...],
    watchers: tuple[RecoveryWatcher, ...],
    *,
    authorization_id: str,
    source_repository: Path,
    source_token_file: Path,
    source_sha: str,
    uv: Path,
    uv_version: str,
) -> PreparedTarget:
    root = chain.root / "target"
    root.mkdir(mode=0o700)
    for name in ("state", "journal", "bootstrap"):
        (root / name).mkdir(mode=0o700)
    _write_private(root / "state" / ".supervisor-owner.lock", b"")
    # Preserve the exact original OAuth setup, including its live grant table.
    management = root / "bootstrap" / "management.yaml"
    _write_private(management, (chain.root / "A" / "bootstrap" / "management.yaml").read_bytes())
    versions = root / "versions"
    preparation = "recovery-" + uuid.uuid4().hex
    configuration = ManagementRuntimeConfig("recovery-target", MANAGEMENT_REPOSITORY, source_repository, versions, Path(sys.executable), uv, uv_version, management_config=management, source_authority=_source_authority(source_token_file))
    reservation = ManagementVersionStager(configuration)
    reservation._validate_boundaries()
    reservation._reserve_root()
    environment = dict(os.environ, UV_PYTHON_INSTALL_DIR=str(versions / ".python"), PYTHONDONTWRITEBYTECODE="1")
    subprocess.run([str(uv), "python", "install", "3.12", "--no-bin"], check=True, env=environment, timeout=300)
    python = Path(subprocess.check_output([str(uv), "python", "find", "--managed-python", "--system", "3.12"], env=environment, text=True, timeout=30).strip()).resolve(strict=True)
    assert python.is_relative_to(versions.resolve()), "portable Python escaped the same-path version root"
    staging = ManagementVersionStager(replace(configuration, python_executable=python))
    receipt = staging.prepare(ManagementPrepareIntent(source_sha, preparation)).receipt
    assert staging.lookup(Path(receipt.environment_path).parent / "MANAGEMENT_READY.json") == receipt
    bootstrap = BootstrapConfig(
        "recovery-target",
        Path("/management-child-state"),
        Path("/management-oauth"),
        Path("/management-operations"),
        chain.origin + "/mcp",
        receipt.management_runtime_id,
        (),
        crash_budget=0,
        management_version_root=versions,
        management_config=Path("/bootstrap/management.yaml"),
        verification_timeout=1800,
        recovery_epoch_config=Path("/bootstrap/recovery-epoch.json"),
    )
    runtime = runtime_for_receipt(bootstrap, receipt)
    raw = {
        "instance_id": bootstrap.instance_id,
        "state_root": str(bootstrap.state_root),
        "oauth_root": str(bootstrap.oauth_root),
        "journal_root": str(bootstrap.journal_root),
        "listener_url": bootstrap.listener_url,
        "initial_runtime": receipt.management_runtime_id,
        "runtimes": [{**asdict(runtime), "environment": dict(runtime.environment)}],
        "crash_budget": 0,
        "management_version_root": str(versions),
        "management_config": str(bootstrap.management_config),
        "verification_timeout": 1800,
        "recovery_epoch_config": "/bootstrap/recovery-epoch.json",
    }
    config = root / "bootstrap" / "config.json"
    _write_private(config, json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str).encode())
    BootstrapConfig.read(config)
    consumer = root / "bootstrap" / "recovery-epoch.json"
    _write_private(consumer, b"")
    assert chain.history_binding is not None
    mounts = [
        (root / "state", "/management-child-state", True),
        (root / "journal", "/management-operations", True),
        (chain.oauth, "/management-oauth", True),
        (config, "/bootstrap/config.json", False),
        (management, "/bootstrap/management.yaml", False),
        (consumer, "/bootstrap/recovery-epoch.json", False),
        (versions, str(versions), False),
        (PEER_ROOT, "/management-recovery", False),
        (Path(chain.history_binding.root) / "history.lock", "/management-recovery-history/history.lock", False),
    ]
    originals = tuple(sorted((item.original for item in captures), key=lambda item: item.generation_id))
    original_fences = []
    transfers = []
    for index, original in enumerate(originals):
        assert original.owner is not None and original.namespace_pin is not None
        destination = f"/management-recovery-originals/{index}/owner.lock"
        mounts.append((Path(original.owner.path), destination, False))
        original_fences.append(OriginalFence(f"owner-{index}", destination, original.owner.device, original.owner.inode))
        transfers.append(TransferredResource(f"namespace-{index}", "network-namespace", original.namespace_pin.device, original.namespace_pin.inode))
    history = (Path(chain.history_binding.root) / "history.lock").stat()
    original_fences.insert(0, OriginalFence("history", "/management-recovery-history/history.lock", history.st_dev, history.st_ino))
    for index, watcher in enumerate(sorted(watchers, key=lambda item: item.unit)):
        transfers.append(TransferredResource(f"controller-{index}", "transferred-ex", watcher.lock.device, watcher.lock.inode))
    epoch = (EPOCH_ROOT / "epoch.lock").stat()
    transfers.append(TransferredResource("epoch", "transferred-ex", epoch.st_dev, epoch.st_ino))
    name = "recovery-target-" + uuid.uuid4().hex
    chain.names.append(name)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        published_port = reservation.getsockname()[1]
    if published_port == chain.port:
        raise AssertionError("new target endpoint must differ from every old endpoint")
    command = ["create", "--name", name, "--read-only", "--workdir", "/workspace", "--network", "bridge", "--publish", f"127.0.0.1:{published_port}:8080", "--env", "PYTHONDONTWRITEBYTECODE=1"]
    for source, destination, writable in mounts:
        command.extend(("--mount", f"type=bind,src={source},dst={destination}" + ("" if writable else ",readonly")))
    command.extend(("--entrypoint", "python", chain.image, "-m", "auto_coder.management.bootstrap_supervisor", "--config", "/bootstrap/config.json", "--recovery-epoch"))
    identifier = chain.docker(*command)
    generation = Generation("recovery-target", root, identifier, mounts)
    socket_info, owner = PEER_ROOT.stat(), (root / "state" / ".supervisor-owner.lock").stat()
    request_id = "request-" + uuid.uuid4().hex
    selected = ConsumerConfiguration(1, request_id, authorization_id, identifier, 0, socket_info.st_dev, socket_info.st_ino, tuple(original_fences), TargetOwner("target-owner", owner.st_dev, owner.st_ino), tuple(transfers))
    with consumer.open("wb") as stream:
        stream.write(canonical(asdict(selected)))
        stream.flush()
        os.fsync(stream.fileno())
    return PreparedTarget(generation, receipt, python, consumer, request_id, preparation, "enrollment-" + uuid.uuid4().hex, published_port)


def _install_authorization(chain: Chain, target: PreparedTarget, authorization_id: str, source_repository: Path, source_token_file: Path, source_sha: str, uv: Path, uv_version: str) -> None:
    assert chain.history_binding is not None
    socket_info, root, lock = PEER_ROOT.stat(), EPOCH_ROOT.stat(), (EPOCH_ROOT / "epoch.lock").stat()
    authorization = EpochAuthorization(
        AUTHORIZATION_SCHEMA,
        chain.history_binding.deployment,
        authorization_id,
        target.request_id,
        target.preparation_id,
        target.generation.container,
        target.enrollment_id,
        chain.history_binding,
        selected_file(target.consumer),
        selected_file(Path(target.receipt.environment_path).parent / "MANAGEMENT_READY.json"),
        str(source_repository),
        str(source_token_file),
        str(target.python),
        str(uv),
        uv_version,
        source_sha,
        socket_info.st_dev,
        socket_info.st_ino,
    )
    _write_private(AUTHORIZATION_FILE, canonical(asdict(authorization)))
    inventory = EpochInventory(
        2,
        str(EPOCH_ROOT),
        root.st_dev,
        root.st_ino,
        lock.st_dev,
        lock.st_ino,
        authorization.deployment_id,
        authorization_id,
        (target.enrollment_id,),
        target.request_id,
        target.preparation_id,
        target.generation.container,
        source_identity().removeprefix("sha256:"),
        _digest(target.generation.path("bootstrap") / "config.json"),
        _digest(AUTHORIZATION_FILE),
    )
    _write_private(EPOCH_ROOT / "inventory.json", canonical(asdict(inventory)))


def inspect_durable_result(root: Path, *, issuer_log: str, consumer_log: str) -> dict[str, object]:
    """Check the native successful run's four original records and actual logs."""
    inventory = (root / "inventory.json").read_bytes()
    inventory_sha = hashlib.sha256(inventory).hexdigest()
    binding = None
    previous = ""
    digests = {}
    for sequence, phase in enumerate(("reserved", "intent", "consumed", "receipt")):
        encoded = (root / (phase + ".json")).read_bytes()
        record = json.loads(encoded)
        head_bytes = (root / (phase + "-head.json")).read_bytes()
        head = json.loads(head_bytes)
        assert canonical(record) == encoded and canonical(head) == head_bytes
        assert record["schema_version"] == 1 and record["phase"] == phase and record["sequence"] == sequence
        assert record["previous"] == previous and record["inventory_sha256"] == inventory_sha
        assert head == {"schema_version": 1, "phase": phase, "record_sha256": hashlib.sha256(encoded).hexdigest(), "previous": previous, "inventory_sha256": inventory_sha}
        if binding is None:
            binding = record["binding"]
        assert record["binding"] == binding
        digests[phase] = hashlib.sha256(encoded).hexdigest()
        previous = hashlib.sha256(head_bytes).hexdigest()
    assert binding is not None
    evidence = json.loads(binding["evidence_json"])
    assert hashlib.sha256(binding["evidence_json"].encode()).hexdigest() == binding["evidence_sha256"]
    assert not evidence["lineage"]["unavailable"] and evidence["lineage"]["genesis"] is not None
    assert evidence["inspection"]["unresolved_targets"] >= 1 and evidence["inspection"]["launch_authority"] is False
    assert "One consumed recovery epoch grant send completed" in issuer_log
    assert "Recovery custody acknowledged" in consumer_log
    return {
        "record_sha256": digests,
        "evidence_sha256": binding["evidence_sha256"],
        "inventory_sha256": inventory_sha,
        "historical_keys": evidence["inspection"]["historical_keys"],
        "unresolved_targets": evidence["inspection"]["unresolved_targets"],
        "source_sha": evidence["target"]["receipt"]["source_sha"],
    }


def _observation_cases(value: dict[str, object], name: str, schema: str, expected: frozenset[str]) -> dict[str, dict[str, object]]:
    document = value.get(name)
    assert isinstance(document, dict) and document.get("schema") == schema
    rows = document.get("cases")
    assert isinstance(rows, list) and len(rows) == len(expected) and all(isinstance(item, dict) for item in rows)
    assert {item.get("case") for item in rows} == expected
    return {item["case"]: item for item in rows}


def validate_result(value: dict[str, object]) -> None:
    """The CI oracle requires every native case and rejects partial acceptance."""
    assert value.get("schema") == RESULT_SCHEMA and value.get("status") == "passed"
    scenario = value.get("scenario", "grant")
    assert set(value.get("cases", ())) == required_cases(scenario)
    assert value.get("no_child_started") is True and value.get("preserved_originals") is True and value.get("preserved_workers") is True
    assert value.get("original_outcomes", {}).get("D") == "never-created"
    assert value.get("original_outcomes", {}).get("C") in {"launched", "unknown"}
    assert isinstance(value.get("durable"), dict)
    if scenario == "grant":
        assert set(value["durable"]["record_sha256"]) == {"reserved", "intent", "consumed", "receipt"}
        peer = _observation_cases(value, "peer_observations", "actual-recovery-native-peer-faults-v1", REQUIRED_PEER_CASES)
        for item in peer.values():
            assert item.get("issuer_returncode") == 2 and item.get("epoch_unreserved") is True and item.get("issuer_endpoint_removed_by_original") is True
            assert item.get("consumer_grant_accepted") is False and item.get("no_child_or_admission") is True
            assert item.get("issuer_syscall", {}).get("bytes_observed", 0) > 0
            if item["case"] in {"queued-rights-without-receipt-refused", "received-rights-without-receipt-refused"}:
                assert item.get("receiver_alive_until_issuer_refusal") is True and item.get("issuer_refused_before_receiver_termination") is True
        factory = _observation_cases(value, "factory_override_observations", "actual-recovery-factory-overrides-v1", FACTORY_OBSERVATION_CASES)
        for item in factory.values():
            assert item.get("returncode") == 2 and item.get("refusal")
            assert re.fullmatch(r"[0-9a-f]{64}", item.get("alternate_inputs_sha256", "")) and re.fullmatch(r"[0-9a-f]{64}", item.get("epoch_sha256", ""))
        tasks = _observation_cases(value, "task_observations", "actual-recovery-task-observations-v1", TASK_OBSERVATION_CASES)
        assert tasks["escaped-nonleader-current-containment-refused"]["status"] == "unavailable"
        exited = tasks["measured-exited-nonleader-current-containment-reverified"]
        assert exited["status"] == "observed-current-recovery-containment" and exited["after_exit"] == exited["after_verification"]
        assert exited["after_exit"]["pidfd_readable"] is True and exited["after_exit"]["nonleader_absent"] is True
        assert exited["after_exit"]["leader"]["state"] == "Z" and exited["after_exit"]["leader"]["network_namespace"] is None
    elif scenario in BOUNDARY_SCENARIOS:
        publication, when = parse_boundary(scenario)
        assert value["durable"]["publication"] == publication and value["durable"]["when"] == when
        assert value["durable"]["consumer_grant_acknowledged"] is False
        assert value["durable"]["file_fsync_observed"] is (when == "after") and value["durable"]["directory_fsync_observed"] is (when == "after")
    elif scenario == "retarget-after-reservation":
        assert value["durable"]["phase"] == "reserved" and value["durable"]["consumer_grant_acknowledged"] is False
        assert value["durable"]["inventory_sha256"] != value["durable"]["retargeted_inventory_sha256"]
    elif scenario in LATE_MUTATION_SCENARIOS:
        assert value["durable"]["phase"] == "intent" and value["durable"]["consumer_grant_acknowledged"] is False and value["durable"]["grant_send_completed"] is False
        assert value["durable"]["mutation"]["scenario"] == scenario and value["durable"]["mutation"]["boundary"] == "after-intent-head"
        assert value.get("preservation_baseline") == "after-deliberate-fixture-mutation"
        assert value["durable"]["mutation"]["preservation_baseline"] == value["preservation_baseline"]
        assert value["preserved_originals_sha256"] == value["durable"]["originals_after_injection_sha256"]
        if scenario == "late-target-loss":
            target_fault = value["durable"]["mutation"]["target_fault"]
            assert target_fault["pidfd_death_observed"] is True and target_fault["exact_docker_id_absent"] is True and target_fault["docker_rm_returncode"] == 0
            assert target_fault["other_docker_ids_preserved"] is True
        elif scenario == "late-original-attempt":
            attempt = value["durable"]["mutation"]["original_attempt"]
            changed = value["durable"]["mutation"]["file_fault"]
            assert (attempt["instance_id"], attempt["outcome"], attempt["terminal_kind"]) == ("D", "never-created", "never-created")
            assert attempt["independent_lineage_verified"] is True and attempt["retained_intent_lineage_matched"] is True
            assert attempt["original_file"]["path"] == changed["path"] and attempt["original_file"]["sha256"] == changed["before"][-1]
            assert (attempt["original_file"]["device"], attempt["original_file"]["inode"]) == tuple(changed["before"][:2])
    else:
        assert value["durable"]["phase"] == ("intent" if scenario == "kill-after-intent" else "consumed" if scenario == "kill-after-consume" else "receipt")
        assert value["durable"]["consumer_grant_acknowledged"] is False
        assert value["durable"]["grant_send_completed"] is (scenario == "lost-reply")
        if scenario == "lost-reply":
            assert value["durable"]["receiver_paused_before_send"] is True and value["durable"]["issuer_died_before_receiver_resumed"] is True
            original = value["durable"]["native_observation"]
            queued = value["durable"]["queued_grant"]
            assert queued["schema"] == "actual-recovery-queued-grant-v1" and queued["grant_schema"] == GRANT_SCHEMA
            assert all(queued.get(name) is True for name in ("peek_only", "socket_flags_unchanged", "bytes_still_queued", "receiver_stopped", "issuer_alive"))
            assert queued["bytes_observed"] > 0 and re.fullmatch(r"[0-9a-f]{64}", queued["message_sha256"])
            packet = queued["grant"]
            encoded = canonical(packet) + b"\n"
            assert len(encoded) == queued["bytes_observed"] and hashlib.sha256(encoded).hexdigest() == queued["message_sha256"]
            assert (packet["schema"], packet["target_id"], packet["host_pid"], packet["host_start_token"]) == (queued["grant_schema"], queued["target_id"], original["issuer_pid"], original["issuer_start_token"])
            assert queued["socket_inode"] > 0 and queued["socket_flags_before"] == queued["socket_flags_after"]
            assert all(queued[name] == original[name] for name in ("target_pid", "target_start_token", "issuer_pid", "issuer_start_token"))
            assert queued["peer_credentials"] == {"pid": original["issuer_pid"], "uid": 0, "gid": 0}
            assert original["issuer_death_verified_before_receiver_resumed"] is True


def _selected_boundary(scenario: str) -> tuple[str, str]:
    if scenario in BOUNDARY_SCENARIOS:
        return parse_boundary(scenario)
    if scenario in LATE_MUTATION_SCENARIOS or scenario == "kill-after-intent":
        return "intent-head.json", "after"
    if scenario == "kill-after-consume":
        return "consumed-head.json", "after"
    if scenario == "lost-reply":
        return "receipt-head.json", "after"
    if scenario in {"grant", "retarget-after-reservation"}:
        return "reserved-head.json", "after"
    raise ValueError("unsupported native recovery publication barrier")


def _lost_reply(context: NativeNegativeContext, barrier: KernelPublicationBarrier, issuer: subprocess.Popen, issuer_log: Path, consumer_log: Path, target: PreparedTarget) -> dict[str, object]:
    """Observe successful delivery to the socket before the pinned issuer dies."""
    original_epoch = _snapshot((EPOCH_ROOT,))
    descriptor = os.pidfd_open(issuer.pid, 0)
    try:
        with pause_target_before_grant(context, issuer.pid) as observation:
            assert "Recovery custody acknowledged" not in consumer_log.read_text()
            barrier.release()
            _wait(lambda: "One consumed recovery epoch grant send completed" in issuer_log.read_text(), "actual grant send while its receiver remains kernel-stopped", timeout=1800, process=issuer)
            assert "Recovery custody acknowledged" not in consumer_log.read_text(), "the stopped receiver observed the grant"
            assert _snapshot((EPOCH_ROOT,)) == original_epoch
            queued = observe_queued_grant(context, issuer.pid, target_pid=observation["target_pid"], target_start_token=observation["target_start_token"], issuer_start_token=observation["issuer_start_token"])
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            assert issuer.wait(timeout=30) == -signal.SIGKILL
            assert "Recovery custody acknowledged" not in consumer_log.read_text()
    finally:
        os.close(descriptor)
    _wait(lambda: Chain.docker("inspect", "--format", "{{.State.Running}}", target.generation.container) == "false", "queued grant cannot survive the actual issuer lifetime", timeout=120)
    assert Chain.docker("inspect", "--format", "{{.State.ExitCode}}", target.generation.container) == "1"
    _no_target_child(target.generation, running=False)
    assert "Recovery custody acknowledged" not in consumer_log.read_text()
    assert _snapshot((EPOCH_ROOT,)) == original_epoch
    replay = subprocess.run([sys.executable, "-B", "-m", "auto_coder.management.recovery_epoch"], capture_output=True, text=True, timeout=30)
    assert replay.returncode == 2 and _snapshot((EPOCH_ROOT,)) == original_epoch, "a consumed epoch was reissued after its reply was lost"
    return {"phase": "receipt", "consumer_grant_acknowledged": False, "grant_send_completed": True, "receiver_paused_before_send": True, "issuer_died_before_receiver_resumed": True, "native_observation": observation, "queued_grant": queued, "epoch_sha256": original_epoch}


def _publication_interruption(scenario: str, observed: PublicationObservation, issuer_log: Path, consumer_log: Path, target: PreparedTarget) -> dict[str, object]:
    """Verify exactly the actual interrupted original publication prefix."""
    name, when = parse_boundary(scenario)
    assert (observed.name, observed.when) == (name, when)
    index = PUBLICATIONS.index(name) + (1 if when == "after" else 0)
    assert set(os.listdir(EPOCH_ROOT)) == {"inventory.json", "epoch.lock", *PUBLICATIONS[:index]}, "issuer crossed the selected native publication boundary"
    if when == "after":
        published = EPOCH_ROOT / name
        info = published.stat()
        assert (observed.file_device, observed.file_inode, observed.file_sha256) == (info.st_dev, info.st_ino, _digest(published))
        assert observed.file_fsync_observed and observed.directory_fsync_observed
    else:
        assert observed.file_device is None and observed.file_inode is None and observed.file_sha256 is None
        assert not observed.file_fsync_observed and not observed.directory_fsync_observed
    _wait(lambda: Chain.docker("inspect", "--format", "{{.State.Running}}", target.generation.container) == "false", "native publication interruption ends target custody", timeout=90)
    _no_target_child(target.generation, running=False)
    assert "Recovery custody acknowledged" not in consumer_log.read_text()
    assert "One consumed recovery epoch grant send completed" not in issuer_log.read_text()
    before = _snapshot((EPOCH_ROOT,))
    replay = subprocess.run([sys.executable, "-B", "-m", "auto_coder.management.recovery_epoch"], capture_output=True, text=True, timeout=30)
    assert replay.returncode == 2 and _snapshot((EPOCH_ROOT,)) == before, "interrupted publication or original endpoint was repaired/reissued"
    return {
        "publication": name,
        "when": when,
        "file_fsync_observed": observed.file_fsync_observed,
        "directory_fsync_observed": observed.directory_fsync_observed,
        "consumer_grant_acknowledged": False,
        "grant_send_completed": False,
        "published_names": list(PUBLICATIONS[:index]),
        "native_observation": asdict(observed),
    }


def _passed(result: dict[str, object], cases: list[str], preserved: str, worker_claims: str, worker_baseline: dict[str, object], worker_posts: dict[str, int]) -> None:
    cases.extend(("originals-oauth-and-workers-preserved", "no-consumer-child-or-admission"))
    result.update(status="passed", no_child_started=True, preserved_originals=True, preserved_workers=True, preserved_originals_sha256=preserved, preserved_worker_claims_sha256=worker_claims, worker_lifetimes=worker_baseline, worker_effect_posts=worker_posts)
    validate_result(result)
    logger.info("All {} native recovery ownership cases passed; no consumer child was launched", len(cases))


def _issuer_refusal(context: NativeNegativeContext, target: PreparedTarget, label: str) -> None:
    """Run the actual complete issuer, allowing no first publication on error."""
    assert_unreserved_epoch(context.epoch_root)
    log_path = context.fixture_root / ("refusal-" + label + ".log")
    assert Chain.docker("inspect", "--format", "{{.State.Running}}", target.generation.container) == "false"
    process = None
    started = False
    with log_path.open("xb") as output:
        try:
            process = subprocess.Popen([sys.executable, "-B", "-m", "auto_coder.management.recovery_epoch"], stdout=output, stderr=subprocess.STDOUT, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
            _wait(lambda: process.poll() is not None or PEER_SOCKET.exists() and "Fixed recovery issuer endpoint is listening" in log_path.read_text(), "actual refused issuer startup " + label, timeout=180)
            if process.poll() is None:
                Chain.docker("start", target.generation.container)
                started = True
                _wait(lambda: process.poll() is not None or set(os.listdir(context.epoch_root)) != {"inventory.json", "epoch.lock"}, "actual prepublication refusal " + label, timeout=1800)
            assert_unreserved_epoch(context.epoch_root)
            assert process.wait(timeout=30) == 2, "the complete issuer did not refuse the injected original condition"
            assert "One consumed recovery epoch grant send completed" not in log_path.read_text()
            if started:
                _wait(lambda: Chain.docker("inspect", "--format", "{{.State.Running}}", target.generation.container) == "false", "refused target exits without native state", timeout=120)
                assert Chain.docker("inspect", "--format", "{{.State.ExitCode}}", target.generation.container) == "1"
                consumer_log = target.generation.path("state") / "logs" / "recovery-epoch-bootstrap.log"
                assert "Recovery custody acknowledged" not in consumer_log.read_text()
            _no_target_child(target.generation, running=False)
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=30)
            if started and Chain.docker("inspect", "--format", "{{.State.Running}}", target.generation.container) == "true":
                Chain.docker("kill", target.generation.container)
            assert_unreserved_epoch(context.epoch_root)


def _original_negatives(context: NativeNegativeContext, target: PreparedTarget, chain: Chain, preserved_snapshot: Callable[[], object]) -> list[str]:
    directory = context.fixture_root / "native-original-negative-backups"
    directory.mkdir(mode=0o700)
    database = chain.generations[0].path("journal") / "management-operations.sqlite3"
    saved = directory / "absent-original.sqlite3"
    baseline = preserved_snapshot()
    original = (database.stat().st_dev, database.stat().st_ino, _digest(database))
    database.rename(saved)
    try:
        injected = preserved_snapshot()
        _issuer_refusal(context, target, "original-journal-absence-refused")
        assert preserved_snapshot() == injected
    finally:
        assert_unreserved_epoch(context.epoch_root)
        assert not database.exists() and (saved.stat().st_dev, saved.stat().st_ino, _digest(saved)) == original
        saved.rename(database)
    assert preserved_snapshot() == baseline
    for label, path in (("changed-installed-ready-refused", Path(target.receipt.environment_path).parent / "MANAGEMENT_READY.json"), ("changed-target-management-config-refused", target.generation.path("bootstrap") / "management.yaml")):
        baseline = preserved_snapshot()
        with _replacement(context.epoch_root, path, directory / label, path.read_bytes() + b"\n"):
            injected = preserved_snapshot()
            _issuer_refusal(context, target, label)
            assert preserved_snapshot() == injected
        assert preserved_snapshot() == baseline
    return sorted(ORIGINAL_NEGATIVE_CASES)


def _reserved_contender() -> None:
    assert set(os.listdir(EPOCH_ROOT)) == {"inventory.json", "epoch.lock", "reserved.json", "reserved-head.json"}
    before = _snapshot((EPOCH_ROOT,))
    contender = subprocess.run([sys.executable, "-B", "-m", "auto_coder.management.recovery_epoch"], capture_output=True, text=True, timeout=30)
    assert contender.returncode == 2 and _snapshot((EPOCH_ROOT,)) == before, "a contender overlapped the actual original durable reservation"


def _retarget_reserved(barrier: KernelPublicationBarrier, issuer: subprocess.Popen, issuer_log: Path, consumer_log: Path, target: PreparedTarget) -> dict[str, object]:
    inventory = EPOCH_ROOT / "inventory.json"
    original_digest = _digest(inventory)
    raw = json.loads(inventory.read_bytes())
    raw["preparation_id"] = "foreign-" + uuid.uuid4().hex
    raw["target_id"] = ("f" if raw["target_id"] != "f" * 64 else "e") * 64
    with inventory.open("wb") as stream:
        stream.write(canonical(raw))
        stream.flush()
        os.fsync(stream.fileno())
    _reserved_contender()
    injected = _snapshot((EPOCH_ROOT,))
    barrier.release()
    issuer.wait(timeout=120)
    assert issuer.returncode == 2 and _snapshot((EPOCH_ROOT,)) == injected
    _wait(lambda: Chain.docker("inspect", "--format", "{{.State.Running}}", target.generation.container) == "false", "retargeted reservation ends current custody")
    _no_target_child(target.generation, running=False)
    assert "Recovery custody acknowledged" not in consumer_log.read_text() and "One consumed recovery epoch grant send completed" not in issuer_log.read_text()
    replay = subprocess.run([sys.executable, "-B", "-m", "auto_coder.management.recovery_epoch"], capture_output=True, text=True, timeout=30)
    assert replay.returncode == 2 and _snapshot((EPOCH_ROOT,)) == injected
    return {"phase": "reserved", "consumer_grant_acknowledged": False, "inventory_sha256": original_digest, "retargeted_inventory_sha256": _digest(inventory), "retained_publications": ["reserved.json", "reserved-head.json"]}


def _late_mutation(
    context: NativeNegativeContext,
    scenario: str,
    barrier: KernelPublicationBarrier,
    issuer: subprocess.Popen,
    issuer_log: Path,
    consumer_log: Path,
    target: PreparedTarget,
    retained_roots: tuple[Path, ...],
    check_workers: Callable[[], None],
) -> tuple[dict[str, object], str]:
    """Require an actual late refusal while retaining the injected uncertainty."""
    original = _snapshot(retained_roots)
    with mutate_after_intent(context, scenario, issuer.pid) as observation:
        injected = _snapshot(retained_roots)
        epoch = _snapshot((EPOCH_ROOT,))
        check_workers()
        barrier.release()
        assert issuer.wait(timeout=1800) == 2, "the actual issuer did not refuse the changed post-intent authority"
        if scenario == "late-target-loss":
            _require_target_absent(target.generation.container)
        else:
            _wait(lambda: Chain.docker("inspect", "--format", "{{.State.Running}}", target.generation.container) == "false", "late refusal ends the exact current consumer lifetime", timeout=120)
            assert Chain.docker("inspect", "--format", "{{.State.ExitCode}}", target.generation.container) == "1"
        _no_target_child(target.generation, running=False)
        assert "Recovery custody acknowledged" not in consumer_log.read_text() and "One consumed recovery epoch grant send completed" not in issuer_log.read_text()
        assert _snapshot((EPOCH_ROOT,)) == epoch, "late mutation reached consumption or rewrote original epoch evidence"
        assert _snapshot(retained_roots) == injected, "production refusal changed the deliberately injected original state"
        check_workers()
        replay = subprocess.run([sys.executable, "-B", "-m", "auto_coder.management.recovery_epoch"], capture_output=True, text=True, timeout=30)
        assert replay.returncode == 2 and _snapshot((EPOCH_ROOT,)) == epoch, "the uncertain original intent was repaired or reissued"
        if scenario == "late-target-loss":
            _require_target_absent(target.generation.container)
        assert _snapshot(retained_roots) == injected
        check_workers()
        result = {
            "phase": "intent",
            "consumer_grant_acknowledged": False,
            "grant_send_completed": False,
            "mutation": observation,
            "originals_before_injection_sha256": original,
            "originals_after_injection_sha256": injected,
            "epoch_sha256": epoch,
        }
    return result, injected


def run_fixture(*, authorization_id: str, source_repository: Path, source_token_file: Path, source_sha: str, uv: Path, uv_version: str, output: Path, scenario: str = "grant") -> dict[str, object]:
    required_cases(scenario)
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", uv_version):
        raise ValueError("an exact qualified source and installer version are required")
    if any(not path.is_absolute() or path != path.resolve(strict=True) for path in (source_repository, source_token_file, uv)) or not output.is_absolute():
        raise ValueError("canonical absolute native fixture inputs are required")
    for name in ("AUTOCODER_FRESH_TEST_BASE_IMAGE", "AUTOCODER_RECOVERY_CADDY_IMAGE"):
        if re.fullmatch(r"sha256:[0-9a-f]{64}", os.environ.get(name, "")) is None:
            raise ValueError("an exact already imported fixture image is required: " + name)
    authorization = require_disposable_host(authorization_id)
    root = _fixture_directory()
    # No resource is provisioned until the original marker selected this VM.
    os.umask(0o077)
    for directory in (FIXTURE_ROOT, EPOCH_ROOT, PEER_ROOT, AUTHORIZATION_ROOT):
        if directory.exists() or directory.is_symlink():
            raise FileExistsError("recovery fixture refuses a previous/partial root: " + str(directory))
    for parent in (Path("/var/lib/auto-coder"), Path("/etc/auto-coder")):
        _private_directory(parent)
    _private_directory(FIXTURE_ROOT)
    root.mkdir(mode=0o700)
    for directory in (EPOCH_ROOT, PEER_ROOT, AUTHORIZATION_ROOT):
        _private_directory(directory)
    _write_private(EPOCH_ROOT / "epoch.lock", b"")
    setup_logger(stream=sys.stdout, log_file=str(root / "fixture.log"))
    cases: list[str] = []
    result: dict[str, object] = {"schema": RESULT_SCHEMA, "status": "failed", "scenario": scenario, "cases": cases, "authorization_id": authorization_id, "source_identity": source_identity(), "no_child_started": False, "preserved_originals": False, "preserved_workers": False}
    chain = None
    workers = None
    stock = None
    routes = None
    watcher_fixtures: list[WatcherFixture] = []
    captures: list[CapturedGeneration] = []
    issuer = None
    log_stream = None
    target = None
    barrier = None
    try:
        stock = InhibitedStockRoutes.install()
        worker_root = root / "unrelated"
        worker_root.mkdir(mode=0o700)
        workers = WorkerFixture(worker_root, os.environ["AUTOCODER_FRESH_TEST_BASE_IMAGE"])
        worker_baseline, worker_posts = workers.workers(), workers.effect_posts()
        worker_claims = _worker_claims(worker_root / "workers")
        deployment_root = root / "deployment"
        deployment_root.mkdir(mode=0o700)
        chain = Chain(deployment_root, original_genesis=True)
        original = chain.prepare("A")
        assert chain.genesis is not None and chain.history_binding is not None
        cases.append("affirmative-original-genesis")
        chain.docker("start", original.container)
        _wait(lambda: (original.path("state") / "original-genesis-first-launch.json").is_file(), "actual original first-launch boundary")
        chain.authorize()
        chain.ready(original, fresh=False)
        chain.prove_child(original)
        chain.seed(original)
        captures.append(capture_generation(chain, original))
        fixture, watcher = start_watcher(original, captures[-1].original, first=True)
        watcher_fixtures.append(fixture)
        watchers = [watcher]
        second = chain.prepare("B", original)
        chain.start_cutover(original, second)
        _checkpoint_stopped(original)
        chain.seed(second)
        captures.append(capture_generation(chain, second))
        fixture, watcher = start_watcher(second, captures[-1].original)
        watcher_fixtures.append(fixture)
        watchers.append(watcher)
        cases.append("native-successor-ready")
        _native_refused_branch(chain, second)
        cases.append("native-failed-branch-retained")
        third = _unresolved_successor(chain, second)
        captures.append(capture_generation(chain, third))
        fixture, watcher = start_watcher(third, captures[-1].original)
        watcher_fixtures.append(fixture)
        watchers.append(watcher)
        chain.stop(third)
        _checkpoint_stopped(third)
        for captured in captures:
            assert captured.original.namespace_pin is not None
            assert os.readlink(f"/proc/self/fd/{captured.namespace_fd}") == captured.original.namespace_pin.namespace
        result["original_outcomes"] = _check_lineage(chain, tuple(captures))
        cases.extend(("native-unresolved-successor-retained", "missing-original-refused"))
        target = _target(chain, tuple(captures), tuple(watchers), authorization_id=authorization_id, source_repository=source_repository, source_token_file=source_token_file, source_sha=source_sha, uv=uv, uv_version=uv_version)
        routes = DisposableSystemdRoutes.install(fixture_id="ac-test-pr2829-" + uuid.uuid4().hex, manager_id=third.container)
        routes.assert_inhibited()
        stock.assert_inhibited()
        selected_target = target_from_container(target.generation.container, bootstrap_config=target.generation.path("bootstrap") / "config.json", management_config=target.generation.path("bootstrap") / "management.yaml", published_port=target.published_port)
        enrollment = enroll_recovery_scope(tuple(captures), selected_target, tuple(watchers), deployment_id=chain.history_binding.deployment, enrollment_id=target.enrollment_id, authorization=authorization)
        _install_authorization(chain, target, authorization_id, source_repository, source_token_file, source_sha, uv, uv_version)
        retained_roots = (Path(chain.history_binding.root), chain.oauth, *(path for generation in chain.generations for path in (generation.path("state"), generation.path("journal"), generation.path("bootstrap"))))
        Chain.checkpoint(chain.oauth / "oauth.sqlite3")
        preserved = _snapshot(retained_roots)
        worker_before = (worker_baseline, worker_posts, worker_claims)
        workers.preserved(*worker_before)
        native_context = NativeNegativeContext(enrollment.enrollment, root, EPOCH_ROOT)
        if scenario == "grant":

            def preservation() -> object:
                workers.preserved(*worker_before)
                return _snapshot(retained_roots, semantic=True), worker_claims, workers.effect_posts()

            cases.extend(LINEAGE_REPORT_CASES)
            cases.extend(run_native_negatives(native_context, lambda label: _issuer_refusal(native_context, target, label), preservation))
            result["factory_override_observations"] = json.loads((root / "native-factory-override-observations.json").read_bytes())
            cases.extend(_original_negatives(native_context, target, chain, preservation))
            cases.extend(run_native_peer_cases(native_context, preservation))
            result["peer_observations"] = json.loads((root / "native-peer-observations.json").read_bytes())
            # Only the intentional reversible fixture injections changed path
            # metadata. This fresh strict baseline precedes actual issuance.
            preserved = _snapshot(retained_roots)
        issuer_log = root / "issuer.log"
        log_stream = issuer_log.open("xb")
        issuer = subprocess.Popen([sys.executable, "-B", "-m", "auto_coder.management.recovery_epoch"], stdout=log_stream, stderr=subprocess.STDOUT, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        _wait(lambda: PEER_SOCKET.exists() and "Fixed recovery issuer endpoint is listening" in issuer_log.read_text(), "actual issuer listening", process=issuer)
        contender = subprocess.run([sys.executable, "-B", "-m", "auto_coder.management.recovery_epoch"], capture_output=True, text=True, timeout=30)
        assert contender.returncode == 2 and not (EPOCH_ROOT / "reserved.json").exists(), "a second issuer acquired the same current epoch"
        cases.append("independent-issuer-contender-refused")
        # The actual ptrace stop lets the target finish its interpreter probe
        # before T samples current tasks, then observes the chosen publication.
        publication, when = _selected_boundary(scenario)
        barrier = KernelPublicationBarrier(issuer, root=EPOCH_ROOT, name=publication, when=when)
        barrier.start()
        chain.docker("start", target.generation.container)
        probe = json.loads(chain.docker("exec", target.generation.container, target.receipt.interpreter_path, "-I", "-B", "-m", "auto_coder.build_provenance", timeout=60).splitlines()[-1])
        assert probe["source_revision"]["available"] is True and probe["source_revision"]["value"] == source_sha
        assert probe["distribution_version"]["available"] is True and probe["distribution_version"]["value"] == target.receipt.distribution_version
        _wait(lambda: chain.docker("inspect", "--format", "{{json .ExecIDs}}", target.generation.container) in ("null", "[]"), "exact target probe has left no active Docker exec")
        _no_target_child(target.generation, running=True)
        cases.append("genuine-installed-ready-in-target")
        if scenario == "grant":
            cases.extend(verify_native_containment_reports(native_context, issuer.pid))
            result["task_observations"] = json.loads((root / "native-task-observations.json").read_bytes())
            assert _snapshot(retained_roots) == preserved
            workers.preserved(*worker_before)
        consumer_log = target.generation.path("state") / "logs" / "recovery-epoch-bootstrap.log"
        result["source_proof"] = {name: getattr(target.receipt, name) for name in ("source_sha", "tested_source_ref", "tested_source_object", "test_run_id", "test_run_attempt", "lock_sha256", "distribution_version", "receipt_identity")}
        barrier.resume_initial()
        observed_boundary = barrier.wait(timeout=1800)
        if scenario == "grant":
            _reserved_contender()
            cases.append("reservation-barrier-contender-refused")
            result["reservation_barrier"] = asdict(observed_boundary)
            barrier.release()
        else:
            if scenario == "retarget-after-reservation":
                result["durable"] = _retarget_reserved(barrier, issuer, issuer_log, consumer_log, target)
            elif scenario == "lost-reply":
                result["durable"] = _lost_reply(native_context, barrier, issuer, issuer_log, consumer_log, target)
            elif scenario in LATE_MUTATION_SCENARIOS:
                result["durable"], preserved = _late_mutation(native_context, scenario, barrier, issuer, issuer_log, consumer_log, target, retained_roots, lambda: workers.preserved(*worker_before))
                result["preservation_baseline"] = "after-deliberate-fixture-mutation"
            else:
                barrier.kill()
                actual = scenario if scenario in BOUNDARY_SCENARIOS else "after-" + publication.removesuffix(".json")
                result["durable"] = _publication_interruption(actual, observed_boundary, issuer_log, consumer_log, target)
                if scenario in {"kill-after-intent", "kill-after-consume"}:
                    result["durable"]["phase"] = "intent" if scenario == "kill-after-intent" else "consumed"
            cases.extend(("native-" + scenario, "interrupted-epoch-replay-refused"))
            assert _snapshot(retained_roots) == preserved
            workers.preserved(*worker_before)
            _passed(result, cases, preserved, worker_claims, worker_baseline, worker_posts)
            return result
        _wait(lambda: (EPOCH_ROOT / "receipt-head.json").is_file() and consumer_log.is_file() and "Recovery custody acknowledged" in consumer_log.read_text(), "native grant and consumer custody", timeout=1800, process=issuer)
        result["durable"] = inspect_durable_result(EPOCH_ROOT, issuer_log=issuer_log.read_text(), consumer_log=consumer_log.read_text())
        cases.append("complete-native-epoch-granted")
        for captured in captures:
            assert captured.original.owner is not None
            with Path(captured.original.owner.path).open("rb") as stream:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    raise AssertionError("old original owner was not excluded by actual SH custody")
        for watcher in watchers:
            with Path(watcher.lock.path).open("rb") as stream:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    raise AssertionError("actual watcher EX custody was released")
        cases.append("old-owner-and-controller-contenders-refused")
        _no_target_child(target.generation, running=True)
        assert _snapshot(retained_roots) == preserved
        workers.preserved(*worker_before)
        durable_before = _snapshot((EPOCH_ROOT,))
        issuer.kill()
        issuer.wait(timeout=30)
        _wait(lambda: chain.docker("inspect", "--format", "{{.State.Running}}", target.generation.container) == "false", "actual issuer death invalidates consumer custody")
        assert chain.docker("inspect", "--format", "{{.State.ExitCode}}", target.generation.container) == "1"
        _no_target_child(target.generation, running=False)
        cases.append("issuer-death-ended-current-custody")
        replay = subprocess.run([sys.executable, "-B", "-m", "auto_coder.management.recovery_epoch"], capture_output=True, text=True, timeout=30)
        assert replay.returncode == 2 and _snapshot((EPOCH_ROOT,)) == durable_before, "a consumed epoch was reissued or repaired"
        cases.append("consumed-epoch-replay-refused")
        assert _snapshot(retained_roots) == preserved
        workers.preserved(*worker_before)
        _passed(result, cases, preserved, worker_claims, worker_baseline, worker_posts)
        return result
    except BaseException as error:
        result["status"] = "failed"
        result["failure_type"] = type(error).__name__
        command_failure = _command_failure(error)
        if command_failure is not None:
            result["command_failure"] = command_failure
            for diagnostic in command_failure["diagnostics"]:
                logger.error("Native fixture command diagnostic: {}", diagnostic)
        logger.exception("Native recovery ownership fixture failed")
        raise
    finally:
        diagnostics = []
        for path in (root / "issuer.log", root / "fixture.log"):
            if path.is_file():
                diagnostics.extend(diagnostic_log_lines(path.read_text(errors="replace")))
        if target is not None:
            path = target.generation.path("state") / "logs" / "recovery-epoch-bootstrap.log"
            if path.is_file():
                diagnostics.extend(diagnostic_log_lines(path.read_text(errors="replace")))
        result["diagnostics"] = diagnostics[-120:]
        output.parent.mkdir(parents=True, exist_ok=True)
        _write_private(output, canonical(result))
        if barrier is not None:
            barrier.close()
        if issuer is not None and issuer.poll() is None:
            issuer.send_signal(signal.SIGCONT)
            issuer.terminate()
            try:
                issuer.wait(timeout=15)
            except subprocess.TimeoutExpired:
                issuer.kill()
                issuer.wait(timeout=15)
        if log_stream is not None:
            log_stream.close()
        if chain is not None:
            chain.close()
        for captured in reversed(captures):
            captured.close()
        for watcher_fixture in reversed(watcher_fixtures):
            watcher_fixture.cleanup()
        if routes is not None:
            routes.cleanup()
        if stock is not None:
            stock.cleanup()
        if workers is not None:
            workers.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--source-repository", type=Path, required=True)
    parser.add_argument("--source-token-file", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--uv-version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenario", choices=SCENARIOS, default="grant")
    args = parser.parse_args(argv)
    run_fixture(**vars(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
