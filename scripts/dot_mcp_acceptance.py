#!/usr/bin/env python3
"""Opt-in, disposable-only MCP acceptance probe with durable send fencing.

The fixture's Docker provisioning is an external operator action. This client
never imports or invokes Docker, changes operator policy, or grants OAuth.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import os
import re
import secrets
import stat
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from loguru import logger

# Support both direct script execution and package imports.
if __package__:
    from . import management_mcp_rollout_smoke as smoke
else:
    import management_mcp_rollout_smoke as smoke

TEST_REPOSITORY = "kitamura-tetsuo/auto-coder-test"
SHA = re.compile(r"[0-9a-f]{40}\Z")
NONCE = re.compile(r"[0-9a-f]{32}\Z")
FIXTURE = re.compile(r"pr[0-9]{3,6}-[a-z0-9-]{6,28}\Z")
SCENARIOS = frozenset({"observe", "reevaluate", "beta-source", "inspect-original"})
MUTATIONS = {
    "reevaluate": "auto_coder_reevaluate_target",
    "beta-source": "auto_coder_deploy_beta_source",
}
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
        fixture.target_ids["beta"]: ("beta", TEST_REPOSITORY),
        fixture.target_ids["release"]: ("release", "fixture/" + fixture.fixture_id + "-release"),
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


def original_stage(checkpoint: smoke.DotCheckpoint, scenario: str, selected_stage: str, selected_revision: str, *, mutation: bool) -> None:
    if scenario not in SCENARIOS:
        raise Blocked("Unsupported acceptance scenario")
    if not mutation:
        if scenario not in {"observe", "inspect-original"}:
            raise Blocked("Read-only is the default; mutation requires explicit selection")
        return
    if scenario not in MUTATIONS:
        raise Blocked("No mutating action is available for this scenario")
    if checkpoint.stage not in {"beta", "full"}:
        raise Blocked("Observe stage can never dispatch an effect")
    try:
        smoke.confirm_operator_scope(checkpoint, selected_stage, selected_revision)
    except smoke.OperatorScopeError as error:
        raise Blocked(str(error)) from error
    required = smoke.required_next_observation(checkpoint)
    if required.verdict == "BLOCKED":
        raise Blocked(required.reason)
    if checkpoint.pending_effect_state or checkpoint.pending_operation_id or checkpoint.recovery_attempts:
        raise Blocked("An original unresolved effect or exhausted recovery budget forbids any new send")
    if not checkpoint.idempotency_key or smoke.SAFE_ID.fullmatch(checkpoint.idempotency_key) is None:
        raise Blocked("Persist the immutable original idempotency key before admission")


def ticket_path(scenario: str) -> Path:
    if scenario not in MUTATIONS:
        raise Blocked("Only a fixed disposable mutation may reserve a send ticket")
    return Path("/fixture/management") / f"dot-fixture-{scenario}-original-send.json"


def persist_original_ticket(path: Path, fixture: Fixture, checkpoint: smoke.DotCheckpoint, action: str, args: dict[str, object]) -> None:
    """Fsync original nonsecret intent before an effect, refusing all replays."""
    if path.exists() or path.is_symlink():
        raise Blocked("A previous admission may have been sent; reconcile its original ticket before retry")
    for existing_path in path.parent.glob("dot-fixture-*-original-send.json"):
        try:
            previous = json.loads(existing_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as error:
            raise Blocked("A previous original send ticket cannot be checked") from error
        if not isinstance(previous, dict) or previous.get("fixture_nonce") != fixture.fixture_nonce:
            raise Blocked("A previous ticket has an untrusted fixture identity")
        if previous.get("target") == fixture.target_ids["beta"] and previous.get("idempotency_key") == checkpoint.idempotency_key:
            raise Blocked("Original idempotency key cannot be reused for a different action on one target")
    record = {
        "schema": 1,
        "fixture_nonce": fixture.fixture_nonce,
        "work_repository": checkpoint.repository,
        "issue_number": checkpoint.issue_number,
        "pr_number": checkpoint.pr_number,
        "stage_revision": checkpoint.stage_revision,
        "action": action,
        "target": fixture.target_ids["beta"],
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


def update_original_ticket(path: Path, fixture: Fixture, operation_id: str, state: str) -> None:
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
    temporary = path.parent / (".dot-original-" + secrets.token_hex(8))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(updated, file, sort_keys=True, separators=(",", ":"))
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


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
    sequence = heartbeat.get("sequence")
    if heartbeat.get("available") is not True or type(sequence) is not int or sequence < 0:
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

    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate original receipt field")
            result[key] = item
        return result

    try:
        detail = json.loads(receipt["result"], object_pairs_hook=unique_pairs)
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


def verify_real_source(checkpoint: smoke.DotCheckpoint, fixture: Fixture) -> None:
    """Read the original public Git ref, annotated object and source Actions attempt."""
    if not fixture.source_fixture_available or checkpoint.source_sha != fixture.source_fixture_sha:
        raise Blocked("Source under test is not the fixture's originally pinned main commit")
    from auto_coder.tested_source_refs import TestedSourcePublisher
    from auto_coder.util.gh_cache import GitHubGitDataClient

    publisher = TestedSourcePublisher(GitHubGitDataClient("", TEST_REPOSITORY), TEST_REPOSITORY)
    proof = publisher.read(checkpoint.source_sha)
    if proof is None:
        raise Blocked("The exact immutable tested-source Git ref has not been published")
    record = proof.record
    if (proof.tag_ref, proof.tag_object_oid) != (checkpoint.tested_source_ref, checkpoint.tested_source_oid):
        raise Failed("The test repository's annotated proof ref/OID differs from original checkpoint")
    if (str(record.run_id), str(record.run_attempt)) != (checkpoint.ci_run_id, checkpoint.ci_run_attempt):
        raise Failed("Original source GitHub Actions run/attempt differs from published immutable proof")
    publisher.verify_run(record, recorded_attempt=True)


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


class Live:
    def __init__(self, session: Any, registered: set[str]):
        self.session = session
        self.registered = registered
        self.observations: list[Evidence] = []

    async def call(self, name: str, arguments: dict[str, object], *, mutation: bool = False) -> dict[str, Any]:
        if name not in self.registered:
            raise Blocked("Required MCP producer tool is not registered: " + name)
        if mutation:
            if name not in MUTATIONS.values():
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


async def run_authenticated(fixture: Fixture, checkpoint: smoke.DotCheckpoint, scenario: str, *, mutate: bool, item_number: int) -> Result:
    bearer = os.environ.get("AUTO_CODER_MCP_ACCESS_TOKEN", "")
    if not bearer:
        raise Blocked("No current fixture-specific OAuth access bearer; no MCP request sent")
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(fixture.endpoint, headers={"Authorization": "Bearer " + bearer}, timeout=15, sse_read_timeout=30) as (reader, writer, _):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            live = Live(session, {tool.name for tool in (await session.list_tools()).tools})
            identity = await live.call("auto_coder_whoami", {})
            verify_current_operator(identity, fixture, scenario, mutation=mutate)
            targets = await live.call("auto_coder_targets", {})
            expected = {
                fixture.target_ids["beta"]: ("beta", TEST_REPOSITORY),
                fixture.target_ids["release"]: ("release", "fixture/" + fixture.fixture_id + "-release"),
            }
            actual = {entry["target_id"]: (entry["channel"], entry["repository"]) for entry in targets.get("targets", []) if isinstance(entry, dict) and all(name in entry for name in ("target_id", "channel", "repository"))}
            if targets.get("status") != "OK" or actual != expected:
                raise Blocked("The current policy target map cannot prove the two disposable workers")
            beta = await observed_target(live, fixture.target_ids["beta"], "beta", TEST_REPOSITORY)
            if scenario == "inspect-original":
                return await inspect_original(live, checkpoint, fixture)
            if not mutate:
                release = await observed_target(live, fixture.target_ids["release"], "release", expected[fixture.target_ids["release"]][1])
                await await_advancing_worker_heartbeats(
                    live,
                    [
                        (fixture.target_ids["beta"], "beta", TEST_REPOSITORY, beta, checkpoint.source_sha),
                        (fixture.target_ids["release"], "release", expected[fixture.target_ids["release"]][1], release, ""),
                    ],
                )
                return Result("PASS", "observe", "Two independent installed workers advanced real heartbeat sequences; no effect sent", tuple(live.observations))
            if scenario == "beta-source":
                await asyncio.to_thread(verify_real_source, checkpoint, fixture)
                selected = await live.call("auto_coder_beta_source_readback", {"target_id": fixture.target_ids["beta"]})
                if selected.get("status") != "OK" or (selected.get("source_sha"), selected.get("tested_source_oid")) != (checkpoint.source_sha, checkpoint.tested_source_oid):
                    raise Blocked("The selected test-only beta Git source does not match original immutable proof")
            else:
                if type(item_number) is not int or item_number < 1 or item_number > 999999999:
                    raise Blocked("A positive test-repository Issue number is required")
            worker = beta.get("worker")
            if beta.get("alive") is not True or not isinstance(worker, dict):
                raise Blocked("No real running beta child to fence a mutation")
            generation = beta.get("supervisor_generation")
            if not isinstance(generation, str) or not generation:
                raise Blocked("Signed Supervisor generation is unavailable")
            if scenario == "reevaluate":
                arguments: dict[str, object] = {
                    "target_id": fixture.target_ids["beta"],
                    "target_type": "issue",
                    "number": item_number,
                    "idempotency_key": checkpoint.idempotency_key,
                    "expected_process_run_id": worker.get("process_run_id"),
                    "expected_deployment_generation": worker.get("deployment_generation"),
                }
            else:
                arguments = {
                    "target_id": fixture.target_ids["beta"],
                    "selected_git_sha": checkpoint.source_sha,
                    "idempotency_key": checkpoint.idempotency_key,
                    "expected_supervisor_generation": generation,
                    "expected_runtime_id": worker.get("runtime_id"),
                    "expected_process_run_id": worker.get("process_run_id"),
                    "expected_deployment_generation": worker.get("deployment_generation"),
                }
            if any(not isinstance(value, (str, int)) or value in {"", None} for value in arguments.values()):
                raise Blocked("Original live worker fencing or immutable intent is incomplete")
            original_ticket = ticket_path(scenario)
            persist_original_ticket(original_ticket, fixture, checkpoint, MUTATIONS[scenario], arguments)
            admitted = await live.call(MUTATIONS[scenario], arguments, mutation=True)
            original_id = admitted.get("operation_id")
            if not isinstance(original_id, str) or smoke.SAFE_ID.fullmatch(original_id) is None:
                raise Blocked("Admission response is missing its original operation ID; durable ticket remains held")
            update_original_ticket(original_ticket, fixture, original_id, "ADMITTED")
            for _ in range(30):
                original = await live.call("auto_coder_operation_inspect", {"target_id": fixture.target_ids["beta"], "operation_id": original_id})
                if original.get("operation_id") != original_id:
                    raise Blocked("Original operation readback is unbound or unavailable")
                if original.get("status") in {"NEEDS_ATTENTION", "FAILED", "UNKNOWN", "UNAVAILABLE"}:
                    raise Blocked("Original worker operation is held or failed; never allocate a new key")
                if scenario == "reevaluate":
                    if verified_reevaluation_receipt(
                        original,
                        operation_id=original_id,
                        target_id=fixture.target_ids["beta"],
                        process_run_id=str(worker["process_run_id"]),
                    ):
                        update_original_ticket(original_ticket, fixture, original_id, "SUCCEEDED")
                        return Result("PASS", scenario, "The genuine worker durably accepted reevaluation; Issue work was not certified complete", tuple(live.observations), original_id, effect_dispatched=True)
                elif original.get("status") == "SUCCEEDED":
                    if original.get("action") != "deploy_beta_source" or original.get("target_id") != fixture.target_ids["beta"] or original.get("phase") != "succeeded" or original.get("source_sha") != checkpoint.source_sha:
                        raise Blocked("Terminal beta deployment is not bound to the original tested source and target")
                    update_original_ticket(original_ticket, fixture, original_id, "SUCCEEDED")
                    installed = await observed_target(live, fixture.target_ids["beta"], "beta", TEST_REPOSITORY)
                    await await_advancing_worker_heartbeats(
                        live,
                        [(fixture.target_ids["beta"], "beta", TEST_REPOSITORY, installed, checkpoint.source_sha)],
                    )
                    return Result("PASS", scenario, "Real worker installed the original tested source and advanced producer heartbeats", tuple(live.observations), original_id, positive_deployment_proven=True, effect_dispatched=True)
                await asyncio.sleep(2)
            raise Blocked("Original management operation has not reached a verified terminal receipt")


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
            original_stage(checkpoint, args.scenario, args.operator_stage, args.operator_stage_revision, mutation=args.enable_fixture_mutation)
            if args.enable_fixture_mutation and (not fixture.source_fixture_available or checkpoint.source_sha != fixture.source_fixture_sha):
                raise Blocked("Mutation source must be the immutable test-only Git fixture")
            report = asyncio.run(run_authenticated(fixture, checkpoint, args.scenario, mutate=args.enable_fixture_mutation, item_number=args.fixture_issue_number))
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
