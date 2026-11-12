#!/usr/bin/env python3
"""Opt-in, disposable-only MCP acceptance probe with durable send fencing.

The fixture's Docker provisioning is an external operator action. This client
never imports or invokes Docker, changes operator policy, or grants OAuth.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import secrets
import stat
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

# Support both direct script execution and package imports.
if __package__:
    from . import management_mcp_rollout_smoke as smoke
else:
    import management_mcp_rollout_smoke as smoke

TEST_REPOSITORY = "kitamura-tetsuo/auto-coder-test"
BETA_WORK_REPOSITORY = "kitamura-tetsuo/auto-coder-test-beta"
SHA = re.compile(r"[0-9a-f]{40}\Z")
NONCE = re.compile(r"[0-9a-f]{32}\Z")
FIXTURE = re.compile(r"pr[0-9]{3,6}-[a-z0-9-]{6,28}\Z")
MUTATIONS = {
    "reevaluate": "auto_coder_reevaluate_target",
    "beta-source": "auto_coder_deploy_beta_source",
    "beta-acceptance": "auto_coder_run_beta_acceptance",
    "promote-source": "auto_coder_promote_accepted_source_release",
    "release-source": "auto_coder_deploy_release_source",
}
SCENARIOS = frozenset({"observe", "inspect-original", *MUTATIONS})
FULL_SCENARIOS = frozenset({"promote-source", "release-source"})
READS = frozenset(smoke.READ_TOOLS)


class Blocked(RuntimeError):
    """Missing current authority or trustworthy external evidence."""


class HeartbeatNotAdvanced(Blocked):
    """The same live worker has not emitted a newer heartbeat yet."""


class Failed(RuntimeError):
    """A real producer contradicted the immutable expected identity."""


@dataclass(frozen=True)
class Fixture:
    schema: int
    fixture_nonce: str
    firebase_project_id: str
    repository: str
    fixture_id: str
    compose_project: str
    subnet: str
    endpoint: str
    endpoint_namespace: str
    target_ids: dict[str, str]
    mutation_policy: str
    docker_authority_in_worker_or_management: bool
    source_fixture_sha: str
    source_fixture_available: bool

    @classmethod
    def load(cls, path: Path) -> "Fixture":
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or set(raw) != set(cls.__dataclass_fields__):
            raise Blocked("Disposable fixture manifest has missing or extra fields")
        try:
            fixture = cls(**raw)
        except TypeError as error:
            raise Blocked("Disposable fixture manifest types are invalid") from error
        if type(fixture.schema) is not int or fixture.schema != 1 or not isinstance(fixture.fixture_id, str) or FIXTURE.fullmatch(fixture.fixture_id) is None:
            raise Blocked("Disposable fixture identity is invalid")
        if not isinstance(fixture.fixture_nonce, str) or NONCE.fullmatch(fixture.fixture_nonce) is None:
            raise Blocked("Disposable fixture nonce is invalid")
        if not isinstance(fixture.firebase_project_id, str) or re.fullmatch(r"fixture-[a-z0-9-]{6,50}", fixture.firebase_project_id) is None:
            raise Blocked("The current Firebase project is not bounded to a disposable fixture")
        if fixture.repository != TEST_REPOSITORY or fixture.compose_project != "dot-" + fixture.fixture_id:
            raise Blocked("Target source repository or Compose project is not the approved disposable fixture")
        if fixture.endpoint != "http://127.0.0.1:8081/mcp" or fixture.endpoint_namespace != "management-container-only":
            raise Blocked("The fixture MCP endpoint must be its own container loopback")
        if fixture.mutation_policy != "observe until separately configured and granted" or fixture.docker_authority_in_worker_or_management is not False:
            raise Blocked("Fixture grants or Docker authority differ from the original fail-closed configuration")
        if type(fixture.source_fixture_available) is not bool or not isinstance(fixture.source_fixture_sha, str):
            raise Blocked("Fixture source identity is malformed")
        if fixture.source_fixture_available != bool(fixture.source_fixture_sha) or (fixture.source_fixture_sha and SHA.fullmatch(fixture.source_fixture_sha) is None):
            raise Blocked("Fixture source requires a pinned full Git SHA")
        if fixture.target_ids != {"beta": "dot-" + fixture.fixture_id + "-beta", "release": "dot-" + fixture.fixture_id + "-release"}:
            raise Blocked("Fixture target IDs are not fixed to the original disposable project")
        try:
            network = ipaddress.ip_network(fixture.subnet, strict=True)
        except ValueError as error:
            raise Blocked("Fixture network identity is invalid") from error
        if network.version != 4 or network.prefixlen != 24 or not str(network).startswith("10.249."):
            raise Blocked("Fixture private network is outside the approved test address pool")
        return fixture


@dataclass(frozen=True)
class Evidence:
    tool: str
    sha256: str
    origin: str
    status: str


@dataclass(frozen=True)
class Result:
    verdict: str
    scenario: str
    reason: str
    observations: tuple[Evidence, ...] = ()
    operation_id: str = ""
    positive_deployment_proven: bool = False
    effect_dispatched: bool | None = False
    original_ticket_state: str = ""
    details: dict[str, object] = field(default_factory=dict)


def bound_local_fixture(fixture: Fixture, *, environ: dict[str, str] | None = None, docker_socket: Path = Path("/var/run/docker.sock")) -> None:
    """Enforce a trusted, nonproduction container boundary before opening MCP."""
    environment = os.environ if environ is None else environ
    if environment.get("DOT_DISPOSABLE_FIXTURE_NONCE") != fixture.fixture_nonce:
        raise Blocked("Run only from the exact disposable management container; no transport opened")
    if environment.get("DOCKER_HOST") or environment.get("DOCKER_CONTEXT"):
        raise Blocked("The MCP client must have no runtime Docker authority")
    try:
        socket_mode = docker_socket.stat().st_mode
    except FileNotFoundError:
        socket_mode = 0
    if stat.S_ISSOCK(socket_mode):
        raise Blocked("The runtime Docker socket is forbidden")
    try:
        registry = json.loads(environment["AUTO_CODER_WORKER_REGISTRY"])
    except (KeyError, ValueError, TypeError) as error:
        raise Blocked("Fixture worker registry cannot be independently checked") from error
    if not isinstance(registry, dict) or set(registry) != {"workers"} or not isinstance(registry["workers"], list) or len(registry["workers"]) != 2:
        raise Blocked("Fixture must have exactly two independent worker bindings")
    expected = {
        fixture.target_ids["beta"]: ("beta", BETA_WORK_REPOSITORY),
        fixture.target_ids["release"]: ("release", TEST_REPOSITORY),
    }
    actual = {}
    for entry in registry["workers"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("target_id"), str):
            raise Blocked("Worker registry entry is invalid")
        target = entry["target_id"]
        if entry.get("supervisor_instance_id") != target or entry.get("bridge_port") != 8843:
            raise Blocked("Physical target identity or bridge port changed")
        if entry.get("bridge_secret_file") != "/fixture/secrets/" + entry.get("channel", "") + "-bridge":
            raise Blocked("Worker secret path is outside the fixture")
        try:
            address = ipaddress.ip_address(entry["bridge_address"])
        except (KeyError, ValueError) as error:
            raise Blocked("Worker bridge address is invalid") from error
        if address not in ipaddress.ip_network(fixture.subnet):
            raise Blocked("Worker bridge address is outside the fixture network")
        actual[target] = (entry.get("channel"), entry.get("repository"))
    if actual != expected:
        raise Blocked("Worker registry does not match exact disposable beta/release targets")


def original_stage(checkpoint: smoke.DotCheckpoint, scenario: str, selected_stage: str, selected_revision: str, *, mutation: bool, resume_original: bool = False) -> None:
    if scenario not in SCENARIOS:
        raise Blocked("Unsupported acceptance scenario")
    if not mutation:
        if resume_original:
            raise Blocked("Original mutation reconciliation requires explicit mutation selection")
        if scenario not in {"observe", "inspect-original"}:
            raise Blocked("Read-only is the default; mutation requires explicit selection")
        if scenario == "observe" and (checkpoint.pending_effect_state or checkpoint.pending_operation_id):
            raise Blocked("Inspect the original unresolved operation before worker-only observation")
        return
    if scenario not in MUTATIONS:
        raise Blocked("No mutating action is available for this scenario")
    if checkpoint.stage not in {"beta", "full"}:
        raise Blocked("Observe stage can never dispatch an effect")
    if scenario in FULL_SCENARIOS and checkpoint.stage != "full":
        raise Blocked("Source promotion and release activation require the current full operator stage")
    try:
        smoke.confirm_operator_scope(checkpoint, selected_stage, selected_revision)
    except smoke.OperatorScopeError as error:
        raise Blocked(str(error)) from error
    if not (checkpoint.source_sha and checkpoint.ci_run_id and checkpoint.ci_run_attempt and checkpoint.tested_source_ref and checkpoint.tested_source_oid and checkpoint.beta_target):
        raise Blocked("Exact original tested source, CI and beta target lineage are required")
    if scenario in FULL_SCENARIOS and (not checkpoint.release_target or checkpoint.release_target == checkpoint.beta_target):
        raise Blocked("Full stage requires a distinct original configured release target")
    # A full-stage promotion creates its own catalog. Requiring that completed
    # catalog here would make the first legitimate promotion unreachable.
    if not resume_original and (checkpoint.pending_effect_state or checkpoint.pending_operation_id):
        raise Blocked("Reconcile the original unknown effect before any new send")
    if checkpoint.recovery_attempts and not resume_original:
        raise Blocked("An original unresolved effect or exhausted recovery budget forbids any new send")
    if not checkpoint.idempotency_key or smoke.SAFE_ID.fullmatch(checkpoint.idempotency_key) is None:
        raise Blocked("Persist the immutable original idempotency key before admission")


def ticket_path(scenario: str) -> Path:
    if scenario not in MUTATIONS:
        raise Blocked("Only a fixed disposable mutation may reserve a send ticket")
    return Path("/fixture/management") / f"dot-fixture-{scenario}-original-send.json"


async def check_retained_recovery(live: Live, fixture: Fixture, checkpoint: smoke.DotCheckpoint, action: str, target: str, *, root: Path) -> None:
    """Keep a consumed original budget across checkpoint/key replacement.

    Terminal FAILED lifecycle operations release the producer's target lane.
    The companion ledger therefore remains relevant even without a positive
    send ticket. Inspect only its exact original; never reconcile or retry it.
    """
    expected = (action.removeprefix("auto_coder_"), target, checkpoint.source_sha)
    for path in sorted(root.glob("dot-recovery-*.json")):
        try:
            if path.is_symlink():
                raise ValueError("replaced recovery record")
            retained = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_pairs)
        except (OSError, ValueError, UnicodeError) as error:
            raise Blocked("The retained original recovery budget cannot be checked") from error
        failure = retained.get("failure") if isinstance(retained, dict) else None
        if not isinstance(failure, dict):
            raise Blocked("The retained original recovery identity is unavailable")
        if (failure.get("action"), failure.get("target"), failure.get("source_sha")) != expected:
            continue
        signature = hashlib.sha256(json.dumps(failure, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        operation_id = retained.get("original_operation_id")
        if (
            type(retained.get("schema")) is not int
            or retained["schema"] != 1
            or retained.get("fixture_nonce") != fixture.fixture_nonce
            or set(failure) != {"action", "target", "source_sha", "reason"}
            or not isinstance(failure.get("reason"), str)
            or smoke.SAFE_ID.fullmatch(failure["reason"]) is None
            or retained.get("signature") != signature
            or path.name != f"dot-recovery-{signature}.json"
            or not isinstance(operation_id, str)
            or smoke.SAFE_ID.fullmatch(operation_id) is None
            or type(retained.get("recovery_attempts")) is not int
            or retained["recovery_attempts"] != 1
            or retained.get("outcome") != "BLOCKED"
        ):
            raise Blocked("The retained original recovery budget has changed or is incomplete")
        original = await live.call("auto_coder_operation_inspect", {"target_id": target, "operation_id": operation_id})
        if (original.get("operation_id"), original.get("action"), original.get("target_id"), original.get("reason")) != (operation_id, expected[0], target, failure["reason"]):
            raise Blocked("The retained exhausted original cannot be independently verified; reconcile that original before any new send")
        if expected[0].startswith("deploy_") and original.get("source_sha") != checkpoint.source_sha:
            raise Blocked("The retained exhausted original source cannot be independently verified")
        if type(original.get("recovery_attempts")) is not int or original["recovery_attempts"] != 1 or (original.get("status"), original.get("phase")) not in {("FAILED", "failed"), ("NEEDS_ATTENTION", "needs_attention")}:
            raise Blocked("The retained recovery outcome changed; reconcile its original operation before any new send")
        if original["status"] == "FAILED" and original.get("effect_state") != "confirmed":
            raise Blocked("The retained failed recovery has no confirmed original producer outcome")
        raise Blocked(f"Original operation {operation_id} exhausted the recovery budget for this action, target and source; a new key cannot retry failure {signature}")


def persist_original_ticket(path: Path, fixture: Fixture, checkpoint: smoke.DotCheckpoint, action: str, args: dict[str, object]) -> None:
    """Serialize action-specific original admission across independent clients.

    O_EXCL protects a single action path, not two different actions racing
    on the same target. Hold the stable, cross-process directory lock through
    scanning prior intent records and fsyncing the new original ticket.
    """
    lock_path = path.parent / ".dot-fixture-original-send.lock"
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise Blocked("The disposable original admission lock is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise Blocked("The disposable original admission lock is untrusted")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as error:
            raise Blocked("Cannot establish exclusive original admission custody") from error
        _persist_original_ticket_locked(path, fixture, checkpoint, action, args)
    finally:
        os.close(descriptor)


def _persist_original_ticket_locked(path: Path, fixture: Fixture, checkpoint: smoke.DotCheckpoint, action: str, args: dict[str, object]) -> None:
    """Fsync original nonsecret intent before a send, under the directory lock."""
    if path.exists() or path.is_symlink():
        raise Blocked("A previous admission may have been sent; reconcile its original ticket before retry")
    target = args.get("target_id")
    if target not in fixture.target_ids.values():
        raise Blocked("Original intent must target one exact disposable worker")
    for existing_path in path.parent.glob("dot-fixture-*-original-send.json"):
        try:
            previous = json.loads(existing_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as error:
            raise Blocked("A previous original send ticket cannot be checked") from error
        if not isinstance(previous, dict) or previous.get("fixture_nonce") != fixture.fixture_nonce:
            raise Blocked("A previous ticket has an untrusted fixture identity")
        if previous.get("target") == target and previous.get("idempotency_key") == checkpoint.idempotency_key:
            raise Blocked("Original idempotency key cannot be reused for a different action on one target")
        if previous.get("target") == target:
            refusal = previous.get("refusal_evidence")
            refused = previous.get("effect_state") == "REFUSED" and not previous.get("operation_id") and isinstance(refusal, dict) and refusal.get("operation_absent") is True and isinstance(refusal.get("journal_sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", refusal["journal_sha256"]) is not None
            if previous.get("effect_state") != "SUCCEEDED" and not refused:
                raise Blocked("Another original effect on this target is unresolved; changing the idempotency key cannot bypass it")
    record = {
        "schema": 1,
        "fixture_nonce": fixture.fixture_nonce,
        "task_id": checkpoint.task_id,
        "work_repository": checkpoint.repository,
        "issue_number": checkpoint.issue_number,
        "pr_number": checkpoint.pr_number,
        "stage_revision": checkpoint.stage_revision,
        "source_sha": checkpoint.source_sha,
        "action": action,
        "target": target,
        "idempotency_key": checkpoint.idempotency_key,
        "args": args,
        "effect_state": "ADMISSION_UNKNOWN",
        "operation_id": "",
    }
    encoded = (json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    if len(encoded) > 8192:
        raise Blocked("Original immutable intent is too large to journal safely")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            file.write(encoded)
            file.flush()
            os.fsync(file.fileno())
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except FileExistsError as error:
        raise Blocked("Original admission ticket already exists; never allocate a new key") from error


def _single_nested_acceptance_error(error: BaseException) -> Blocked | Failed | None:
    """Preserve one trusted refusal across MCP transport task-group wrappers.

    Multiple failures or unrelated transport exceptions remain unknown effects.
    """
    nested = error
    while isinstance(nested, ExceptionGroup) and len(nested.exceptions) == 1:
        nested = nested.exceptions[0]
    return nested if isinstance(nested, (Blocked, Failed)) else None


def exception_result(
    scenario: str,
    reason: str,
    *,
    mutation: bool,
    verdict: str = "BLOCKED",
    fixture: Fixture | None = None,
) -> Result:
    """Use the immutable original ticket when an attempted MCP response is lost.

    No exception handler may claim 'not dispatched' once a durable admission
    ticket could have been sent. Ticket inspection is local and never retries.
    """
    if not mutation or scenario not in MUTATIONS:
        return Result(verdict, scenario, reason)
    path = ticket_path(scenario)
    try:
        exists = path.exists() or path.is_symlink()
    except OSError:
        return Result(verdict, scenario, reason, effect_dispatched=None, original_ticket_state="UNKNOWN")
    if not exists:
        return Result(verdict, scenario, reason)
    # Never follow a replaced original ticket symlink, even in the fixture.
    if path.is_symlink():
        return Result(verdict, scenario, reason, effect_dispatched=None, original_ticket_state="UNKNOWN")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        record = None
    if (
        not isinstance(record, dict)
        or record.get("schema") != 1
        or record.get("action") != MUTATIONS[scenario]
        or not isinstance(record.get("fixture_nonce"), str)
        or (fixture is not None and record["fixture_nonce"] != fixture.fixture_nonce)
        or record.get("effect_state") not in {"ADMISSION_UNKNOWN", "ADMITTED", "SUCCEEDED"}
    ):
        return Result(verdict, scenario, reason, effect_dispatched=None, original_ticket_state="UNKNOWN")
    state = record["effect_state"]
    operation_id = record.get("operation_id")
    if not isinstance(operation_id, str) or (operation_id and smoke.SAFE_ID.fullmatch(operation_id) is None) or (state != "ADMISSION_UNKNOWN" and not operation_id):
        return Result(verdict, scenario, reason, effect_dispatched=None, original_ticket_state="UNKNOWN")
    return Result(verdict, scenario, reason, operation_id=operation_id, effect_dispatched=None, original_ticket_state=state)


def update_original_ticket(path: Path, fixture: Fixture, operation_id: str, state: str, *, evidence: dict[str, object] | None = None) -> None:
    """Persist only the observed original ID/state; never authorize resending."""
    if state not in {"ADMITTED", "SUCCEEDED"} or smoke.SAFE_ID.fullmatch(operation_id) is None:
        raise Blocked("Original operation update has an invalid identity")
    try:
        original = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise Blocked("The durable original send ticket is not readable") from error
    if not isinstance(original, dict) or original.get("fixture_nonce") != fixture.fixture_nonce:
        raise Blocked("Original send ticket was replaced or corrupted")
    if original.get("operation_id") not in {"", operation_id} or original.get("effect_state") not in {"ADMISSION_UNKNOWN", "ADMITTED", "SUCCEEDED"}:
        raise Blocked("Original ticket operation ID/state changed unexpectedly")
    if original.get("effect_state") == "SUCCEEDED" and state != "SUCCEEDED":
        raise Blocked("Terminal original send cannot regress to an admitted state")
    updated = {**original, "operation_id": operation_id, "effect_state": state}
    if evidence is not None:
        updated["terminal_evidence"] = evidence
    _replace_original_ticket(path, updated)


def _replace_original_ticket(path: Path, record: dict[str, object]) -> None:
    """Atomically replace only this fixture-owned original evidence record."""
    temporary = path.parent / (".dot-original-" + secrets.token_hex(8))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(record, file, sort_keys=True, separators=(",", ":"))
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def record_refused_ticket(path: Path, fixture: Fixture, *, reason: str, journal_sha256: str) -> None:
    """Record a live refusal only after independent absence in the real journal.

    This is used by the negative acceptance orchestrator after checking the
    exact original key. A refusal response alone cannot establish no admission.
    """
    if not reason or len(reason) > 256 or re.fullmatch(r"[0-9a-f]{64}", journal_sha256) is None or path.is_symlink():
        raise Blocked("Independent original refusal evidence is invalid")
    try:
        original = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise Blocked("Original refused admission ticket is unavailable") from error
    if not isinstance(original, dict) or original.get("fixture_nonce") != fixture.fixture_nonce or original.get("effect_state") != "ADMISSION_UNKNOWN" or original.get("operation_id") != "":
        raise Blocked("An admitted or replaced original ticket cannot be reported as refused")
    _replace_original_ticket(path, {**original, "effect_state": "REFUSED", "refusal_evidence": {"reason": reason, "journal_sha256": journal_sha256, "operation_absent": True}})


def positive_heartbeat(snapshot: dict[str, Any], *, source_sha: str = "") -> int:
    if snapshot.get("status") != "OK" or snapshot.get("alive") is not True or snapshot.get("process_state") != "RUNNING":
        raise Blocked("Signed Supervisor cannot establish a live original worker")
    if snapshot.get("installed_binding_status") != "VERIFIED":
        raise Blocked("An unstaged/bootstrap child is not verified installed source")
    binding = snapshot.get("installed_runtime")
    if not isinstance(binding, dict) or (source_sha and binding.get("source_sha") != source_sha):
        raise Failed("Installed source binding contradicts the exact tested Git commit")
    observation = snapshot.get("worker_observation")
    if not isinstance(observation, dict):
        raise Blocked("Worker-produced observation is unavailable")
    lifecycle, heartbeat = observation.get("lifecycle"), observation.get("heartbeat")
    if not isinstance(lifecycle, dict) or lifecycle.get("startup_reconciled") is not True or not isinstance(heartbeat, dict):
        raise Blocked("Real startup reconciliation or worker heartbeat is unavailable")
    invocations = observation.get("invocations")
    worker = snapshot.get("worker")
    if (
        lifecycle.get("state") != "running"
        or lifecycle.get("daemon_serving") is not True
        or "startup_reconciliation_error" not in lifecycle
        or lifecycle["startup_reconciliation_error"] is not None
        or lifecycle.get("startup_failure") is not None
        or not isinstance(invocations, dict)
        or invocations.get("state") != "running"
        or (isinstance(worker, dict) and worker.get("stop_requested") is True)
    ):
        raise Blocked("A draining, failed or nonserving child cannot establish worker readiness")
    sequence = heartbeat.get("sequence")
    if heartbeat.get("available") is not True or type(sequence) is not int or sequence <= 0 or not str(heartbeat.get("stage", "")).startswith("producer:"):
        raise Blocked("A running container is not a positive heartbeat producer")
    return sequence


def require_advancing_worker_heartbeat(before: dict[str, Any], after: dict[str, Any], *, source_sha: str = "") -> None:
    """Prove progress within one stable installed worker, not across a restart.

    An increased counter alone cannot distinguish two different process runs.
    Both observations must be from the same original Supervisor, runtime,
    deployment, process lifetime and installed source binding.
    """
    prior_sequence = positive_heartbeat(before, source_sha=source_sha)
    current_sequence = positive_heartbeat(after, source_sha=source_sha)

    def identity(snapshot: dict[str, Any]) -> tuple[str, str, str, str, dict[str, Any]]:
        worker = snapshot.get("worker")
        binding = snapshot.get("installed_runtime")
        if not isinstance(worker, dict) or not isinstance(binding, dict):
            raise Blocked("Original installed worker identity is unavailable")
        fields = (
            snapshot.get("supervisor_generation"),
            worker.get("runtime_id"),
            worker.get("process_run_id"),
            worker.get("deployment_generation"),
        )
        if any(not isinstance(value, str) or not value for value in fields):
            raise Blocked("Original installed worker identity is incomplete")
        selected_runtime = snapshot.get("selected_runtime_id")
        if selected_runtime is not None and selected_runtime != worker["runtime_id"]:
            raise Blocked("Selected and running worker runtime identities differ")
        return fields[0], fields[1], fields[2], fields[3], binding

    if identity(before) != identity(after):
        raise Blocked("Worker identity or installed source changed between heartbeat observations")
    if current_sequence <= prior_sequence:
        raise HeartbeatNotAdvanced("Original worker heartbeat did not advance")


def _unique_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, item in pairs:
        if key in result:
            raise ValueError("duplicate original receipt field")
        result[key] = item
    return result


def verified_reevaluation_receipt(value: dict[str, Any], *, operation_id: str, target_id: str, process_run_id: str) -> bool:
    """Recognize the actual journal's accepted-for-reevaluation projection.

    The worker receipt proves queue admission, not Issue work completion.
    An unresolved original remains pending; it cannot trigger a new send.
    """
    if value.get("operation_id") != operation_id or value.get("target_id") != target_id or value.get("action") != "reevaluate_target":
        raise Failed("Original reevaluation operation, action or physical target differs")
    if value.get("status") != "accepted_for_reevaluation":
        if value.get("status") == "SUCCEEDED":
            raise Blocked("A generic completed status is not a worker reevaluation receipt")
        return False
    if value.get("phase") != "succeeded" or value.get("effect_state") != "confirmed":
        raise Blocked("Reevaluation acceptance lacks a confirmed original journal phase")
    receipt = value.get("receipt")
    if not isinstance(receipt, dict) or receipt.get("source") != "worker-uds-invalidation-receipt" or not isinstance(receipt.get("result"), str):
        raise Blocked("The original signed worker invalidation receipt is unavailable")

    try:
        detail = json.loads(receipt["result"], object_pairs_hook=_unique_json_pairs)
    except ValueError as error:
        raise Blocked("Original worker receipt content cannot be verified") from error
    if (
        not isinstance(detail, dict)
        or set(detail) != {"outcome", "generation", "disposition", "accepting_process_run_id"}
        or detail["outcome"] != "accepted_for_reevaluation"
        or type(detail["generation"]) is not int
        or detail["generation"] <= 0
        or not isinstance(detail["disposition"], str)
        or not detail["disposition"]
        or detail["accepting_process_run_id"] != process_run_id
    ):
        raise Failed("Original worker receipt does not bind the accepted reevaluation")
    return True


def _source_client():
    """Use only the fixture's read credential and fixed disposable repository."""
    from auto_coder.util.gh_cache import GitHubGitDataClient

    token = os.environ.get("AUTO_CODER_SOURCE_READ_TOKEN", "")
    path = Path("/fixture/secrets/source-public-read")
    if not token and path.is_file():
        token = path.read_text(encoding="utf-8").strip()
    return GitHubGitDataClient(token, TEST_REPOSITORY)


