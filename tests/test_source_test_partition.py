"""Production collection oracle for the complete source-test partition."""

from __future__ import annotations

import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

from auto_coder.ci_shard_plan import ShardContext, build_plan, verify_plan

ROOT = Path(__file__).resolve().parents[1]


def _collect() -> list[str]:
    command = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", "not opencode_live"]
    result = subprocess.run(
        command,
        cwd=ROOT,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.startswith("tests/") and "::" in line]


def test_complete_source_selection_is_partitioned_once_by_immutable_plan() -> None:
    complete = _collect()
    context = ShardContext(
        "kitamura-tetsuo/auto-coder",
        "source-required-v1",
        "a" * 40,
        ("-m", "not opencode_live"),
        (),
        (),
        {"repository": "kitamura-tetsuo/auto-coder"},
    )
    profile = {"repository": context.repository, "profile_id": context.profile_id, "configuration_digest": "sha256:" + "f" * 64, "python_major_minor": "3.12"}
    planned = verify_plan(build_plan(context, profile, tuple(complete), None))
    shards = planned.shards
    flattened = [node for shard in shards for node in shard]

    assert complete
    assert all(shards)
    assert len(shards) <= 12
    assert Counter(flattened) == Counter(complete)
    assert len(flattened) == len(set(flattened))
