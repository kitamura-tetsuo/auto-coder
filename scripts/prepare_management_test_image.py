#!/usr/bin/env python3
"""Prepare the Python-only management fixture for complete publication tests."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
from pathlib import Path

from auto_coder.build_identity import resolve_build_identity, serialize_build_identity
from auto_coder.logger_config import get_logger

logger = get_logger(__name__)


def _dirty_checkout_details(repository: Path) -> str:
    """Report bounded Git status evidence without weakening clean-source admission."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), "status", "--porcelain=v1", "--untracked-files=normal"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "git status unavailable"
    entries = result.stdout.splitlines()
    return f"git_status={entries[:20]!r}" + (" (truncated)" if len(entries) > 20 else "")


def verify_image(repository: Path, image_id: str) -> str:
    """Recheck the immutable image actually used by a retained-startup consumer."""
    identity = resolve_build_identity(repository)
    if identity.state != "clean" or not identity.source_revision:
        detail = f"state={identity.state}, reason={getattr(identity, 'reason', None)}"
        if identity.state == "dirty":
            detail += f"; {_dirty_checkout_details(repository)}"
        raise RuntimeError(f"Management test fixtures require a complete clean source identity ({detail})")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        raise RuntimeError("Management fixture Docker identity is unavailable or malformed")
    inspected_id = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", image_id], text=True).strip()
    if inspected_id != image_id:
        raise RuntimeError("Management fixture consumed image differs from inspected immutable identity")
    observation = json.loads(
        subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                "--pull",
                "never",
                "--network",
                "none",
                "--read-only",
                "--entrypoint",
                "/opt/auto-coder/.venv/bin/python",
                image_id,
                "-m",
                "auto_coder.build_provenance",
            ],
            text=True,
        )
    )
    source = observation.get("source_revision") if isinstance(observation, dict) else None
    version = observation.get("distribution_version") if isinstance(observation, dict) else None
    if not isinstance(source, dict) or not isinstance(version, dict) or source.get("value") != identity.source_revision or version.get("value") != identity.version:
        raise RuntimeError("Management fixture installed provenance differs from the clean source")
    logger.info("Verified management fixture checkout={} image={} version={}", identity.source_revision, image_id, identity.version)
    return image_id


def prepare_image(repository: Path, github_env: Path | None = None) -> str:
    identity = resolve_build_identity(repository)
    if identity.state != "clean" or not identity.source_revision:
        raise RuntimeError(f"Management test fixtures require a complete clean source identity (state={identity.state}, reason={getattr(identity, 'reason', None)})")
    encoded = base64.b64encode(serialize_build_identity(identity).encode()).decode()
    tag = f"auto-coder:management-fixture-{identity.source_revision[:12]}"
    dockerfile = (repository / "Dockerfile").read_text() + (
        "\nFROM python-environment AS management-test-fixture\n" 'ENV PATH="/opt/auto-coder/.venv/bin:${PATH}"\n' "ARG AUTO_CODER_SOURCE_REVISION\n" "RUN AUTO_CODER_SOURCE_REVISION=$AUTO_CODER_SOURCE_REVISION /opt/auto-coder/.venv/bin/python -B -m auto_coder.build_provenance embed\n"
    )
    subprocess.run(
        ["docker", "build", "--file", "-", "--target", "management-test-fixture", "--build-arg", f"AUTO_CODER_BUILD_IDENTITY_B64={encoded}", "--build-arg", f"AUTO_CODER_SOURCE_REVISION={identity.source_revision}", "--tag", tag, str(repository)],
        input=dockerfile,
        text=True,
        check=True,
    )
    image_id = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", tag], text=True).strip()
    verify_image(repository, image_id)
    # The production image puts the installed virtualenv first on PATH. The
    # Python-only CI stage must run the same interpreter when tests request
    # `python3` as PID 1 or as a separately executing non-owner process.
    selected_python = subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--entrypoint",
            "python3",
            image_id,
            "-B",
            "-c",
            "import sys, auto_coder.management.bootstrap_supervisor; print(sys.prefix)",
        ],
        text=True,
        timeout=30,
    ).splitlines()
    if not selected_python or selected_python[-1] != "/opt/auto-coder/.venv":
        raise RuntimeError("Python-only management fixture does not select installed runtime dependencies")
    if github_env is not None:
        with github_env.open("a", encoding="utf-8") as output:
            # Dockerfile FROM requires the local image tag; callers that execute
            # the fixture resolve and verify the immutable image ID themselves.
            output.write(f"AUTOCODER_FRESH_TEST_BASE_IMAGE={tag}\n")
    logger.info("Prepared Python-only management test fixture source={} image={}", identity.source_revision, image_id)
    return image_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    args = parser.parse_args()
    prepare_image(args.repository.resolve(), Path(os.environ["GITHUB_ENV"]) if os.environ.get("GITHUB_ENV") else None)


if __name__ == "__main__":
    main()