def verify_real_source(checkpoint: smoke.DotCheckpoint, fixture: Fixture) -> dict[str, object]:
    """Read the original public Git ref, annotated object and source Actions attempt."""
    if not fixture.source_fixture_available or checkpoint.source_sha != fixture.source_fixture_sha:
        raise Blocked("Source under test is not the fixture's originally pinned main commit")
    from auto_coder.source_release_catalog import digest
    from auto_coder.tested_source_refs import TestedSourcePublisher

    publisher = TestedSourcePublisher(_source_client(), TEST_REPOSITORY)
    proof = publisher.read(checkpoint.source_sha)
    if proof is None:
        raise Blocked("The exact immutable tested-source Git ref has not been published")
    record = proof.record
    if (proof.tag_ref, proof.tag_object_oid) != (checkpoint.tested_source_ref, checkpoint.tested_source_oid):
        raise Failed("The test repository's annotated proof ref/OID differs from original checkpoint")
    if (str(record.run_id), str(record.run_attempt)) != (checkpoint.ci_run_id, checkpoint.ci_run_attempt):
        raise Failed("Original source GitHub Actions run/attempt differs from published immutable proof")
    publisher.verify_run(record, recorded_attempt=True)
    return {"tested_source_record_sha256": digest(asdict(record))}


