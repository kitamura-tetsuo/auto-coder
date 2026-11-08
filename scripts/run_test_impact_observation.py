"""Run the unsharded ordinary PR pytest profile with opt-in coverage contexts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from loguru import logger

PROFILE = "ordinary-pr-full-v1"
SELECTOR = "not browser and not opencode_live and not muse_live and not supervisor_integration"
WORKFLOW_PATH = ".github/workflows/test-impact-observation.yml"


def _digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _version(package: str) -> str | None:
    try:
        return version(package)
    except PackageNotFoundError:
        return None


def _manifest(repository: str, source_ref: str, invocation_id: str) -> dict[str, object]:
    root = Path.cwd()
    try:
        uv_version = subprocess.run(["uv", "--version"], check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        uv_version = None
    return {
        "schema_version": 1,
        "invocation_id": invocation_id,
        "repository": repository,
        "source_ref": source_ref,
        "checkout_sha": _git("rev-parse", "HEAD"),
        "checkout_tree": _git("rev-parse", "HEAD^{tree}"),
        "event_sha": os.environ.get("GITHUB_SHA"),
        "workflow_path": WORKFLOW_PATH,
        "workflow_event": os.environ.get("GITHUB_EVENT_NAME"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "profile": PROFILE,
        "selector": SELECTOR,
        "versions": {
            "python": platform.python_version(),
            "pytest": _version("pytest"),
            "pytest_cov": _version("pytest-cov"),
            "coverage": _version("coverage"),
            "uv": uv_version,
        },
        "digests": {
            name: _digest(root / name)
            for name in (
                "uv.lock",
                "pyproject.toml",
                ".coveragerc",
                "pytest.ini",
                "setup.cfg",
                "tox.ini",
                "tests/ci_impact_plugin.py",
                "scripts/summarize_test_impact.py",
                "scripts/run_test_impact_observation.py",
            )
        },
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def run(directory: Path, repository: str, source_ref: str) -> int:
    invocation_id = directory.name
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"Test-impact evidence directory unavailable: {exc}", file=sys.stderr, flush=True)
    diagnostics_ready = directory.is_dir()
    logger.remove()
    logger.add(sys.stdout, format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | {file}:{function}:{line} | {message}", colorize=True)
    try:
        logger.add(directory / "observer.log", rotation="10 MB", format="{time} | {level} | {file}:{function}:{line} | {message}")
    except OSError as exc:
        logger.warning(f"Diagnostic log unavailable: {exc}")
    try:
        (directory / "manifest.json").write_text(json.dumps(_manifest(repository, source_ref, invocation_id), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception as exc:
        logger.error(f"Test-impact manifest unavailable: {type(exc).__name__}: {exc}")

    command = [sys.executable, "-m", "pytest"]
    if diagnostics_ready:
        command.extend(["-p", "tests.ci_impact_plugin", "--cov=.", "--cov-context=test", "--cov-report=", "--cov-fail-under=0"])
    else:
        logger.warning("Diagnostic directory unusable; running the same native selection without the observer")
    command.extend(["-m", SELECTOR])
    logger.info(f"Executing native pytest with complete profile {PROFILE}")
    environment = {
        **os.environ,
        "AUTO_CODER_TEST_IMPACT_DIR": str(directory),
        "AUTO_CODER_TEST_IMPACT_INVOCATION": invocation_id,
        "COVERAGE_FILE": str(directory / "coverage.db"),
    }
    try:
        exit_code = subprocess.call(command, env=environment)
    except OSError as exc:
        logger.error(f"Native pytest could not start: {exc}")
        exit_code = 127
    logger.info(f"Native pytest exit code: {exit_code}")
    try:
        (directory / "native-exit.json").write_text(json.dumps({"invocation_id": invocation_id, "exit_code": exit_code}) + "\n", encoding="utf-8")
    except Exception as exc:
        logger.error(f"Native exit receipt unavailable: {type(exc).__name__}: {exc}")
    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-ref", required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.directory.resolve(), args.repository, args.source_ref))


if __name__ == "__main__":
    main()
