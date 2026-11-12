#!/usr/bin/env python3
"""Observe real disposable refusal/recovery boundaries without granting authority.

Only the two explicit refusal probes send a fixed reevaluation request. They
require an operator-selected observe policy and a durable pre-send ticket.
All other scenarios inspect the original operation and never retry or recover it.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from loguru import logger

if __package__:
    from . import dot_mcp_acceptance as accept
else:
    import dot_mcp_acceptance as accept

smoke = accept.smoke
SCENARIOS = frozenset({"observe-refusal", "policy-downgrade", "held-drain", "unknown-effect", "repeated-failure", "data-rollback"})
REFUSAL_PROBES = frozenset({"observe-refusal", "policy-downgrade"})
ROOT = Path("/fixture/management")
POLICY = Path("/fixture/policy/operating-policy.yaml")
JOURNAL = ROOT / "operations/management-operations.sqlite3"


@dataclass(frozen=True)
class NegativeReport:
    verdict: str
    scenario: str
    reason: str
    observations: tuple[accept.Evidence, ...] = ()
    operation_id: str = ""
    negative_boundary_verified: bool = False
    positive_deployment_proven: bool = False
    effect_dispatched: bool | None = False
    failure_signature: str = ""


@dataclass(frozen=True)
class FailureIdentity:
    action: str
    target: str
    source_sha: str
    reason: str

    @property
    def signature(self) -> str:
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def refusal_ticket(scenario: str, root: Path = ROOT) -> Path:
    if scenario not in REFUSAL_PROBES:
        raise accept.Blocked("Only an explicitly selected refusal probe has a send ticket")
    return root / f"dot-fixture-{scenario}-original-send.json"


def selected_checkpoint(fixture: accept.Fixture, checkpoint: smoke.DotCheckpoint, scenario: str, stage: str, revision: str, *, probe: bool) -> None:
    """Keep test intent, current operator selection and old unknowns separate."""
    if scenario not in SCENARIOS:
        raise accept.Blocked("Unsupported negative acceptance scenario")
    try:
        smoke.confirm_operator_scope(checkpoint, stage, revision)
    except smoke.OperatorScopeError as error:
        raise accept.Blocked(str(error)) from error
    if (checkpoint.beta_target, checkpoint.release_target) != (fixture.target_ids["beta"], fixture.target_ids["release"]):
        raise accept.Blocked("The checkpoint must retain both original disposable targets")
    if checkpoint.source_sha != fixture.source_fixture_sha or not fixture.source_fixture_available:
        raise accept.Blocked("The negative scenario must retain the exact disposable source")
    if scenario in REFUSAL_PROBES:
        if not probe:
            raise accept.Blocked("An actual refusal probe requires explicit operator selection")
        if checkpoint.pending_effect_state or checkpoint.pending_operation_id or checkpoint.recovery_attempts:
            raise accept.Blocked("A refusal probe cannot bypass an original unresolved effect or exhausted recovery")
        if not checkpoint.task_id or not checkpoint.repository or not (checkpoint.issue_number or checkpoint.pr_number) or not checkpoint.stage_revision:
            raise accept.Blocked("Persist the original work and operator revision before a refusal probe")
        if not checkpoint.idempotency_key or smoke.SAFE_ID.fullmatch(checkpoint.idempotency_key) is None:
            raise accept.Blocked("Persist a fixed original refusal-probe idempotency key")
    elif probe:
        raise accept.Blocked("This original-operation scenario is strictly read-only")


def observe_policy_revision(fixture: accept.Fixture, path: Path = POLICY) -> str:
    """Read the mounted operator policy without modifying or granting anything."""
    from auto_coder.operating_policy import PolicyProfile, load_operating_policy

    loaded = load_operating_policy(path)
    policy = loaded.policy
    if policy is None or policy.profile is not PolicyProfile.OBSERVE:
        raise accept.Blocked("The external operator has not selected the required observe policy")
    expected = {(fixture.target_ids["beta"], "beta", (accept.BETA_WORK_REPOSITORY,)), (fixture.target_ids["release"], "release", (accept.TEST_REPOSITORY,))}
    actual = {(target.target_id, target.channel.value, target.repositories) for target in policy.targets}
    if actual != expected or policy.management_self_update is not None:
        raise accept.Blocked("The mounted operator policy is not the exact two-target disposable profile")
    return policy.revision


def journal_original_key(path: Path, target: str, key: str) -> tuple[str, ...]:
    """Read actual admission rows independently, with SQLite in read-only mode."""
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as connection:
            connection.execute("PRAGMA query_only=ON")
            rows = connection.execute("SELECT operation_id,intent_json FROM operations").fetchall()
        found = []
        for operation_id, encoded in rows:
            intent = json.loads(encoded)
            if not isinstance(intent, dict) or not isinstance(intent.get("target"), list) or len(intent["target"]) != 3:
                raise ValueError("invalid original journal intent")
            if intent["target"][0] == target and intent.get("idempotency_key") == key:
                found.append(str(operation_id))
        return tuple(sorted(found))
    except (OSError, sqlite3.Error, ValueError, TypeError) as error:
        raise accept.Blocked("The original admission journal cannot independently prove absence") from error


def _journal_absence_digest(target: str, key: str, found: tuple[str, ...]) -> str:
    payload = {"target": target, "idempotency_key": key, "operation_ids": found}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def prior_reevaluation(live: accept.Live, fixture: accept.Fixture, root: Path) -> None:
    """A downgrade requires a real previous acceptance, not a saved profile name."""
    try:
        record = json.loads((root / "dot-fixture-reevaluate-original-send.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise accept.Blocked("No original successful reevaluation ticket proves the earlier authority") from error
    arguments = record.get("args") if isinstance(record, dict) else None
    if (
        not isinstance(record, dict)
        or record.get("fixture_nonce") != fixture.fixture_nonce
        or record.get("target") != fixture.target_ids["beta"]
        or record.get("action") != "auto_coder_reevaluate_target"
        or record.get("effect_state") != "SUCCEEDED"
        or not isinstance(record.get("operation_id"), str)
        or not record["operation_id"]
        or not isinstance(arguments, dict)
        or not isinstance(arguments.get("expected_process_run_id"), str)
    ):
        raise accept.Blocked("The original successful reevaluation is not bound to this fixture")
    original = await live.call("auto_coder_operation_inspect", {"target_id": fixture.target_ids["beta"], "operation_id": record["operation_id"]})
    if not accept.verified_reevaluation_receipt(original, operation_id=record["operation_id"], target_id=fixture.target_ids["beta"], process_run_id=arguments["expected_process_run_id"]):
        raise accept.Blocked("The current producer did not confirm the original earlier reevaluation")


async def refusal_probe(live: accept.Live, fixture: accept.Fixture, checkpoint: smoke.DotCheckpoint, scenario: str, issue_number: int, *, root: Path = ROOT, policy_path: Path = POLICY, journal_path: Path = JOURNAL) -> NegativeReport:
    """Prove current policy refusal at the producer and independent admission DB."""
    if scenario not in REFUSAL_PROBES or type(issue_number) is not int or not 1 <= issue_number <= 999999999:
        raise accept.Blocked("Select one fixed refusal scenario and a test-repository Issue number")
    policy_revision = observe_policy_revision(fixture, policy_path)
    if scenario == "policy-downgrade":
        await prior_reevaluation(live, fixture, root)
    target = fixture.target_ids["beta"]
    before = journal_original_key(journal_path, target, checkpoint.idempotency_key)
    if before:
        raise accept.Blocked("A refusal probe cannot replay an existing original journal intent")
    beta = await accept.observed_target(live, target, "beta", accept.BETA_WORK_REPOSITORY)
    worker = beta.get("worker")
    if beta.get("alive") is not True or not isinstance(worker, dict):
        raise accept.Blocked("No genuine current worker fence is available for the refusal probe")
    arguments = {
        "target_id": target,
        "target_type": "issue",
        "number": issue_number,
        "idempotency_key": checkpoint.idempotency_key,
        "expected_process_run_id": worker.get("process_run_id"),
        "expected_deployment_generation": worker.get("deployment_generation"),
    }
    if any(not isinstance(value, (str, int)) or not value for value in arguments.values()):
        raise accept.Blocked("The current refusal-probe process fence is unavailable")
    if observe_policy_revision(fixture, policy_path) != policy_revision:
        raise accept.Blocked("The operator policy changed before the refusal probe")
    ticket = refusal_ticket(scenario, root)
    accept.persist_original_ticket(ticket, fixture, checkpoint, "auto_coder_reevaluate_target", arguments)
    response = await live.call("auto_coder_reevaluate_target", arguments, mutation=True)
    operation_id = response.get("operation_id")
    if isinstance(operation_id, str) and operation_id and response.get("action") == "reevaluate_target" and response.get("target_id") == target and response.get("status") not in {"BUSY", "CONFLICT", "NOT_FOUND", "DENIED", "UNAVAILABLE", "INVALID_REQUEST"}:
        accept.update_original_ticket(ticket, fixture, operation_id, "ADMITTED")
        raise accept.Failed("The expected refusal admitted an operation; preserve its original ticket and stop")
    if operation_id is not None:
        raise accept.Blocked("A busy, conflicting or unbound operation is not this probe's original admission; preserve uncertainty")
    if response.get("status") != "DENIED" or response.get("reason") != "profile_denies_mutation":
        raise accept.Blocked("The producer did not prove the expected policy refusal; the send remains held")
    after = journal_original_key(journal_path, target, checkpoint.idempotency_key)
    if after:
        raise accept.Failed("The producer refusal conflicts with an admitted original journal intent")
    digest = _journal_absence_digest(target, checkpoint.idempotency_key, after)
    accept.record_refused_ticket(ticket, fixture, reason="profile_denies_mutation", journal_sha256=digest)
    if observe_policy_revision(fixture, policy_path) != policy_revision:
        raise accept.Blocked("The refusal is retained, but the operator policy changed during the scenario")
    live.observations.append(accept.Evidence("management-operation-journal", digest, "fixture SQLite mode=ro original-key read", "ABSENT"))
    return NegativeReport("PASS", scenario, "The current producer denied the fixed request and its original admission key is absent; no worker effect", tuple(live.observations), negative_boundary_verified=True)


def original_binding(checkpoint: smoke.DotCheckpoint, fixture: accept.Fixture) -> tuple[str, str]:
    operation_id = checkpoint.pending_operation_id
    if not operation_id or checkpoint.pending_effect_state not in smoke.UNKNOWN_EFFECTS:
        raise accept.Blocked("Retain the exact original unresolved operation before a recovery scenario")
    try:
        target = smoke._original_operation_target(checkpoint, operation_id)
    except ValueError as error:
        raise accept.Blocked("Original operation must have one retained target binding") from error
    if target not in fixture.target_ids.values():
        raise accept.Blocked("Original operation is outside the disposable fixture")
    return target, operation_id


def check_original(value: dict[str, object], target: str, operation_id: str, source_sha: str) -> None:
    if value.get("operation_id") != operation_id or value.get("target_id") != target:
        raise accept.Blocked("The original journal operation is unavailable or differently bound")
    if value.get("action") not in {"reevaluate_target", "deploy_beta_source", "deploy_release_source", "start_worker", "stop_worker", "restart_worker"}:
        raise accept.Blocked("The original action has no supported recovery acceptance scenario")
    if str(value.get("action")).startswith("deploy_") and value.get("source_sha") != source_sha:
        raise accept.Failed("Original source operation differs from the immutable selected source")


def record_exhausted_failure(root: Path, fixture: accept.Fixture, identity: FailureIdentity, operation_id: str) -> str:
    """Retain the producer's consumed budget; never reserve or dispatch a recovery."""
    signature = identity.signature
    record = {"schema": 1, "fixture_nonce": fixture.fixture_nonce, "failure": asdict(identity), "signature": signature, "original_operation_id": operation_id, "recovery_attempts": 1, "outcome": "BLOCKED"}
    path = root / f"dot-recovery-{signature}.json"
    encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        try:
            prior = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise accept.Blocked("The retained recovery budget is unavailable") from error
        if prior != record:
            raise accept.Blocked("A different operation cannot replace the original exhausted failure signature")
        return signature
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return signature