def _receipt(value: dict[str, object], source: str) -> dict[str, object]:
    receipt = value.get("receipt")
    if not isinstance(receipt, dict) or receipt.get("source") != source or not isinstance(receipt.get("result"), str):
        raise Blocked("Original confirmed producer receipt is unavailable")

    try:
        detail = json.loads(receipt["result"], object_pairs_hook=_unique_json_pairs)
    except (ValueError, TypeError) as error:
        raise Blocked("Original producer receipt cannot be parsed exactly") from error
    if not isinstance(detail, dict):
        raise Blocked("Original producer receipt is not a structured record")
    return detail


def verified_acceptance_receipt(value: dict[str, object], *, operation_id: str, target_id: str, source_sha: str) -> dict[str, object]:
    if (value.get("operation_id"), value.get("target_id"), value.get("action")) != (operation_id, target_id, "run_beta_acceptance"):
        raise Failed("Original beta acceptance operation or physical target differs")
    if value.get("status") != "PASS_PUBLISHED" or value.get("phase") != "succeeded" or value.get("effect_state") != "confirmed":
        raise Blocked("Local beta smoke is not independently published acceptance")
    receipt = _receipt(value, "github-source-acceptance-readback")
    if receipt.get("status") != "PASS_PUBLISHED" or receipt.get("tag_ref") != "refs/tags/auto-coder-beta-acceptance-" + operation_id or receipt.get("direct_commit_sha") != source_sha or not isinstance(receipt.get("tag_object_oid"), str) or SHA.fullmatch(receipt["tag_object_oid"]) is None:
        raise Failed("Published beta acceptance receipt differs from the original source")
    return {"beta_acceptance_operation_id": operation_id, "acceptance_ref": receipt["tag_ref"], "acceptance_oid": receipt["tag_object_oid"]}


