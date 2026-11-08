#!/usr/bin/env python3
"""Plan a complete, time-budgeted CI matrix from actual pytest collection."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import cast

from loguru import logger

from auto_coder.ci_duration_profile import ProfileIdentity
from auto_coder.ci_shard_plan import INVENTORY_SCHEMA, PLUGIN, ShardContext, ShardPlanError, _digest, _object, _read, _strings, build_plan, validate_inventory, verify_plan, write_plan

LOG_FORMAT = "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level}</level> | {file.name}:{function}:{line} | {message}"


def _logging(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stdout, colorize=True, format=LOG_FORMAT, catch=False)
    logger.add(directory / "ci-shard-plan.log", colorize=False, format=LOG_FORMAT, rotation="10 MB", retention=5, encoding="utf-8", catch=False)


def _collect(context_file: Path, context: ShardContext) -> tuple[dict[str, object], tuple[str, ...]]:
    with tempfile.TemporaryDirectory(prefix="ci-shard-collect-") as directory:
        output = Path(directory) / "inventory.json"
        # Use attached values: pytest discovers its root/config before registering plugin options.
        command = [sys.executable, "-m", "pytest", "-p", PLUGIN, "--collect-only", f"--ci-context={context_file.resolve()}", f"--ci-collect-output={output}", *context.pytest_args]
        try:
            process = subprocess.run(command, text=True, capture_output=True, check=False, timeout=300)
        except subprocess.TimeoutExpired as error:
            raise ShardPlanError("real pytest collection timed out (no tests were executed)") from error
        if process.returncode != 0:
            raise ShardPlanError(f"real pytest collection failed with exit {process.returncode}; no plan published")
        data = _read(output)
        if data.get("schema_version") != INVENTORY_SCHEMA or data.get("checkout_sha") != context.checkout_sha or data.get("pytest_args") != list(context.pytest_args):
            raise ShardPlanError("collected inventory origin/arguments differ from requested profile")
        profile = _object(data.get("profile"), "collected.profile")
        identity = ProfileIdentity.parse(profile)
        if profile != identity.json() or identity.repository != context.repository or identity.profile_id != context.profile_id or identity.python_major_minor != f"{sys.version_info.major}.{sys.version_info.minor}":
            raise ShardPlanError("collected profile identity mismatch")
        nodeids = _strings(data.get("nodeids"), "nodeids", allow_empty=False)
        validate_inventory(nodeids)
        if data.get("inventory_digest") != _digest(list(nodeids)):
            raise ShardPlanError("collected inventory digest mismatch")
        return profile, nodeids


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--" not in arguments:
        logger.error("Pass the complete pytest selection after a literal --")
        return 2
    split = arguments.index("--")
    parser = argparse.ArgumentParser(description="Create an immutable complete CI shard plan from real pytest collection")
    parser.add_argument("--context", type=Path, required=True, help="Explicit tested checkout, profile, selectors, harness/env and origin JSON")
    parser.add_argument("--duration-profile", type=Path, help="Optional immutable native duration snapshot; invalid/missing history uses fallback estimates")
    parser.add_argument("--cold-start-estimate-seconds", type=int, default=1, help="Conservative estimate for unknown cases; never substitutes for native duration evidence")
    parser.add_argument("--output", type=Path, required=True, help="New immutable output file")
    parser.add_argument("--log-dir", type=Path, required=True, help="Rotating diagnostic logs")
    args = parser.parse_args(arguments[:split])
    try:
        _logging(args.log_dir)
        context = ShardContext.read(args.context)
        if tuple(arguments[split + 1 :]) != context.pytest_args:
            raise ShardPlanError("CLI pytest selectors/configuration differ from context.pytest_args")
        profile, nodeids = _collect(args.context, context)
        plan = build_plan(context, profile, nodeids, args.duration_profile, cold_start_fallback_ms=args.cold_start_estimate_seconds * 1000)
        verified = verify_plan(plan)
        protected = (args.context, *((args.duration_profile,) if args.duration_profile is not None else ()))
        write_plan(args.output, plan, protected)
        semantic = _object(plan["semantic"], "semantic")
        source = _object(semantic["duration_source"], "duration_source")
        logger.info("CI plan {} covers {} collected pytest nodes in {} shards; predicted work {} ms (effective capacity {} ms)", verified.digest, len(nodeids), len(verified.shards), semantic["predicted_total_ms"], semantic["effective_capacity_ms"])
        if source["status"] != "matched":
            logger.warning("Duration history not used: {}", source["reason"])
        if semantic["capacity_limited"]:
            logger.warning("Shard cap {} requires increasing effective capacity from 420000 to {} ms", semantic["shard_cap"], semantic["effective_capacity_ms"])
        if any(_object(shard, "shard")["over_budget_nodes"] for shard in cast(list[object], semantic["shards"])):
            logger.warning("Indivisible node(s) exceed the 420-second target and remain fully assigned")
        return 0
    except (ShardPlanError, OSError, ValueError) as error:
        logger.error("CI shard plan refused: {}: {}", type(error).__name__, error)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
