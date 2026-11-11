#!/usr/bin/env python3
"""Bounded read-only MCP rollout checkpoint observer; never dispatches an effect.

Only the authenticated production service supplies live evidence. An offline
checkpoint, a green CI run or a successful diagnostic cannot prove deployment.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from loguru import logger

READ_TOOLS = frozenset(
    {
        "auto_coder_whoami",
        "auto_coder_targets",
        "auto_coder_status",
        "auto_coder_deployment_status",
        "auto_coder_operation_get",
        "auto_coder_beta_source_readback",
        "auto_coder_beta_acceptance_get",
    }
)
STAGES = frozenset({"observe", "beta", "full"})
UNKNOWN_EFFECTS = frozenset({"SENT_UNKNOWN", "NEEDS_ATTENTION"})
SHA = re.compile(r"[0-9a-f]{40}\Z")
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
ISSUE_NUMBER = re.compile(r"[1-9][0-9]{0,8}\Z")


class OperatorScopeError(ValueError):
    """A saved operator selection is not proof of present operator authority."""


@dataclass(frozen=True)
class DotCheckpoint:
    """Operator-owned reminder, never an admission grant or current evidence."""

    schema: int
    stage: str
    task_id: str = ""
    repository: str = ""
    issue_number: str = ""
    pr_number: str = ""
    stage_revision: str = ""
    source_sha: str = ""
    ci_run_id: str = ""
    ci_run_attempt: str = ""
    tested_source_ref: str = ""
    tested_source_oid: str = ""
    beta_target: str = ""
    beta_operation_id: str = ""
    beta_runtime_id: str = ""
    beta_generation: str = ""
    beta_acceptance_ref: str = ""
    beta_acceptance_oid: str = ""
    promotion_operation_id: str = ""
    promotion_run_id: str = ""
    promotion_catalog_ref: str = ""
    promotion_catalog_oid: str = ""
    release_target: str = ""
    release_operation_id: str = ""
    release_runtime_id: str = ""
    release_generation: str = ""
    idempotency_key: str = ""
    pending_effect_state: str = ""
    pending_operation_id: str = ""
    failure_signature: str = ""
    recovery_attempts: int = 0


@dataclass(frozen=True)
class Observation:
    tool: str
    digest: str
    evidence_source: str


@dataclass(frozen=True)
class RolloutReport:
    verdict: str
    stage: str
    reason: str
    observations: tuple[Observation, ...] = ()
    positive_deployment_proven: bool = False
    effect_dispatched: bool = False


def load_checkpoint(path: Path | None) -> DotCheckpoint:
    if path is None:
        return DotCheckpoint(schema=1, stage="observe")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != set(DotCheckpoint.__dataclass_fields__):
        raise ValueError("checkpoint must contain exactly the documented schema-1 fields")
    if type(raw["schema"]) is not int or raw["schema"] != 1:
        raise ValueError("unsupported checkpoint schema")
    if raw["stage"] not in STAGES:
        raise ValueError("unsupported operator-selected stage")
    for field in DotCheckpoint.__dataclass_fields__:
        if field in {"schema", "recovery_attempts"}:
            continue
        value = raw[field]
        if not isinstance(value, str) or len(value) > 256 or "\x00" in value:
            raise ValueError("invalid checkpoint field: " + field)
    if raw["task_id"] and SAFE_ID.fullmatch(raw["task_id"]) is None:
        raise ValueError("invalid original task identity")
    if raw["repository"] and REPOSITORY.fullmatch(raw["repository"]) is None:
        raise ValueError("invalid original repository identity")
    for field in ("issue_number", "pr_number"):
        if raw[field] and ISSUE_NUMBER.fullmatch(raw[field]) is None:
            raise ValueError("invalid original GitHub work identity: " + field)
        if raw[field] and not raw["repository"]:
            raise ValueError("original Issue/PR identity requires its repository")
    if raw["stage_revision"] and SAFE_ID.fullmatch(raw["stage_revision"]) is None:
        raise ValueError("invalid original operator stage revision")
    if raw["source_sha"] and SHA.fullmatch(raw["source_sha"]) is None:
        raise ValueError("checkpoint source SHA must be exact full lowercase Git SHA")
    if raw["idempotency_key"] and SAFE_ID.fullmatch(raw["idempotency_key"]) is None:
        raise ValueError("invalid original idempotency key")
    if type(raw["recovery_attempts"]) is not int or not 0 <= raw["recovery_attempts"] <= 1:
        raise ValueError("immutable failure-signature recovery budget exceeded")
    if raw["recovery_attempts"] and not raw["failure_signature"]:
        raise ValueError("recovery attempt requires its immutable failure signature")
    if raw["pending_effect_state"] not in UNKNOWN_EFFECTS | {""}:
        raise ValueError("unsupported pending effect state: refuse unknown-operation reconciliation")
    if raw["pending_effect_state"]:
        if not raw["pending_operation_id"]:
            raise ValueError("pending effect requires its exact original operation ID")
        _original_operation_target(DotCheckpoint(**raw), raw["pending_operation_id"])
    elif raw["pending_operation_id"]:
        raise ValueError("pending original operation ID requires a pending effect state")
    return DotCheckpoint(**raw)


def _original_operation_target(checkpoint: DotCheckpoint, operation_id: str) -> str:
    """Bind a saved operation to exactly one original target, never a caller guess."""
    bindings = [
        target
        for saved_id, target in (
            (checkpoint.beta_operation_id, checkpoint.beta_target),
            (checkpoint.promotion_operation_id, checkpoint.release_target),
            (checkpoint.release_operation_id, checkpoint.release_target),
        )
        if saved_id == operation_id
    ]
    if len(bindings) != 1 or not bindings[0]:
        raise ValueError("original operation ID must be uniquely bound to its configured target")
    return bindings[0]


def required_next_observation(checkpoint: DotCheckpoint) -> RolloutReport:
    """Never promote a checkpoint into live success or release unknown effects."""
    if checkpoint.pending_effect_state:
        if checkpoint.pending_effect_state not in UNKNOWN_EFFECTS:
            return RolloutReport("BLOCKED", checkpoint.stage, "Unsupported pending effect state; never assume no original effect")
        if not checkpoint.pending_operation_id:
            return RolloutReport("BLOCKED", checkpoint.stage, "Pending effect lacks its exact original operation ID")
        try:
            _original_operation_target(checkpoint, checkpoint.pending_operation_id)
        except ValueError:
            return RolloutReport("BLOCKED", checkpoint.stage, "Pending original operation ID is not uniquely bound to its target")
        return RolloutReport("BLOCKED", checkpoint.stage, "Reconcile the original unknown effect by original operation ID; never allocate another key")
    if checkpoint.recovery_attempts == 1 and checkpoint.failure_signature:
        return RolloutReport("BLOCKED", checkpoint.stage, "The unchanged failure signature has exhausted its one permitted recovery attempt")
    if checkpoint.stage != "observe" and not (checkpoint.task_id and checkpoint.repository and (checkpoint.issue_number or checkpoint.pr_number) and checkpoint.stage_revision):
        return RolloutReport("BLOCKED", checkpoint.stage, "Original work identity or operator stage revision is missing; never infer authority from current HEAD or stage name")
    if checkpoint.stage == "observe":
        return RolloutReport("NOT_RUN", checkpoint.stage, "Current authenticated status, original operation and child evidence require an independent live read")
    if not (checkpoint.source_sha and checkpoint.ci_run_id and checkpoint.ci_run_attempt and checkpoint.tested_source_ref and checkpoint.tested_source_oid and checkpoint.beta_target):
        return RolloutReport("BLOCKED", checkpoint.stage, "Exact tested Git source SHA/ref/OID, original CI run/attempt and configured beta target are required")
    if checkpoint.stage == "beta":
        return RolloutReport("NOT_RUN", checkpoint.stage, "Beta mutation and installed worker acceptance require disposable original producers and current operator authority")
    if not (checkpoint.beta_acceptance_ref and checkpoint.beta_acceptance_oid and checkpoint.promotion_operation_id and checkpoint.promotion_catalog_oid):
        return RolloutReport("BLOCKED", checkpoint.stage, "Current beta acceptance and distinct release promotion evidence are missing")
    return RolloutReport("NOT_RUN", checkpoint.stage, "Release installed-worker lifecycle and original receipt cannot be certified by a checkpoint")


def confirm_operator_scope(checkpoint: DotCheckpoint, current_stage: str | None, current_revision: str) -> None:
    """Refuse stale stage selection before any network request; never grant effects."""
    if current_stage not in STAGES:
        raise OperatorScopeError("current operator stage is required before live diagnostics")
    if current_stage != checkpoint.stage:
        raise OperatorScopeError("current operator stage differs from the original checkpoint; no MCP request sent")
    if current_stage == "observe":
        if current_revision and current_revision != checkpoint.stage_revision:
            raise OperatorScopeError("current operator stage revision differs from the original checkpoint")
        return
    if not (checkpoint.task_id and checkpoint.repository and (checkpoint.issue_number or checkpoint.pr_number) and checkpoint.stage_revision):
        raise OperatorScopeError("original work identity and operator stage revision are required")
    if not current_revision:
        raise OperatorScopeError("current operator stage revision is required before live diagnostics")
    if SAFE_ID.fullmatch(current_revision) is None:
        raise OperatorScopeError("current operator stage revision is invalid")
    if current_revision != checkpoint.stage_revision:
        raise OperatorScopeError("current operator stage revision differs from the original checkpoint; no MCP request sent")


def checked_endpoint(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.query or parsed.fragment or parsed.username or parsed.password or parsed.path != "/mcp":
        raise ValueError("MCP endpoint must be a fixed resource /mcp URL without credentials or parameters")
    if not parsed.hostname or parsed.port == 0:
        raise ValueError("MCP endpoint hostname is required")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}):
        raise ValueError("bearer credentials require HTTPS except for explicit loopback testing")
    return url


def checked_tool(name: str, arguments: dict[str, object]) -> None:
    if name not in READ_TOOLS:
        raise ValueError("this observer never dispatches mutating MCP tools")
    if name == "auto_coder_whoami" and arguments:
        raise ValueError("unexpected identity arguments")
    if name == "auto_coder_status" and set(arguments) != {"target_id"}:
        raise ValueError("status requires only exact target ID")
    if name == "auto_coder_operation_get" and set(arguments) != {"target_id", "operation_id"}:
        raise ValueError("operation readback requires original operation and target IDs")
    if any(not isinstance(v, str) or SAFE_ID.fullmatch(v) is None for v in arguments.values()):
        raise ValueError("read-only observer accepts only exact safe identifiers")


def read_plan(checkpoint: DotCheckpoint, target: str, operation_id: str) -> tuple[tuple[str, dict[str, object]], ...]:
    """Produce an explicit allowlist, not a caller-controlled arbitrary MCP tool."""
    if not target or SAFE_ID.fullmatch(target) is None:
        raise ValueError("an exact configured target is required")
    if operation_id and SAFE_ID.fullmatch(operation_id) is None:
        raise ValueError("invalid original operation ID")
    if checkpoint.pending_effect_state and checkpoint.pending_effect_state not in UNKNOWN_EFFECTS:
        raise ValueError("unsupported pending effect state")
    if checkpoint.pending_effect_state in UNKNOWN_EFFECTS:
        if not checkpoint.pending_operation_id or not operation_id:
            raise ValueError("unknown original effect requires its original operation ID")
        if operation_id != checkpoint.pending_operation_id:
            raise ValueError("pending original operation ID cannot be replaced by another saved operation")
    if checkpoint.pending_operation_id and not checkpoint.pending_effect_state:
        raise ValueError("pending original operation ID without unresolved effect state")
    if operation_id and _original_operation_target(checkpoint, operation_id) != target:
        raise ValueError("original operation ID must be uniquely bound to its configured target")
    original_read: tuple[str, dict[str, object]] = ("auto_coder_operation_get", {"target_id": target, "operation_id": operation_id})
    status_read: tuple[str, dict[str, object]] = ("auto_coder_status", {"target_id": target})
    deployment_read: tuple[str, dict[str, object]] = ("auto_coder_deployment_status", {"target_id": target})
    if checkpoint.pending_effect_state in UNKNOWN_EFFECTS:
        # Original-operation reconciliation precedes current deployment diagnostics.
        plan: tuple[tuple[str, dict[str, object]], ...] = (
            ("auto_coder_whoami", {}),
            original_read,
            status_read,
            deployment_read,
        )
    else:
        plan = (
            ("auto_coder_whoami", {}),
            status_read,
            deployment_read,
        )
        if operation_id:
            plan += (original_read,)
    for name, arguments in plan:
        checked_tool(name, arguments)
    return plan


async def observe_live(endpoint: str, bearer: str, plan: tuple[tuple[str, dict[str, object]], ...], stage: str) -> RolloutReport:
    """Capture hashes of actual MCP read responses, not claimed deployment PASS."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    observations: list[Observation] = []
    async with streamablehttp_client(endpoint, headers={"Authorization": "Bearer " + bearer}, timeout=15, sse_read_timeout=30) as (reader, writer, _):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            registered = {item.name for item in (await session.list_tools()).tools}
            if any(name not in registered for name, _ in plan):
                return RolloutReport("BLOCKED", stage, "Required authenticated read-only tool is unavailable")
            for name, arguments in plan:
                checked_tool(name, arguments)
                result = await session.call_tool(name, arguments)
                if result.isError or result.structuredContent is None:
                    return RolloutReport("BLOCKED", stage, "Current authenticated readback was unavailable", tuple(observations))
                encoded = json.dumps(result.structuredContent, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
                observations.append(Observation(name, hashlib.sha256(encoded).hexdigest(), "live authenticated MCP structuredContent"))
    return RolloutReport(
        "NOT_RUN",
        stage,
        "Read-only diagnostics collected; independent tested Git, accepted release and advancing installed-worker producer evidence remain unverified",
        tuple(observations),
    )


def _configure_logging() -> None:
    location = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "auto-coder"
    location.mkdir(parents=True, exist_ok=True, mode=0o700)
    logger.remove()
    logger.add(sys.stdout, serialize=True)
    logger.add(location / "dot-rollout-smoke.log", rotation="2 MB", retention=3, format="{time} | {level} | {name}:{function}:{line} | {message}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only operator checkpoint; no mutation or deployment")
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--endpoint", help="Explicit operator-selected OAuth MCP resource ending in /mcp")
    parser.add_argument("--target-id", help="Exact configured worker alias (required with endpoint)")
    parser.add_argument("--operation-id", default="", help="Existing original management operation only")
    parser.add_argument("--operator-stage", help="Fresh operator-selected stage for diagnostics, not a grant")
    parser.add_argument("--operator-stage-revision", default="", help="Fresh operator-selected stage revision; must match the saved original")
    args = parser.parse_args()
    _configure_logging()
    try:
        checkpoint = load_checkpoint(args.checkpoint)
        if args.endpoint:
            endpoint = checked_endpoint(args.endpoint)
            current_stage = args.operator_stage or ("observe" if checkpoint.stage == "observe" else None)
            confirm_operator_scope(checkpoint, current_stage, args.operator_stage_revision)
            plan = read_plan(checkpoint, args.target_id or "", args.operation_id)
            bearer = os.environ.get("AUTO_CODER_MCP_ACCESS_TOKEN", "")
            if not bearer:
                report = RolloutReport("BLOCKED", checkpoint.stage, "No operator-provided current OAuth access token; no MCP request sent")
            else:
                report = asyncio.run(observe_live(endpoint, bearer, plan, checkpoint.stage))
        else:
            report = required_next_observation(checkpoint)
        logger.info("Dot rollout observation stage={} verdict={} remote_effect=false", report.stage, report.verdict)
    except OperatorScopeError as error:
        logger.warning("Original operator scope differs from current selection; no MCP request sent")
        report = RolloutReport("BLOCKED", checkpoint.stage, str(error))
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        logger.warning("Dot rollout evidence unavailable: {}", type(error).__name__)
        report = RolloutReport("BLOCKED", "unknown", "Checkpoint, authority or evidence unavailable; no effect dispatched")
    except Exception as error:
        logger.warning("Dot rollout transport unavailable: {}", type(error).__name__)
        report = RolloutReport("BLOCKED", "unknown", "MCP transport or authenticated producer unavailable; no effect dispatched")
    print(json.dumps(asdict(report), sort_keys=True))
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 2, "NOT_RUN": 3}[report.verdict]


if __name__ == "__main__":
    raise SystemExit(main())
