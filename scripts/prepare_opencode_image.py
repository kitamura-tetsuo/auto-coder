#!/usr/bin/env python3
"""Prepares the OpenCode production runtime container image for dedicated CI (Issue #2152).

Builds the container image from the actual checked-out Dockerfile and application
source, records provenance diagnostics, and exports the resolved image identity
to GitHub Actions environment variables and persistent log files.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from loguru import logger


def _init_logging(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    prep_log = log_dir / "image-preparation.log"
    logger.remove()
    logger.add(
        sys.stdout,
        colorize=True,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {file.name}:{function}:{line} | {message}",
    )
    logger.add(
        prep_log,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {file.name}:{function}:{line} | {message}",
        enqueue=False,
    )
    return prep_log


def _record_diagnostic(
    log_dir: Path,
    *,
    status: str,
    commit: str = "unknown",
    image_tag: str = "",
    image_id: str = "",
    duration: float = 0.0,
    error: str = "",
) -> None:
    identity_file = log_dir / "image-identity.json"
    data = {
        "status": status,
        "checkout_commit": commit,
        "image_tag": image_tag,
        "image_id": image_id,
        "preparation_duration_seconds": round(duration, 3),
        "error": error,
    }
    identity_file.write_text(json.dumps(data, indent=2), encoding="utf-8")


def prepare_image(log_dir: Path, custom_tag: str | None = None, github_env_file: str | None = None) -> int:
    prep_log = _init_logging(log_dir)
    started = time.monotonic()
    logger.info("Starting OpenCode runtime image preparation...")

    # 1. Verify Docker daemon is available
    logger.info("Verifying Docker daemon connectivity...")
    docker_check = subprocess.run(["docker", "info"], capture_output=True, text=True)
    if docker_check.returncode != 0:
        err = f"Docker daemon is unavailable: {docker_check.stderr.strip() or docker_check.stdout.strip()}"
        logger.error(err)
        _record_diagnostic(log_dir, status="failed", error=err, duration=time.monotonic() - started)
        return 1

    # 2. Resolve checked-out commit
    try:
        commit_res = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
        commit = commit_res.stdout.strip()
    except subprocess.SubprocessError as exc:
        err = f"Failed to determine git commit SHA: {exc}"
        logger.error(err)
        _record_diagnostic(log_dir, status="failed", error=err, duration=time.monotonic() - started)
        return 1

    tag = custom_tag or f"auto-coder:opencode-{commit[:12]}"
    logger.info("Resolved checkout commit: {}", commit)
    logger.info("Target image tag: {}", tag)

    # 3. Build image from current checkout
    build_cmd = [
        "docker",
        "build",
        "--build-arg",
        f"AUTO_CODER_SOURCE_REVISION={commit}",
        "-t",
        tag,
        ".",
    ]
    logger.info("Executing build: {}", " ".join(build_cmd))

    with prep_log.open("a", encoding="utf-8") as f_log:
        f_log.write(f"\n--- Build Invocation: {' '.join(build_cmd)} ---\n")
        proc = subprocess.Popen(
            build_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            f_log.write(line)
        proc.wait()

    if proc.returncode != 0:
        err = f"docker build failed with exit code {proc.returncode}"
        logger.error(err)
        _record_diagnostic(
            log_dir,
            status="failed",
            commit=commit,
            image_tag=tag,
            duration=time.monotonic() - started,
            error=err,
        )
        return 1

    # 4. Resolve image ID
    inspect_res = subprocess.run(
        ["docker", "inspect", "--format={{.Id}}", tag],
        capture_output=True,
        text=True,
    )
    if inspect_res.returncode != 0:
        err = f"Failed to inspect built image {tag}: {inspect_res.stderr.strip()}"
        logger.error(err)
        _record_diagnostic(
            log_dir,
            status="failed",
            commit=commit,
            image_tag=tag,
            duration=time.monotonic() - started,
            error=err,
        )
        return 1

    image_id = inspect_res.stdout.strip()
    duration = time.monotonic() - started
    logger.success(
        "Successfully prepared image '{}' (id: {}) in {:.3f}s",
        tag,
        image_id,
        duration,
    )

    # 5. Record diagnostic metadata
    _record_diagnostic(
        log_dir,
        status="success",
        commit=commit,
        image_tag=tag,
        image_id=image_id,
        duration=duration,
    )

    # 6. Export to GITHUB_ENV if provided
    env_path = github_env_file or os.environ.get("GITHUB_ENV")
    if env_path and Path(env_path).parent.exists():
        try:
            with Path(env_path).open("a", encoding="utf-8") as f_env:
                f_env.write(f"AUTOCODER_OPENCODE_IMAGE={tag}\n")
                f_env.write(f"AUTO_CODER_OPENCODE_IMAGE={tag}\n")
                f_env.write(f"AUTOCODER_OPENCODE_IMAGE_ID={image_id}\n")
            logger.info("Exported image identity to GITHUB_ENV ({})", env_path)
        except OSError as exc:
            logger.warning("Could not write to GITHUB_ENV: {}", exc)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare OpenCode runtime image for dedicated CI")
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path("opencode-live-logs"),
        help="Directory to store preparation logs and metadata",
    )
    parser.add_argument("--tag", type=str, default=None, help="Explicit tag for the image")
    parser.add_argument(
        "--github-env",
        type=str,
        default=None,
        help="Path to GITHUB_ENV file to append exported variables",
    )
    args = parser.parse_args()
    return prepare_image(args.log_dir, args.tag, args.github_env)


if __name__ == "__main__":
    raise SystemExit(main())