def verify_published_acceptance(checkpoint: smoke.DotCheckpoint, fixture: Fixture, evidence: dict[str, object]) -> dict[str, object]:
    """Independently read the actual immutable Git acceptance object."""
    from auto_coder.management.source_acceptance import SourceAcceptancePublisher

    operation_id = str(evidence["beta_acceptance_operation_id"])
    observed = SourceAcceptancePublisher(_source_client(), TEST_REPOSITORY).read(operation_id)
    if observed is None:
        raise Blocked("Original beta acceptance Git object is not available")
    if (observed.tag_ref, observed.tag_object_oid, observed.direct_commit_sha) != (evidence["acceptance_ref"], evidence["acceptance_oid"], checkpoint.source_sha):
        raise Failed("Independent beta acceptance Git readback differs from the original receipt")
    document = json.loads(observed.annotation)
    local = document.get("local_evidence")
    if not isinstance(local, dict) or (local.get("operation_id"), local.get("instance_id"), local.get("channel"), local.get("repository"), local.get("worker_repository"), local.get("source_sha")) != (
        operation_id,
        fixture.target_ids["beta"],
        "beta",
        TEST_REPOSITORY,
        BETA_WORK_REPOSITORY,
        checkpoint.source_sha,
    ):
        raise Failed("Published beta acceptance is not from the original disposable beta worker")
    return {**evidence, "acceptance_annotation_sha256": hashlib.sha256(observed.annotation.encode("utf-8")).hexdigest()}


def verified_promotion_receipt(value: dict[str, object], *, operation_id: str, target_id: str, source_sha: str) -> dict[str, object]:
    if (value.get("operation_id"), value.get("target_id"), value.get("action")) != (operation_id, target_id, "promote_accepted_source_release"):
        raise Failed("Original source promotion operation or target differs")
    if value.get("status") != "SUCCEEDED" or value.get("phase") != "succeeded" or value.get("effect_state") != "confirmed" or value.get("source_promotion") != "SOURCE_PROMOTION_CONFIRMED" or value.get("worker_deployed") is not False:
        raise Blocked("Original source promotion is unresolved, superseded, or not independently confirmed")
    receipt = _receipt(value, "github-source-release-readback")
    run = value.get("workflow_run")
    if (
        receipt.get("outcome") != "SOURCE_PROMOTION_CONFIRMED"
        or receipt.get("source_sha") != source_sha
        or receipt.get("worker_deployed") is not False
        or not isinstance(receipt.get("catalog_oid"), str)
        or SHA.fullmatch(receipt["catalog_oid"]) is None
        or not isinstance(run, dict)
        or type(receipt.get("run_id")) is not int
        or receipt["run_id"] <= 0
        or type(receipt.get("run_attempt")) is not int
        or receipt["run_attempt"] <= 0
        or (run.get("id"), run.get("run_attempt")) != (receipt["run_id"], receipt["run_attempt"])
    ):
        raise Failed("Source promotion receipt does not bind its original catalog and Actions attempt")
    return {
        "promotion_operation_id": operation_id,
        "catalog_ref": "refs/tags/auto-coder-source-release-" + operation_id,
        "catalog_oid": receipt["catalog_oid"],
        "promotion_run_id": receipt["run_id"],
        "promotion_run_attempt": receipt["run_attempt"],
        "workflow_run": run,
        "release_worker_deployed": False,
    }