async def original_scenario(live: accept.Live, fixture: accept.Fixture, checkpoint: smoke.DotCheckpoint, scenario: str, *, root: Path = ROOT) -> NegativeReport:
    target, operation_id = original_binding(checkpoint, fixture)
    arguments = {"target_id": target, "operation_id": operation_id}
    original = await live.call("auto_coder_operation_inspect", arguments)
    check_original(original, target, operation_id, checkpoint.source_sha)
    signature = ""
    if scenario == "held-drain":
        steps = original.get("steps")
        drain = steps.get("drain") if isinstance(steps, dict) else None
        incumbent = original.get("incumbent")
        if original.get("status") != "DRAINING" or not isinstance(drain, dict) or drain.get("operation_id") != "d-" + operation_id or drain.get("state") not in {"SENT_UNKNOWN", "CONFIRMED"} or not isinstance(incumbent, dict) or "start" in steps:
            raise accept.Blocked("No original held graceful drain is currently proven")
        channel = "beta" if target == fixture.target_ids["beta"] else "release"
        repository = accept.BETA_WORK_REPOSITORY if channel == "beta" else accept.TEST_REPOSITORY
        current = await accept.observed_target(live, target, channel, repository)
        worker = current.get("worker")
        if (
            current.get("status") != "OK"
            or current.get("target_id") != target
            or current.get("alive") is not True
            or current.get("process_state") != "DRAINING"
            or not incumbent.get("supervisor_generation")
            or current.get("supervisor_generation") != incumbent["supervisor_generation"]
            or not isinstance(worker, dict)
            or any(not incumbent.get(key) or worker.get(key) != incumbent[key] for key in ("process_run_id", "deployment_generation", "runtime_id"))
        ):
            raise accept.Blocked("The original draining worker lifetime cannot be independently verified")
        reason = "The genuine original drain remains held; no start, retry or recovery was dispatched"
    elif scenario == "unknown-effect":
        if original.get("status") not in {"NEEDS_ATTENTION", "RECONCILING", "RUNNING", "DRAINING", "STARTING", "ACCEPTED"} and original.get("effect_state") != "sent_unknown":
            raise accept.Blocked("The original operation is not currently an unresolved-effect case")
        reason = "The original effect remains unresolved; only its non-dispatching journal was inspected"
    elif scenario == "repeated-failure":
        if type(original.get("recovery_attempts")) is not int or original["recovery_attempts"] != 1 or original.get("status") not in {"NEEDS_ATTENTION", "FAILED", "RECOVERED_PREVIOUS"}:
            raise accept.Blocked("The producer has not proven one consumed recovery attempt for this failure")
        failure_reason = original.get("reason")
        if not isinstance(failure_reason, str) or not failure_reason or smoke.SAFE_ID.fullmatch(failure_reason) is None:
            raise accept.Blocked("The original producer failure category is unavailable")
        identity = FailureIdentity(str(original["action"]), target, checkpoint.source_sha, failure_reason)
        signature = record_exhausted_failure(root, fixture, identity, operation_id)
        exhausted = replace(checkpoint, pending_effect_state="", pending_operation_id="", failure_signature=signature, recovery_attempts=1)
        if smoke.required_next_observation(exhausted).verdict != "BLOCKED":
            raise accept.Failed("The unchanged failure signature did not trip the recovery circuit breaker")
        reason = "The producer consumed its one recovery attempt; the durable unchanged failure signature blocks another attempt"
    elif scenario == "data-rollback":
        # No currently supported producer attests to persistent-data downgrade
        # compatibility. A Python runtime receipt must never be promoted into it.
        return NegativeReport("BLOCKED", scenario, "Persistent data rollback and compatibility are unsupported; original runtime evidence cannot authorize another recovery or data restoration", tuple(live.observations), operation_id)
    else:
        raise accept.Blocked("Select a supported read-only original-operation scenario")
    after = await live.call("auto_coder_operation_inspect", arguments)
    check_original(after, target, operation_id, checkpoint.source_sha)
    if any(after.get(key) != original.get(key) for key in ("action", "status", "phase", "source_sha", "recovery_attempts", "recovery_state")):
        raise accept.Blocked("The original producer state changed during bounded negative observation")
    return NegativeReport("BLOCKED", scenario, reason, tuple(live.observations), operation_id, negative_boundary_verified=True, failure_signature=signature)


