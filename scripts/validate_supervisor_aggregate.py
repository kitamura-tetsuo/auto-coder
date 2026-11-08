"""Validate the complete native Supervisor integration execution set."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from loguru import logger
from supervisor_aggregate_evidence import REQUIRED_GROUPS, evaluate
from supervisor_ci_profiles import REPOSITORY
from supervisor_native_evidence import NativeEvidenceError

from auto_coder.util.gh_cache import GitHubGitDataClient


def main() -> int:
    """Fail unless every native constituent and receipt is matched exactly."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--plans", type=Path, default=Path("evidence/plans"))
    parser.add_argument("--artifacts", type=Path, default=Path("evidence/shards"))
    parser.add_argument("--metrics", type=Path, default=Path("supervisor-integration-logs/native-metrics.json"))
    parser.add_argument("--selections", type=Path, default=Path("supervisor-integration-logs/validated-selections.json"))
    args = parser.parse_args()
    logs = args.metrics.parent
    logs.mkdir(parents=True, exist_ok=True)
    log_format = "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level}</level> | {file}:{function}:{line} | {message}"
    logger.remove()
    logger.add(sys.stdout, format=log_format, colorize=True)
    logger.add(logs / "supervisor-aggregate.log", format=log_format, colorize=False, rotation="10 MB", retention=5)
    try:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        selections, metrics = evaluate(
            GitHubGitDataClient(os.environ.get("GITHUB_TOKEN", ""), REPOSITORY),
            json.loads(os.environ["REQUIRED_RESULTS"]),
            args.plans,
            args.artifacts,
            sha,
            os.environ["GITHUB_SHA"],
            int(os.environ["GITHUB_RUN_ID"]),
            int(os.environ["GITHUB_RUN_ATTEMPT"]),
        )
        args.metrics.parent.mkdir(parents=True, exist_ok=True)
        args.metrics.write_text(json.dumps(metrics, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        args.selections.parent.mkdir(parents=True, exist_ok=True)
        args.selections.write_text(json.dumps([{"profile": item.profile, "shard": item.shard, "producing_attempt": item.producing_attempt, "job_id": item.receipt["job_id"], "artifact": item.artifact.name} for item in selections], sort_keys=True, indent=2) + "\n", encoding="utf-8")
        logger.info("Validated {} original native executions, exact profile partitions, and source/attempt identities", len(selections))
        logger.info("Supervisor queue/setup/test/overlap/wave/runner metrics: {}", json.dumps(metrics, sort_keys=True))
    except (NativeEvidenceError, OSError, ValueError, KeyError, RuntimeError) as error:
        logger.error("Supervisor aggregate refused: {}", error)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