def confirm_saved_promotion(checkpoint: smoke.DotCheckpoint, evidence: dict[str, object]) -> None:
    """A server observation cannot silently replace a saved Actions identity."""
    if checkpoint.promotion_operation_id and checkpoint.promotion_operation_id != evidence["promotion_operation_id"]:
        raise Failed("Current promotion differs from the original selected operation")
    if checkpoint.promotion_run_id and checkpoint.promotion_run_id != str(evidence["promotion_run_id"]):
        raise Failed("Current promotion differs from the original selected Actions run")
    if (checkpoint.promotion_catalog_ref or checkpoint.promotion_catalog_oid) and (checkpoint.promotion_catalog_ref, checkpoint.promotion_catalog_oid) != (evidence["catalog_ref"], evidence["catalog_oid"]):
        raise Failed("Current promotion catalog differs from the original selected checkpoint ref/OID")


def verify_published_promotion(checkpoint: smoke.DotCheckpoint, fixture: Fixture, evidence: dict[str, object], acceptance: dict[str, object]) -> dict[str, object]:
    """Read the actual Actions attempt, catalog and both current Git selectors."""
    from auto_coder.management.source_release_dispatch import SourceReleaseDispatch
    from auto_coder.source_release_catalog import SourceReleaseCatalog, SourceReleasePolicy, SourceReleaseRequest
    from auto_coder.source_release_promotion import AcceptedSourceValidator, SourceReleasePromotion

    policy = SourceReleasePolicy.load(Path("/fixture/policy/source-release-policy.json"))
    if policy.source_repository != TEST_REPOSITORY or fixture.target_ids["beta"] not in policy.accepted_beta_instance_ids:
        raise Blocked("Original source promotion policy is not bound to this disposable fixture")
    client = _source_client()
    original = str(evidence["promotion_operation_id"])
    catalog = SourceReleaseCatalog(client, TEST_REPOSITORY).read(original)
    if catalog is None:
        raise Blocked("Original source promotion catalog is unavailable")
    if catalog.tag_object_oid != evidence["catalog_oid"] or "refs/tags/" + catalog.record.tag != evidence["catalog_ref"]:
        raise Failed("Independent source catalog ref/OID differs from the original operation")
    source = verify_real_source(checkpoint, fixture)
    request = SourceReleaseRequest(
        operation_id=original,
        source_repository=TEST_REPOSITORY,
        source_sha=checkpoint.source_sha,
        ci_run_id=int(checkpoint.ci_run_id),
        ci_run_attempt=int(checkpoint.ci_run_attempt),
        tested_source_ref=checkpoint.tested_source_ref,
        tested_source_oid=checkpoint.tested_source_oid,
        tested_source_record_sha256=str(source["tested_source_record_sha256"]),
        acceptance_ref=str(acceptance["acceptance_ref"]),
        acceptance_oid=str(acceptance["acceptance_oid"]),
        release_target=policy.release_target,
        promotion_run_id=int(evidence["promotion_run_id"]),
        promotion_run_attempt=int(evidence["promotion_run_attempt"]),
    )
    validated = AcceptedSourceValidator(client, policy).validate(request)
    promoter = SourceReleasePromotion(client, policy)
    promoter._verify_original(catalog.record, request, validated)
    # This existing producer method performs repeated readback only. Never
    # call execute(), which can publish Git objects or move a release ref.
    promoter._confirmed(catalog.record, catalog.tag_object_oid, "CONFIRMED")
    run = evidence["workflow_run"]
    if not isinstance(run, dict) or type(run.get("workflow_id")) is not int:
        raise Blocked("Original promotion workflow identity is unavailable")
    response = client.request("GET", f"actions/runs/{request.promotion_run_id}/attempts/{request.promotion_run_attempt}")
    if response.status != 200 or not isinstance(response.data, dict):
        raise Blocked("Original promotion Actions attempt is not available")
    if SourceReleaseDispatch._run(response.data, policy, run["workflow_id"], original) != run or response.data.get("status") != "completed" or response.data.get("conclusion") != "success":
        raise Failed("Original promotion Actions attempt is not exactly successful")
    return {**evidence, **acceptance, "source_sha": checkpoint.source_sha, "source_repository": TEST_REPOSITORY}


def verify_current_operator(identity: dict[str, Any], fixture: Fixture, scenario: str, *, mutation: bool) -> None:
    scopes = identity.get("scopes")
    expected = {"auto-coder.read"}
    if mutation:
        expected.add("auto-coder.operate" if scenario == "reevaluate" else "auto-coder.deploy")
    if (
        identity.get("resource") != fixture.endpoint
        or identity.get("client_id") != "dot-disposable-operator"
        or not isinstance(identity.get("uid"), str)
        or not identity["uid"]
        or identity.get("project_id") != fixture.firebase_project_id
        or not isinstance(scopes, (list, tuple))
        or not expected.issubset(set(scopes))
    ):
        raise Blocked("The original fixture OAuth client, identity, resource or current scopes are not verified")


def verify_current_targets(value: dict[str, Any], fixture: Fixture) -> None:
    """Require two unambiguous current policy targets, without discarding entries."""
    selected = value.get("targets")
    expected = {
        fixture.target_ids["beta"]: ("beta", BETA_WORK_REPOSITORY),
        fixture.target_ids["release"]: ("release", TEST_REPOSITORY),
    }
    if value.get("status") != "OK" or not isinstance(selected, list) or len(selected) != len(expected):
        raise Blocked("The current policy must expose exactly two disposable worker targets")
    actual: dict[str, tuple[str, str]] = {}
    for entry in selected:
        if not isinstance(entry, dict) or any(not isinstance(entry.get(key), str) or not entry[key] for key in ("target_id", "channel", "repository")):
            raise Blocked("The current policy contains an incomplete physical target binding")
        target = entry["target_id"]
        if target in actual:
            raise Blocked("The current policy contains a duplicate physical target binding")
        actual[target] = (entry["channel"], entry["repository"])
    if actual != expected:
        raise Blocked("The current policy target map cannot prove the two disposable workers")


class Live:
    def __init__(self, session: Any, registered: set[str]):
        self.session = session
        self.registered = registered
        self.observations: list[Evidence] = []
        self.original_reconciliation: tuple[str, str] | None = None

    def authorize_original_reconciliation(self, target_id: str, operation_id: str) -> None:
        """Bind one explicitly authorized promotion readback, never a new key."""
        if any(smoke.SAFE_ID.fullmatch(value) is None for value in (target_id, operation_id)):
            raise Blocked("Original reconciliation identity is invalid")
        selected = (target_id, operation_id)
        if self.original_reconciliation not in {None, selected}:
            raise Blocked("One acceptance invocation cannot switch its original operation")
        self.original_reconciliation = selected

    async def call(self, name: str, arguments: dict[str, object], *, mutation: bool = False) -> dict[str, Any]:
        if name not in self.registered:
            raise Blocked("Required MCP producer tool is not registered: " + name)
        if mutation:
            original_read = name == "auto_coder_operation_get" and set(arguments) == {"target_id", "operation_id"} and self.original_reconciliation == (arguments.get("target_id"), arguments.get("operation_id"))
            if name not in MUTATIONS.values() and not original_read:
                raise Blocked("Only fixed fixture mutation tools are allowed")
        else:
            smoke.checked_tool(name, arguments)
        result = await self.session.call_tool(name, arguments)
        if result.isError or not isinstance(result.structuredContent, dict):
            raise Blocked("Current authenticated MCP structured producer evidence is unavailable")
        value = result.structuredContent
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.observations.append(Evidence(name, hashlib.sha256(encoded).hexdigest(), "authenticated live MCP", str(value.get("status", ""))))
        return value


async def observed_target(live: Live, target: str, channel: str, repository: str) -> dict[str, Any]:
    readback = await live.call("auto_coder_deployment_status", {"target_id": target})
    if readback.get("status") != "OK":
        raise Blocked("The configured Supervisor target is not currently observable")
    if (readback.get("target_id"), readback.get("instance_id"), readback.get("channel"), readback.get("repository")) != (target, target, channel, repository):
        raise Failed("Authenticated server's physical target identity differs from the disposable fixture")
    return readback


