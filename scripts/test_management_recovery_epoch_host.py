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
from auto_coder.management.recovery_epoch_consumer import ConsumerConfiguration, OriginalFence, TargetOwner, TransferredResource
from auto_coder.management.recovery_epoch_state import EPOCH_ROOT, EpochInventory
from auto_coder.management.self_update_handoff import runtime_for_receipt
from auto_coder.management.version_staging import ManagementPrepareIntent, ManagementReadyReceipt, ManagementRuntimeConfig, ManagementVersionStager
from auto_coder.operating_policy import MANAGEMENT_REPOSITORY
from tests.test_management_archived_cutover_container import Chain, Generation, diagnostic_log_lines

logger = get_logger(__name__)
FIXTURE_ROOT = Path("/var/lib/auto-coder/recovery-epoch-fixtures")
RESULT_SCHEMA = "auto-coder-native-recovery-epoch-result-v1"
SCENARIOS = ("grant", "kill-after-intent", "kill-after-consume", "lost-reply")
REQUIRED_CASES = frozenset(
    {
        "affirmative-original-genesis",
        "native-successor-ready",
        "native-failed-branch-retained",
        "native-unresolved-successor-retained",
        "missing-original-refused",
        "genuine-installed-ready-in-target",
        "independent-issuer-contender-refused",
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
        return REQUIRED_CASES
    return (REQUIRED_CASES - {"complete-native-epoch-granted", "old-owner-and-controller-contenders-refused", "issuer-death-ended-current-custody", "consumed-epoch-replay-refused"}) | {"native-" + scenario, "interrupted-epoch-replay-refused"}


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


def _snapshot(roots: tuple[Path, ...]) -> str:
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
        verification_timeout=600,
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
        "verification_timeout": 600,
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
    command = ["create", "--name", name, "--read-only", "--network", "bridge", "--publish", f"127.0.0.1:{published_port}:8080", "--env", "PYTHONDONTWRITEBYTECODE=1"]
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
    else:
        assert value["durable"]["phase"] == ("intent" if scenario == "kill-after-intent" else "consumed" if scenario == "kill-after-consume" else "receipt")
        assert value["durable"]["consumer_grant_acknowledged"] is False


def _interrupted_issuer(scenario: str, issuer: subprocess.Popen, issuer_log: Path, consumer_log: Path, target: PreparedTarget) -> dict[str, object]:
    """Signal real post-fsync publications; reject a race beyond the boundary."""
    phase = "intent" if scenario == "kill-after-intent" else "consumed" if scenario == "kill-after-consume" else "receipt"
    selected_pid = issuer.pid if scenario != "lost-reply" else int(Chain.docker("inspect", "--format", "{{.State.Pid}}", target.generation.container))
    descriptor = os.pidfd_open(selected_pid, 0)
    try:
        _wait(lambda: f"Recovery epoch {phase} durably recorded" in issuer_log.read_text(), "actual durable " + phase + " publication", timeout=600, process=issuer)
        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
    finally:
        os.close(descriptor)
    if scenario == "lost-reply":
        issuer.wait(timeout=120)
        assert issuer.returncode == 2, "issuer accepted a dead consumer after durable consumption"
    else:
        issuer.wait(timeout=30)
        assert issuer.returncode == -signal.SIGKILL
    _wait(lambda: Chain.docker("inspect", "--format", "{{.State.Running}}", target.generation.container) == "false", "interrupted real peer lifetime ends", timeout=90)
    _no_target_child(target.generation, running=False)
    assert "Recovery custody acknowledged" not in consumer_log.read_text(), "signal raced beyond the required pregrant boundary"
    assert "One consumed recovery epoch grant send completed" not in issuer_log.read_text(), "signal raced beyond actual grant dispatch"
    encoded = (EPOCH_ROOT / (phase + ".json")).read_bytes()
    head = json.loads((EPOCH_ROOT / (phase + "-head.json")).read_bytes())
    assert head["phase"] == phase and head["record_sha256"] == hashlib.sha256(encoded).hexdigest()
    if scenario == "kill-after-intent":
        assert not (EPOCH_ROOT / "consumed.json").exists(), "intent interruption raced beyond consumption"
    before = _snapshot((EPOCH_ROOT,))
    replay = subprocess.run([sys.executable, "-B", "-m", "auto_coder.management.recovery_epoch"], capture_output=True, text=True, timeout=30)
    assert replay.returncode == 2 and _snapshot((EPOCH_ROOT,)) == before, "an interrupted original epoch was repaired or reissued"
    return {"phase": phase, "record_sha256": hashlib.sha256(encoded).hexdigest(), "consumer_grant_acknowledged": False, "grant_send_completed": False}


def _passed(result: dict[str, object], cases: list[str], preserved: str, worker_claims: str, worker_baseline: dict[str, object], worker_posts: dict[str, int]) -> None:
    cases.extend(("originals-oauth-and-workers-preserved", "no-consumer-child-or-admission"))
    result.update(status="passed", no_child_started=True, preserved_originals=True, preserved_workers=True, preserved_originals_sha256=preserved, preserved_worker_claims_sha256=worker_claims, worker_lifetimes=worker_baseline, worker_effect_posts=worker_posts)
    validate_result(result)
    logger.info("All {} native recovery ownership cases passed; no consumer child was launched", len(cases))


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
    # No resource is provisioned until the original marker selected this VM.
    os.umask(0o077)
    for directory in (FIXTURE_ROOT, EPOCH_ROOT, PEER_ROOT, AUTHORIZATION_ROOT):
        if directory.exists() or directory.is_symlink():
            raise FileExistsError("recovery fixture refuses a previous/partial root: " + str(directory))
    _private_directory(FIXTURE_ROOT)
    root = FIXTURE_ROOT / uuid.uuid4().hex
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
        enroll_recovery_scope(tuple(captures), selected_target, tuple(watchers), deployment_id=chain.history_binding.deployment, enrollment_id=target.enrollment_id, authorization=authorization)
        _install_authorization(chain, target, authorization_id, source_repository, source_token_file, source_sha, uv, uv_version)
        retained_roots = (Path(chain.history_binding.root), chain.oauth, *(path for generation in chain.generations for path in (generation.path("state"), generation.path("journal"), generation.path("bootstrap"))))
        Chain.checkpoint(chain.oauth / "oauth.sqlite3")
        preserved = _snapshot(retained_roots)
        worker_before = (worker_baseline, worker_posts, worker_claims)
        workers.preserved(*worker_before)
        issuer_log = root / "issuer.log"
        log_stream = issuer_log.open("xb")
        issuer = subprocess.Popen([sys.executable, "-B", "-m", "auto_coder.management.recovery_epoch"], stdout=log_stream, stderr=subprocess.STDOUT, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        _wait(lambda: PEER_SOCKET.exists() and "Fixed recovery issuer endpoint is listening" in issuer_log.read_text(), "actual issuer listening", process=issuer)
        contender = subprocess.run([sys.executable, "-B", "-m", "auto_coder.management.recovery_epoch"], capture_output=True, text=True, timeout=30)
        assert contender.returncode == 2 and not (EPOCH_ROOT / "reserved.json").exists(), "a second issuer acquired the same current epoch"
        cases.append("independent-issuer-contender-refused")
        # A real SIGSTOP barrier lets the exact target execute its installed
        # interpreter probe before T takes its current process observations.
        issuer.send_signal(signal.SIGSTOP)
        _wait(lambda: Path(f"/proc/{issuer.pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "T", "issuer kernel stop barrier", process=issuer)
        chain.docker("start", target.generation.container)
        probe = json.loads(chain.docker("exec", target.generation.container, target.receipt.interpreter_path, "-I", "-B", "-m", "auto_coder.build_provenance", timeout=60).splitlines()[-1])
        assert probe["source_revision"]["available"] is True and probe["source_revision"]["value"] == source_sha
        assert probe["distribution_version"]["available"] is True and probe["distribution_version"]["value"] == target.receipt.distribution_version
        _no_target_child(target.generation, running=True)
        cases.append("genuine-installed-ready-in-target")
        issuer.send_signal(signal.SIGCONT)
        consumer_log = target.generation.path("state") / "logs" / "recovery-epoch-bootstrap.log"
        result["source_proof"] = {name: getattr(target.receipt, name) for name in ("source_sha", "tested_source_ref", "tested_source_object", "test_run_id", "test_run_attempt", "lock_sha256", "distribution_version", "receipt_identity")}
        if scenario != "grant":
            result["durable"] = _interrupted_issuer(scenario, issuer, issuer_log, consumer_log, target)
            cases.extend(("native-" + scenario, "interrupted-epoch-replay-refused"))
            assert _snapshot(retained_roots) == preserved
            workers.preserved(*worker_before)
            _passed(result, cases, preserved, worker_claims, worker_baseline, worker_posts)
            return result
        _wait(lambda: (EPOCH_ROOT / "receipt-head.json").is_file() and consumer_log.is_file() and "Recovery custody acknowledged" in consumer_log.read_text(), "native grant and consumer custody", timeout=600, process=issuer)
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
        result["failure_type"] = type(error).__name__
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
