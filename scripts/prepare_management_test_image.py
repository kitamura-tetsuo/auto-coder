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


def prepare_image(repository: Path, github_env: Path | None = None) -> str:
    identity = resolve_build_identity(repository)
    if identity.state != "clean" or not identity.source_revision:
        raise RuntimeError("Management test fixtures require a complete clean source identity")
    encoded = base64.b64encode(serialize_build_identity(identity).encode()).decode()
    tag = f"auto-coder:management-fixture-{identity.source_revision[:12]}"
    subprocess.run(
        ["docker", "build", "--target", "python-environment", "--build-arg", f"AUTO_CODER_BUILD_IDENTITY_B64={encoded}", "--tag", tag, str(repository)],
        check=True,
    )
    image_id = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", tag], text=True).strip()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        raise RuntimeError("Management fixture Docker identity is unavailable or malformed")
    observation = json.loads(
        subprocess.check_output(
            ["docker", "run", "--rm", "--network", "none", "--read-only", "--entrypoint", "/opt/auto-coder/.venv/bin/python", image_id, "-m", "auto_coder.build_provenance"],
            text=True,
        )
    )
    source = observation.get("source_revision") if isinstance(observation, dict) else None
    version = observation.get("distribution_version") if isinstance(observation, dict) else None
    if not isinstance(source, dict) or not isinstance(version, dict) or source.get("value") != identity.source_revision or version.get("value") != identity.version:
        raise RuntimeError("Management fixture installed provenance differs from the clean source")
    if github_env is not None:
        with github_env.open("a", encoding="utf-8") as output:
            output.write(f"AUTOCODER_FRESH_TEST_BASE_IMAGE={image_id}\n")
    logger.info("Prepared Python-only management test fixture source={} image={}", identity.source_revision, image_id)
    return image_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    args = parser.parse_args()
    prepare_image(args.repository.resolve(), Path(os.environ["GITHUB_ENV"]) if os.environ.get("GITHUB_ENV") else None)


if __name__ == "__main__":
    main()