async def await_advancing_worker_heartbeats(
    live: Live,
    samples: list[tuple[str, str, str, dict[str, Any], str]],
    *,
    timeout_seconds: float = 150.0,
    poll_seconds: float = 5.0,
) -> None:
    """Wait for real producer progress across slow maintenance cycles.

    A missing heartbeat may be retried within the bound, but any change of
    original worker identity or installed source immediately fails closed.
    """
    if not samples or len({target for target, *_ in samples}) != len(samples):
        raise Blocked("Heartbeat targets must be distinct and nonempty")
    deadline = time.monotonic() + timeout_seconds
    advanced: set[str] = set()
    while True:
        for target, channel, repository, before, source_sha in samples:
            after = await observed_target(live, target, channel, repository)
            try:
                require_advancing_worker_heartbeat(before, after, source_sha=source_sha)
            except HeartbeatNotAdvanced:
                advanced.discard(target)
            else:
                advanced.add(target)
        if len(advanced) == len(samples):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Blocked("Original worker heartbeat did not advance within the bounded observation window")
        await asyncio.sleep(min(poll_seconds, remaining))


async def inspect_original(live: Live, checkpoint: smoke.DotCheckpoint, fixture: Fixture) -> Result:
    original = checkpoint.pending_operation_id
    if not original:
        raise Blocked("Exact original pending operation ID is required")
    try:
        target = smoke._original_operation_target(checkpoint, original)
    except ValueError as error:
        raise Blocked("Pending original operation ID does not have one immutable bound target") from error
    if target not in fixture.target_ids.values():
        raise Blocked("Pending original operation targets a nonfixture service")
    value = await live.call("auto_coder_operation_inspect", {"target_id": target, "operation_id": original})
    if value.get("operation_id") != original or value.get("status") in {"NOT_FOUND", "UNAVAILABLE"}:
        raise Blocked("The original journal operation remains unverified; do not rekey or resubmit")
    return Result("BLOCKED", "inspect-original", "Original journal inspected without dispatch; unresolved effects remain held", tuple(live.observations), original)


def scenario_target(fixture: Fixture, scenario: str) -> str:
    return fixture.target_ids["release" if scenario in FULL_SCENARIOS else "beta"]


def load_original_ticket(fixture: Fixture, checkpoint: smoke.DotCheckpoint, scenario: str, *, resume: bool) -> dict[str, object]:
    """Read one original local send; a missing admission ID never authorizes replay."""
    path = ticket_path(scenario)
    if path.is_symlink():
        raise Blocked("Original admission ticket was replaced")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as error:
        raise Blocked("Original admission ticket is unavailable") from error
    if not isinstance(record, dict):
        raise Blocked("Original admission ticket is malformed")
    arguments = record.get("args")
    expected = {
        "schema": 1,
        "fixture_nonce": fixture.fixture_nonce,
        "task_id": checkpoint.task_id,
        "source_sha": checkpoint.source_sha,
        "work_repository": checkpoint.repository,
        "issue_number": checkpoint.issue_number,
        "pr_number": checkpoint.pr_number,
        "action": MUTATIONS[scenario],
        "target": scenario_target(fixture, scenario),
    }
    if any(record.get(key) != value for key, value in expected.items()) or not isinstance(arguments, dict) or arguments.get("target_id") != expected["target"]:
        raise Blocked("Original admission ticket differs from its fixture, work or target")
    if scenario != "reevaluate" and arguments.get("selected_git_sha", arguments.get("source_sha")) != checkpoint.source_sha:
        raise Blocked("Original admission ticket selected a different immutable source")
    operation_id = record.get("operation_id")
    if record.get("effect_state") not in {"ADMITTED", "SUCCEEDED"} or not isinstance(operation_id, str) or smoke.SAFE_ID.fullmatch(operation_id) is None:
        raise Blocked("Original admission outcome lacks its operation ID; never resend or allocate a new key")
    if resume:
        if record.get("stage_revision") != checkpoint.stage_revision or record.get("idempotency_key") != checkpoint.idempotency_key or arguments.get("idempotency_key") != checkpoint.idempotency_key:
            raise Blocked("Original operation cannot be resumed with changed authority or idempotency intent")
        if checkpoint.pending_operation_id and checkpoint.pending_operation_id != operation_id:
            raise Blocked("Original operation cannot be replaced by another pending operation")
        selected_operation = {"beta-source": checkpoint.beta_operation_id, "promote-source": checkpoint.promotion_operation_id, "release-source": checkpoint.release_operation_id}.get(scenario)
        if selected_operation and selected_operation != operation_id:
            raise Blocked("Original operation differs from its saved checkpoint action")
        if scenario in FULL_SCENARIOS and checkpoint.beta_acceptance_ref and (arguments.get("acceptance_ref"), arguments.get("acceptance_oid")) != (checkpoint.beta_acceptance_ref, checkpoint.beta_acceptance_oid):
            raise Blocked("Original selected beta acceptance cannot be changed during resumption")
        if scenario == "beta-acceptance" and checkpoint.beta_operation_id and arguments.get("deployment_operation_id") != checkpoint.beta_operation_id:
            raise Blocked("Original beta acceptance deployment cannot be changed during resumption")
    elif record.get("effect_state") != "SUCCEEDED":
        raise Blocked("A prerequisite original operation has not reached a verified terminal result")
    return record


def original_prerequisite(fixture: Fixture, checkpoint: smoke.DotCheckpoint, scenario: str, selected_id: str = "") -> str:
    if selected_id:
        if smoke.SAFE_ID.fullmatch(selected_id) is None:
            raise Blocked("Original prerequisite operation ID is invalid")
        return selected_id
    return str(load_original_ticket(fixture, checkpoint, scenario, resume=False)["operation_id"])


def verified_deployment(value: dict[str, object], fixture: Fixture, checkpoint: smoke.DotCheckpoint, scenario: str, operation_id: str) -> None:
    expected = (operation_id, scenario_target(fixture, scenario), MUTATIONS[scenario].removeprefix("auto_coder_"), checkpoint.source_sha)
    if (value.get("operation_id"), value.get("target_id"), value.get("action"), value.get("source_sha")) != expected:
        raise Failed("Original deployment receipt differs from its exact source or target")
    if value.get("status") != "SUCCEEDED" or value.get("phase") != "succeeded" or value.get("effect_state") != "confirmed":
        raise Blocked("Original deployment has no confirmed terminal lifecycle outcome")


async def acceptance_evidence(live: Live, fixture: Fixture, checkpoint: smoke.DotCheckpoint, *, operation_id: str = "") -> dict[str, object]:
    prefix = "refs/tags/auto-coder-beta-acceptance-"
    if not operation_id and checkpoint.beta_acceptance_ref:
        if not checkpoint.beta_acceptance_ref.startswith(prefix):
            raise Blocked("Original acceptance ref is outside the immutable beta namespace")
        operation_id = checkpoint.beta_acceptance_ref.removeprefix(prefix)
    operation_id = original_prerequisite(fixture, checkpoint, "beta-acceptance", operation_id)
    value = await live.call("auto_coder_beta_acceptance_get", {"target_id": fixture.target_ids["beta"], "operation_id": operation_id})
    evidence = verified_acceptance_receipt(value, operation_id=operation_id, target_id=fixture.target_ids["beta"], source_sha=checkpoint.source_sha)
    if checkpoint.beta_acceptance_ref and (checkpoint.beta_acceptance_ref, checkpoint.beta_acceptance_oid) != (evidence["acceptance_ref"], evidence["acceptance_oid"]):
        raise Failed("Current acceptance differs from the original selected checkpoint ref/OID")
    return await asyncio.to_thread(verify_published_acceptance, checkpoint, fixture, evidence)


async def promotion_evidence(live: Live, fixture: Fixture, checkpoint: smoke.DotCheckpoint, *, operation_id: str = "") -> dict[str, object]:
    operation_id = original_prerequisite(fixture, checkpoint, "promote-source", operation_id or checkpoint.promotion_operation_id)
    value = await live.call("auto_coder_operation_inspect", {"target_id": fixture.target_ids["release"], "operation_id": operation_id})
    evidence = verified_promotion_receipt(value, operation_id=operation_id, target_id=fixture.target_ids["release"], source_sha=checkpoint.source_sha)
    confirm_saved_promotion(checkpoint, evidence)
    accepted = await acceptance_evidence(live, fixture, checkpoint)
    return await asyncio.to_thread(verify_published_promotion, checkpoint, fixture, evidence, accepted)


def worker_arguments(snapshot: dict[str, object]) -> dict[str, object]:
    worker = snapshot.get("worker")
    if snapshot.get("alive") is not True or not isinstance(worker, dict):
        raise Blocked("No real running child to fence an original source operation")
    arguments = {
        "expected_supervisor_generation": snapshot.get("supervisor_generation"),
        "expected_runtime_id": worker.get("runtime_id"),
        "expected_process_run_id": worker.get("process_run_id"),
        "expected_deployment_generation": worker.get("deployment_generation"),
    }
    if any(not isinstance(value, str) or smoke.SAFE_ID.fullmatch(value) is None for value in arguments.values()):
        raise Blocked("Original live worker fencing is incomplete")
    return arguments