async def run_authenticated(fixture: accept.Fixture, checkpoint: smoke.DotCheckpoint, scenario: str, *, issue_number: int) -> NegativeReport:
    bearer = os.environ.get("AUTO_CODER_MCP_ACCESS_TOKEN", "")
    if not bearer:
        raise accept.Blocked("No current fixture-specific OAuth bearer; no MCP request sent")
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(fixture.endpoint, headers={"Authorization": "Bearer " + bearer}, timeout=15, sse_read_timeout=30) as (reader, writer, _):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            live = accept.Live(session, {tool.name for tool in (await session.list_tools()).tools})
            identity = await live.call("auto_coder_whoami", {})
            accept.verify_current_operator(identity, fixture, "reevaluate", mutation=scenario in REFUSAL_PROBES)
            accept.verify_current_targets(await live.call("auto_coder_targets", {}), fixture)
            if scenario in REFUSAL_PROBES:
                return await refusal_probe(live, fixture, checkpoint, scenario, issue_number)
            return await original_scenario(live, fixture, checkpoint, scenario)


def exception_report(scenario: str, reason: str, *, verdict: str = "BLOCKED", root: Path = ROOT) -> NegativeReport:
    if scenario not in REFUSAL_PROBES:
        return NegativeReport(verdict, scenario, reason)
    path = refusal_ticket(scenario, root)
    if not path.exists() and not path.is_symlink():
        return NegativeReport(verdict, scenario, reason)
    operation_id = ""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and isinstance(value.get("operation_id"), str) and smoke.SAFE_ID.fullmatch(value["operation_id"]):
            operation_id = value["operation_id"]
    except (OSError, ValueError):
        pass
    # Conservatively retain uncertainty on every post-ticket exceptional path,
    # even if the response was a refusal but its independent readback failed.
    return NegativeReport(verdict, scenario, reason, operation_id=operation_id, effect_dispatched=None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-manifest", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="unknown-effect")
    parser.add_argument("--enable-refusal-probe", action="store_true")
    parser.add_argument("--operator-stage", default="")
    parser.add_argument("--operator-stage-revision", default="")
    parser.add_argument("--fixture-issue-number", type=int, default=0)
    args = parser.parse_args()
    logger.remove()
    layout = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {file}:{function}:{line} | {message}"
    logger.add(sys.stdout, colorize=True, format=layout)
    state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "auto-coder"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    logger.add(state / "dot-mcp-negative-acceptance.log", rotation="2 MB", retention=3, format=layout)
    if args.fixture_manifest is None:
        report = NegativeReport("NOT_RUN", args.scenario, "No disposable fixture selected; no network or effect")
    else:
        try:
            fixture = accept.Fixture.load(args.fixture_manifest)
            accept.bound_local_fixture(fixture)
            checkpoint = smoke.load_checkpoint(args.checkpoint)
            selected_checkpoint(fixture, checkpoint, args.scenario, args.operator_stage, args.operator_stage_revision, probe=args.enable_refusal_probe)
            report = asyncio.run(run_authenticated(fixture, checkpoint, args.scenario, issue_number=args.fixture_issue_number))
        except accept.Failed as error:
            report = exception_report(args.scenario, str(error), verdict="FAIL")
        except accept.Blocked as error:
            report = exception_report(args.scenario, str(error))
        except ExceptionGroup as error:
            nested = accept._single_nested_acceptance_error(error)
            report = exception_report(args.scenario, str(nested) if nested else "Fixture transport is unavailable; original sends remain held", verdict="FAIL" if isinstance(nested, accept.Failed) else "BLOCKED")
        except Exception:
            report = exception_report(args.scenario, "Fixture evidence or transport is unavailable; original sends remain held")
    logger.info("Negative acceptance verdict={} scenario={}", report.verdict, report.scenario)
    print(json.dumps(asdict(report), sort_keys=True, separators=(",", ":"), allow_nan=False))
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 2, "NOT_RUN": 3}[report.verdict]


if __name__ == "__main__":
    raise SystemExit(main())
