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


@dataclass(frozen=True)
class DotCheckpoint:
    """Operator-owned reminder, never an admission grant or current evidence."""

    schema: int
    stage: str
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
    if raw["source_sha"] and SHA.fullmatch(raw["source_sha"]) is None:
        raise ValueError("checkpoint source SHA must be exact full lowercase Git SHA")
    if raw["idempotency_key"] and SAFE_ID.fullmatch(raw["idempotency_key"]) is None:
        raise ValueError("invalid original idempotency key")
    if type(raw["recovery_attempts"]) is not int or not 0 <= raw["recovery_attempts"] <= 1:
        raise ValueError("immutable failure-signature recovery budget exceeded")
    if raw["recovery_attempts"] and not raw["failure_signature"]:
        raise ValueError("recovery attempt requires its immutable failure signature")
    return DotCheckpoint(**raw)


def required_next_observation(checkpoint: DotCheckpoint) -> RolloutReport:
    """Never promote a checkpoint into live success or release unknown effects."""
    if checkpoint.pending_effect_state in UNKNOWN_EFFECTS:
        return RolloutReport("BLOCKED", checkpoint.stage, "Reconcile the original unknown effect by original operation ID; never allocate another key")
    if checkpoint.recovery_attempts == 1 and checkpoint.failure_signature:
        return RolloutReport("BLOCKED", checkpoint.stage, "The unchanged failure signature has exhausted its one permitted recovery attempt")
    if checkpoint.stage == "observe":
        return RolloutReport("NOT_RUN", checkpoint.stage, "Current authenticated status, original operation and child evidence require an independent live read")
    if checkpoint.stage == "beta":
        if not (checkpoint.source_sha and checkpoint.ci_run_id and checkpoint.tested_source_ref):
            return RolloutReport("BLOCKED", checkpoint.stage, "Exact tested Git source and original CI evidence are missing")
        return RolloutReport("NOT_RUN", checkpoint.stage, "Beta mutation and installed worker acceptance require disposable original producers and current operator authority")
    if not (checkpoint.beta_acceptance_ref and checkpoint.beta_acceptance_oid and checkpoint.promotion_operation_id and checkpoint.promotion_catalog_oid):
        return RolloutReport("BLOCKED", checkpoint.stage, "Current beta acceptance and distinct release promotion evidence are missing")
    return RolloutReport("NOT_RUN", checkpoint.stage, "Release installed-worker lifecycle and original receipt cannot be certified by a checkpoint")


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
    if checkpoint.pending_effect_state in UNKNOWN_EFFECTS and not operation_id:
        raise ValueError("unknown original effect requires its original operation ID")
    plan: tuple[tuple[str, dict[str, object]], ...] = (
        ("auto_coder_whoami", {}),
        ("auto_coder_status", {"target_id": target}),
        ("auto_coder_deployment_status", {"target_id": target}),
    )
    if operation_id:
        plan += (("auto_coder_operation_get", {"target_id": target, "operation_id": operation_id}),)
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
    args = parser.parse_args()
    _configure_logging()
    try:
        checkpoint = load_checkpoint(args.checkpoint)
        if args.endpoint:
            endpoint = checked_endpoint(args.endpoint)
            plan = read_plan(checkpoint, args.target_id or "", args.operation_id)
            bearer = os.environ.get("AUTO_CODER_MCP_ACCESS_TOKEN", "")
            if not bearer:
                report = RolloutReport("BLOCKED", checkpoint.stage, "No operator-provided current OAuth access token; no MCP request sent")
            else:
                report = asyncio.run(observe_live(endpoint, bearer, plan, checkpoint.stage))
        else:
            report = required_next_observation(checkpoint)
        logger.info("Dot rollout observation stage={} verdict={} remote_effect=false", report.stage, report.verdict)
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