async def prepare_arguments(live: Live, fixture: Fixture, checkpoint: smoke.DotCheckpoint, scenario: str, item_number: int) -> dict[str, object]:
    target = scenario_target(fixture, scenario)
    arguments: dict[str, object] = {"target_id": target, "idempotency_key": checkpoint.idempotency_key}
    if scenario == "promote-source":
        source = await asyncio.to_thread(verify_real_source, checkpoint, fixture)
        accepted = await acceptance_evidence(live, fixture, checkpoint)
        return {
            **arguments,
            "source_sha": checkpoint.source_sha,
            "ci_run_id": int(checkpoint.ci_run_id),
            "ci_run_attempt": int(checkpoint.ci_run_attempt),
            "tested_source_ref": checkpoint.tested_source_ref,
            "tested_source_oid": checkpoint.tested_source_oid,
            "tested_source_record_sha256": source["tested_source_record_sha256"],
            "acceptance_ref": accepted["acceptance_ref"],
            "acceptance_oid": accepted["acceptance_oid"],
        }
    if scenario == "release-source":
        promoted = await promotion_evidence(live, fixture, checkpoint)
        release = await observed_target(live, target, "release", TEST_REPOSITORY)
        return {**arguments, **worker_arguments(release), "selected_git_sha": checkpoint.source_sha, **{key: promoted[key] for key in ("promotion_operation_id", "catalog_ref", "catalog_oid", "acceptance_ref", "acceptance_oid")}}
    beta = await observed_target(live, target, "beta", BETA_WORK_REPOSITORY)
    fence = worker_arguments(beta)
    if scenario == "reevaluate":
        if type(item_number) is not int or not 1 <= item_number <= 999999999:
            raise Blocked("A positive test-repository Issue number is required")
        return {**arguments, "target_type": "issue", "number": item_number, **{key: fence[key] for key in ("expected_process_run_id", "expected_deployment_generation")}}
    await asyncio.to_thread(verify_real_source, checkpoint, fixture)
    if scenario == "beta-source":
        selected = await live.call("auto_coder_beta_source_readback", {"target_id": target})
        if selected.get("status") != "OK" or (selected.get("source_sha"), selected.get("tested_source_oid")) != (checkpoint.source_sha, checkpoint.tested_source_oid):
            raise Blocked("The selected test-only beta Git source does not match original immutable proof")
        return {**arguments, **fence, "selected_git_sha": checkpoint.source_sha}
    positive_heartbeat(beta, source_sha=checkpoint.source_sha)
    original = original_prerequisite(fixture, checkpoint, "beta-source", checkpoint.beta_operation_id)
    deployed = await live.call("auto_coder_operation_inspect", {"target_id": target, "operation_id": original})
    verified_deployment(deployed, fixture, checkpoint, "beta-source", original)
    worker = deployed.get("new_worker")
    if not isinstance(worker, dict) or any(worker.get(key) != fence["expected_" + key] for key in ("runtime_id", "process_run_id", "deployment_generation")):
        raise Blocked("Current beta worker differs from the original source deployment")
    return {
        **arguments,
        "selected_git_sha": checkpoint.source_sha,
        "installed_runtime_id": fence["expected_runtime_id"],
        "process_run_id": fence["expected_process_run_id"],
        "deployment_generation": fence["expected_deployment_generation"],
        "supervisor_generation": fence["expected_supervisor_generation"],
        "deployment_operation_id": original,
    }


async def complete_original(live: Live, fixture: Fixture, checkpoint: smoke.DotCheckpoint, scenario: str, original_id: str, arguments: dict[str, object], *, wait_seconds: float) -> Result:
    target = scenario_target(fixture, scenario)
    deadline = time.monotonic() + wait_seconds
    while True:
        name = "auto_coder_beta_acceptance_get" if scenario == "beta-acceptance" else "auto_coder_operation_inspect"
        original = await live.call(name, {"target_id": target, "operation_id": original_id})
        if original.get("operation_id") != original_id:
            raise Blocked("Original operation readback is unbound or unavailable")
        if (original.get("action"), original.get("target_id")) != (MUTATIONS[scenario].removeprefix("auto_coder_"), target):
            raise Blocked("Original operation readback does not bind its saved action and target")
        details: dict[str, object] | None = None
        positive = False
        if scenario == "reevaluate":
            if verified_reevaluation_receipt(original, operation_id=original_id, target_id=target, process_run_id=str(arguments["expected_process_run_id"])):
                details = {"reevaluation_accepted": True, "issue_completion_proven": False}
        elif scenario == "beta-acceptance" and original.get("status") == "PASS_PUBLISHED":
            details = verified_acceptance_receipt(original, operation_id=original_id, target_id=target, source_sha=checkpoint.source_sha)
            details = await asyncio.to_thread(verify_published_acceptance, checkpoint, fixture, details)
        elif scenario == "promote-source" and original.get("status") == "SUCCEEDED":
            promoted = verified_promotion_receipt(original, operation_id=original_id, target_id=target, source_sha=checkpoint.source_sha)
            confirm_saved_promotion(checkpoint, promoted)
            accepted = {"beta_acceptance_operation_id": str(arguments["acceptance_ref"]).removeprefix("refs/tags/auto-coder-beta-acceptance-"), "acceptance_ref": arguments["acceptance_ref"], "acceptance_oid": arguments["acceptance_oid"]}
            await asyncio.to_thread(verify_published_acceptance, checkpoint, fixture, accepted)
            details = await asyncio.to_thread(verify_published_promotion, checkpoint, fixture, promoted, accepted)
        elif scenario in {"beta-source", "release-source"} and original.get("status") == "SUCCEEDED":
            verified_deployment(original, fixture, checkpoint, scenario, original_id)
            if scenario == "release-source":
                expected = {key: arguments[key] for key in ("promotion_operation_id", "catalog_ref", "catalog_oid", "acceptance_ref", "acceptance_oid")}
                if original.get("release_identity") != {**expected, "source_sha": checkpoint.source_sha}:
                    raise Failed("Release worker operation does not bind the original promotion and acceptance")
                # Recheck Git selection after activation; a superseded promotion
                # cannot become a successful current full release.
                await promotion_evidence(live, fixture, checkpoint, operation_id=str(arguments["promotion_operation_id"]))
            role = "release" if scenario == "release-source" else "beta"
            workload_repository = TEST_REPOSITORY if role == "release" else BETA_WORK_REPOSITORY
            installed = await observed_target(live, target, role, workload_repository)
            worker = installed.get("worker")
            recorded_worker = original.get("new_worker")
            if not isinstance(worker, dict) or not isinstance(recorded_worker, dict) or any(worker.get(key) != recorded_worker.get(key) for key in ("runtime_id", "process_run_id", "deployment_generation")):
                raise Blocked("Current installed worker is not the original operation's confirmed child")
            await await_advancing_worker_heartbeats(live, [(target, role, workload_repository, installed, checkpoint.source_sha)])
            details = {"source_sha": checkpoint.source_sha, "target_id": target, "worker": worker, "supervisor_generation": installed.get("supervisor_generation"), "release_worker_deployed": role == "release"}
            positive = True
        if details is not None:
            update_original_ticket(ticket_path(scenario), fixture, original_id, "SUCCEEDED", evidence=details)
            reasons = {
                "reevaluate": "The genuine worker durably accepted reevaluation; Issue work was not certified complete",
                "beta-source": "Real beta worker installed the original tested source and advanced producer heartbeats",
                "beta-acceptance": "Original beta runtime acceptance was independently published and read back from Git",
                "promote-source": "Exact source promotion and Actions/catalog readback are confirmed; release worker activation remains separate",
                "release-source": "Independent release worker activation matches the original promotion and advances real producer heartbeats",
            }
            return Result("PASS", scenario, reasons[scenario], tuple(live.observations), original_id, positive_deployment_proven=positive, effect_dispatched=True, original_ticket_state="SUCCEEDED", details=details)
        status = original.get("status")
        if status in {"FAILED", "UNKNOWN", "UNAVAILABLE", "NOT_FOUND", "ROLLED_BACK"}:
            raise Blocked("Original operation failed or is unavailable; never replay or allocate a new key")
        if scenario == "promote-source" and original.get("effect_state") == "sent_unknown":
            # Source promotion has no recurring service reconciler. Its explicit
            # original query only observes the already-dispatched Actions run;
            # the producer's durable SENT_UNKNOWN fence prevents dispatch again.
            live.authorize_original_reconciliation(target, original_id)
            await live.call("auto_coder_operation_get", {"target_id": target, "operation_id": original_id}, mutation=True)
        elif status == "NEEDS_ATTENTION":
            raise Blocked("Original worker operation needs attention; preserve its original ID and held lane")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Blocked("Original operation has not completed within the bounded wait; use explicit original resumption")
        await asyncio.sleep(min(2.0, remaining))


