"""Materialize a diagnostic-only, immutable rolling CI duration snapshot."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from loguru import logger

from auto_coder.ci_duration_profile import ProfileContext, ProfileInputError, build_profile, write_profile

LOG_FORMAT = "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level}</level> | {file}:{function}:{line} | {message}"


def _logging(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stdout, format=LOG_FORMAT, colorize=True, catch=False)
    logger.add(directory / "ci-duration-profile.log", format=LOG_FORMAT, colorize=False, rotation="10 MB", retention=5, encoding="utf-8", catch=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create an immutable rolling CI duration profile from observed native pytest phases")
    parser.add_argument("--context", type=Path, required=True, help="Explicit profile identity and evaluation time JSON")
    parser.add_argument("--batch", type=Path, action="append", default=[], help="Repeatable native JSONL batch manifest")
    parser.add_argument("--previous", type=Path, action="append", default=[], help="Repeatable prior immutable profile snapshot")
    parser.add_argument("--output", type=Path, required=True, help="New immutable snapshot destination")
    parser.add_argument("--log-dir", type=Path, required=True, help="Directory for rotating diagnostics")
    args = parser.parse_args(argv)
    try:
        _logging(args.log_dir)
        context = ProfileContext.read(args.context)
        result = build_profile(context, args.batch, args.previous)
        write_profile(args.output, result, [args.context, *args.batch, *args.previous])
    except (ProfileInputError, OSError, ValueError) as error:
        logger.error("CI duration profile failed: {}: {}", type(error).__name__, error)
        return 2
    for item in result.diagnostics:
        logger.log("WARNING" if item.reasons else "INFO", "{} {}: {} ({} completed samples)", item.kind, item.source, item.status, item.samples)
        for reason in item.reasons:
            logger.warning("{}: {}", item.source, reason)
    logger.info("CI duration profile {}: {} measured nodes; output {}", result.payload["status"], len(result.payload["nodes"]), args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
