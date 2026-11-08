"""Prepare source-test plan/matrix and optional native duration batches."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from auto_coder.ci_duration_profile import BATCH_SCHEMA
from auto_coder.ci_duration_profile import CONTEXT_SCHEMA as DURATION_CONTEXT_SCHEMA
from auto_coder.ci_shard_plan import CONTEXT_SCHEMA, ShardContext, _canonical, read_plan
from auto_coder.source_ci_evidence import SOURCE_JOB, WORKFLOW_NAME, SourceEvidenceError, _artifact_name, verify_current_source_attempt
from auto_coder.source_ci_metrics import build_report
from auto_coder.util.gh_cache import GitHubGitDataClient

SELECTORS = ("-m", "not opencode_live")
HARNESS = (
    "tests/conftest.py",
    "tests/ci_timing_plugin.py",
    "tests/pytest_fault_diagnostics.py",
    "scripts/ci_shard_plugin.py",
    "scripts/source_ci_receipt_plugin.py",
    "src/auto_coder/ci_shard_plan.py",
    "src/auto_coder/source_ci_evidence.py",
)
SEMANTIC_ENV = ("AUTO_CODER_REQUIRE_BROWSER", "AUTO_CODER_IMMEDIATE_TEST_FAILURES")
LOG_FORMAT = "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level}</level> | {file.name}:{function}:{line} | {message}"


def _logging(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stdout, format=LOG_FORMAT, colorize=True)
    logger.add(directory / "source-ci.log", format=LOG_FORMAT, rotation="10 MB", retention=5)


def _write_new(path: Path, data: dict[str, object]) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError(f"immutable file already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = stream.name
            stream.write(_canonical(data))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _context(args: argparse.Namespace) -> None:
    sha = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    if sha != args.source_sha:
        raise ValueError("actual tested checkout is not the requested head/main SHA")
    origin: dict[str, object] = {
        "repository": args.repository,
        "workflow": WORKFLOW_NAME,
        "run_id": str(args.run_id),
        "run_attempt": str(args.run_attempt),
        "planning_attempt": f"source-plan-{args.run_id}-{args.run_attempt}",
        "event_sha": args.event_sha,
    }
    value = {
        "schema_version": CONTEXT_SCHEMA,
        "repository": args.repository,
        "profile_id": "source-required-v1",
        "checkout_sha": sha,
        "pytest_args": list(SELECTORS),
        "harness_files": list(HARNESS),
        "semantic_environment": list(SEMANTIC_ENV),
        "origin": origin,
    }
    ShardContext.parse(value)
    _write_new(args.output, value)
    logger.info("Source plan context created for actual SHA {} and attempt {}", sha, args.run_attempt)


def _matrix(args: argparse.Namespace) -> None:
    plan, _raw = read_plan(args.plan)
    matrix = {"shard": list(range(1, len(plan.shards) + 1))}
    with args.github_output.open("a", encoding="utf-8") as output:
        output.write(f"matrix={json.dumps(matrix, separators=(',', ':'))}\n")
        output.write(f"count={len(plan.shards)}\n")
    logger.info("Source plan {} covers {} nodes in {} required shards", plan.digest, len(plan.inventory), len(plan.shards))


def _duration(args: argparse.Namespace) -> None:
    """Prepare explicit timing manifests; a caller runs the existing optional CLI."""
    plan, _raw = read_plan(args.plan)
    profile = plan.profile
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    _write_new(args.directory / "context.json", {"schema_version": DURATION_CONTEXT_SCHEMA, "profile": profile, "evaluation_time_utc": now})
    manifests = []
    for shard in range(1, len(plan.shards) + 1):
        artifact = args.artifacts / _artifact_name(int(plan.context.origin["run_id"]), int(plan.context.origin["run_attempt"]), shard)
        timing = sorted((artifact / "timing").glob("timing-*.jsonl"))
        if not timing:
            logger.warning("No native timing batch found for source shard {}", shard)
            continue
        origin = {
            "kind": "github-actions",
            "checkout_sha": plan.context.checkout_sha,
            "event_sha": plan.context.origin["event_sha"],
            "workflow": WORKFLOW_NAME,
            "run_id": plan.context.origin["run_id"],
            "run_attempt": plan.context.origin["run_attempt"],
            "job_key": SOURCE_JOB,
            "invocation_id": f"source-{plan.context.origin['run_id']}-{plan.context.origin['run_attempt']}-{shard}",
        }
        manifest = args.directory / f"batch-{shard}.json"
        _write_new(manifest, {"schema_version": BATCH_SCHEMA, "profile": profile, "origin": origin, "files": [os.path.relpath(path, manifest.parent) for path in timing]})
        manifests.append(manifest)
    if not manifests:
        raise ValueError("no source timing batches available for optional update")
    command = [sys.executable, "scripts/update_ci_duration_profile.py", "--context", str(args.directory / "context.json")]
    for manifest in manifests:
        command.extend(("--batch", str(manifest)))
    if args.previous.is_file():
        command.extend(("--previous", str(args.previous)))
    command.extend(("--output", str(args.output), "--log-dir", str(args.directory / "logs")))
    subprocess.run(command, check=True)
    logger.info("Optional native source duration profile saved: {}", args.output)


def _report(args: argparse.Namespace) -> None:
    plan, _raw = read_plan(args.plan)
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ValueError("Actions read-only token unavailable for diagnostic metrics")
    client = GitHubGitDataClient(token, args.repository)
    verified = False
    try:
        verify_current_source_attempt(client, args.run_id, args.run_attempt, args.source_sha, args.plan, args.artifacts)
        verified = True
    except (SourceEvidenceError, ValueError, OSError) as error:
        logger.warning("Source timing report is not qualified: {}", error)
    metrics = build_report(client, plan, args.artifacts, args.legacy_snapshot, args.run_id, args.run_attempt, verified)
    _write_new(args.output, metrics)
    with args.github_summary.open("a", encoding="utf-8") as summary:
        summary.write("\n### Complete source timing (diagnostic)\n\n")
        summary.write(f"- Exact tested source: `{plan.context.checkout_sha}`; nodes: {len(plan.inventory)}; shards: {len(plan.shards)}\n")
        summary.write(f"- Current-attempt native qualification: **{metrics['qualification']}**\n")
        summary.write(f"- Test-step shard critical path: {metrics['native_test_critical_path_seconds']} s; total shard runner work: {metrics['total_runner_work_seconds']} s\n")
        summary.write(f"- End-to-end required-check latency: {metrics['end_to_end_required_check_seconds']} s (null while the aggregate is still running)\n")
        summary.write("- The 420 s work budget and 600 s end-to-end target are estimates, not enforced deadlines. Queue/setup are separate per-shard values in the artifact.\n")
        if "same_inventory_native_comparison" in metrics:
            comparison = metrics["same_inventory_native_comparison"]
            assert isinstance(comparison, dict)
            summary.write(
                f"- Same-inventory native counterfactual: planned {comparison['current_planned_shards_native_critical_path_seconds']} s vs prior four-way assignments {comparison['former_four_shards_native_critical_path_seconds']} s. This excludes queue/setup and is not a measured old CI run.\n"
            )
    logger.info("Source CI metrics retained: {}", args.output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    context = commands.add_parser("context")
    context.add_argument("--repository", required=True)
    context.add_argument("--source-sha", required=True)
    context.add_argument("--event-sha", required=True)
    context.add_argument("--run-id", type=int, required=True)
    context.add_argument("--run-attempt", type=int, required=True)
    context.add_argument("--output", type=Path, required=True)
    context.set_defaults(action=_context)
    matrix = commands.add_parser("matrix")
    matrix.add_argument("--plan", type=Path, required=True)
    matrix.add_argument("--github-output", type=Path, required=True)
    matrix.set_defaults(action=_matrix)
    duration = commands.add_parser("duration")
    duration.add_argument("--plan", type=Path, required=True)
    duration.add_argument("--artifacts", type=Path, required=True)
    duration.add_argument("--directory", type=Path, required=True)
    duration.add_argument("--previous", type=Path, required=True)
    duration.add_argument("--output", type=Path, required=True)
    duration.set_defaults(action=_duration)
    report = commands.add_parser("report")
    report.add_argument("--repository", required=True)
    report.add_argument("--source-sha", required=True)
    report.add_argument("--run-id", type=int, required=True)
    report.add_argument("--run-attempt", type=int, required=True)
    report.add_argument("--plan", type=Path, required=True)
    report.add_argument("--artifacts", type=Path, required=True)
    report.add_argument("--legacy-snapshot", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    report.add_argument("--github-summary", type=Path, required=True)
    report.set_defaults(action=_report)
    args = parser.parse_args(argv)
    try:
        _logging(args.log_dir)
        args.action(args)
        return 0
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        logger.error("Source CI {} failed: {}", args.command, error)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