async def run_authenticated(fixture: Fixture, checkpoint: smoke.DotCheckpoint, scenario: str, *, mutate: bool, item_number: int, resume_original: bool = False, wait_seconds: float = 300.0) -> Result:
    bearer = os.environ.get("AUTO_CODER_MCP_ACCESS_TOKEN", "")
    if not bearer:
        raise Blocked("No current fixture-specific OAuth access bearer; no MCP request sent")
    if type(wait_seconds) not in {int, float} or not 0 < wait_seconds <= 900:
        raise Blocked("Original operation wait must be finite and at most 900 seconds")
    if mutate and (checkpoint.beta_target != fixture.target_ids["beta"] or (scenario in FULL_SCENARIOS and checkpoint.release_target != fixture.target_ids["release"])):
        raise Blocked("Original checkpoint target differs from its exact disposable fixture")
    # Resolve local originals before opening transport. An unknown admission
    # without its original operation ID cannot be recovered by a second send.
    retained = load_original_ticket(fixture, checkpoint, scenario, resume=True) if resume_original else None
    if mutate and not resume_original and (ticket_path(scenario).exists() or ticket_path(scenario).is_symlink()):
        raise Blocked("A previous admission exists; select explicit original resumption, never replay")
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(fixture.endpoint, headers={"Authorization": "Bearer " + bearer}, timeout=15, sse_read_timeout=30) as (reader, writer, _):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            live = Live(session, {tool.name for tool in (await session.list_tools()).tools})
            identity = await live.call("auto_coder_whoami", {})
            verify_current_operator(identity, fixture, scenario, mutation=mutate)
            if scenario == "inspect-original":
                # Journal recovery must not depend on the failed worker or an
                # unrelated beta Supervisor still being reachable.
                return await inspect_original(live, checkpoint, fixture)
            targets = await live.call("auto_coder_targets", {})
            verify_current_targets(targets, fixture)
            if not mutate:
                beta = await observed_target(live, fixture.target_ids["beta"], "beta", BETA_WORK_REPOSITORY)
                release = await observed_target(live, fixture.target_ids["release"], "release", TEST_REPOSITORY)
                await await_advancing_worker_heartbeats(live, [(fixture.target_ids["beta"], "beta", BETA_WORK_REPOSITORY, beta, checkpoint.source_sha), (fixture.target_ids["release"], "release", TEST_REPOSITORY, release, "")])
                return Result("PASS", "observe", "Two independent installed workers advanced real heartbeat sequences; no effect sent", tuple(live.observations))
            if retained is not None:
                return await complete_original(live, fixture, checkpoint, scenario, str(retained["operation_id"]), retained["args"], wait_seconds=wait_seconds)
            await check_retained_recovery(live, fixture, checkpoint, MUTATIONS[scenario], scenario_target(fixture, scenario), root=ticket_path(scenario).parent)
            arguments = await prepare_arguments(live, fixture, checkpoint, scenario, item_number)
            original_ticket = ticket_path(scenario)
            persist_original_ticket(original_ticket, fixture, checkpoint, MUTATIONS[scenario], arguments)
            admitted = await live.call(MUTATIONS[scenario], arguments, mutation=True)
            if admitted.get("status") in {"CONFLICT", "BUSY", "DENIED", "INVALID_REQUEST", "UNAVAILABLE", "NOT_FOUND"}:
                # BUSY/CONFLICT can carry a different original operation ID.
                # Never bind that occupying operation to this durable send.
                raise Blocked("Admission was refused or unavailable; no returned occupying operation may replace the original send")
            if (admitted.get("action"), admitted.get("target_id")) != (MUTATIONS[scenario].removeprefix("auto_coder_"), arguments["target_id"]):
                raise Blocked("Admission response does not bind this exact original action and target")
            if scenario in {"beta-source", "release-source"} and admitted.get("source_sha") != checkpoint.source_sha:
                raise Blocked("Admission response does not bind this exact original selected source")
            original_id = admitted.get("operation_id")
            if not isinstance(original_id, str) or smoke.SAFE_ID.fullmatch(original_id) is None:
                raise Blocked("Admission response is missing its original operation ID; durable ticket remains held")
            update_original_ticket(original_ticket, fixture, original_id, "ADMITTED")
            return await complete_original(live, fixture, checkpoint, scenario, original_id, arguments, wait_seconds=wait_seconds)


def _logging() -> None:
    logger.remove()
    logger.add(sys.stdout, level="INFO")
    state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "auto-coder"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    logger.add(state / "dot-mcp-acceptance.log", rotation="2 MB", retention=3)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-manifest", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="observe")
    parser.add_argument("--enable-fixture-mutation", action="store_true")
    parser.add_argument("--resume-original", action="store_true", help="Reconcile the exact durable original operation without repeating its admission")
    parser.add_argument("--wait-seconds", type=int, default=300, help="Bounded original operation observation, maximum 900 seconds")
    parser.add_argument("--operator-stage", default="")
    parser.add_argument("--operator-stage-revision", default="")
    parser.add_argument("--fixture-issue-number", type=int, default=0)
    args = parser.parse_args()
    _logging()
    if args.fixture_manifest is None:
        report = Result("NOT_RUN", args.scenario, "No disposable fixture selected; no network or mutation")
    else:
        fixture: Fixture | None = None
        try:
            fixture = Fixture.load(args.fixture_manifest)
            bound_local_fixture(fixture)
            checkpoint = smoke.load_checkpoint(args.checkpoint)
            original_stage(checkpoint, args.scenario, args.operator_stage, args.operator_stage_revision, mutation=args.enable_fixture_mutation, resume_original=args.resume_original)
            if args.enable_fixture_mutation and (not fixture.source_fixture_available or checkpoint.source_sha != fixture.source_fixture_sha):
                raise Blocked("Mutation source must be the immutable test-only Git fixture")
            report = asyncio.run(run_authenticated(fixture, checkpoint, args.scenario, mutate=args.enable_fixture_mutation, item_number=args.fixture_issue_number, resume_original=args.resume_original, wait_seconds=args.wait_seconds))
        except Blocked as error:
            report = exception_result(args.scenario, str(error), mutation=args.enable_fixture_mutation, fixture=fixture)
        except Failed as error:
            report = exception_result(args.scenario, str(error), mutation=args.enable_fixture_mutation, verdict="FAIL", fixture=fixture)
        except ExceptionGroup as error:
            nested = _single_nested_acceptance_error(error)
            if isinstance(nested, Blocked):
                report = exception_result(args.scenario, str(nested), mutation=args.enable_fixture_mutation, fixture=fixture)
            elif isinstance(nested, Failed):
                report = exception_result(args.scenario, str(nested), mutation=args.enable_fixture_mutation, verdict="FAIL", fixture=fixture)
            else:
                logger.warning("Untrusted task-group failure classified as {}", type(error).__name__)
                report = exception_result(args.scenario, "Authenticated fixture transport unavailable; retain any original send ticket", mutation=args.enable_fixture_mutation, fixture=fixture)
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            logger.warning("Fixture acceptance unavailable due to {}", type(error).__name__)
            report = exception_result(args.scenario, "Original fixture, authority or producer evidence unavailable", mutation=args.enable_fixture_mutation, fixture=fixture)
        except Exception as error:
            logger.warning("Untrusted transport failure classified as {}", type(error).__name__)
            report = exception_result(args.scenario, "Authenticated fixture transport unavailable; retain any original send ticket", mutation=args.enable_fixture_mutation, fixture=fixture)
    logger.info("Disposable MCP acceptance scenario={} verdict={} effect_dispatched={}", report.scenario, report.verdict, report.effect_dispatched)
    print(json.dumps(asdict(report), sort_keys=True))
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 2, "NOT_RUN": 3}[report.verdict]


if __name__ == "__main__":
    raise SystemExit(main())
