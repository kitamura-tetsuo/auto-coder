"""Production collection oracle for the complete source-test partition."""

from __future__ import annotations

import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _collect(group: int | None) -> list[str]:
    command = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", "not opencode_live"]
    if group is not None:
        command.extend(
            [
                "--splits",
                "4",
                "--group",
                str(group),
                "--splitting-algorithm",
                "duration_based_chunks",
                "--durations-path",
                "tests/data/pr-test-durations.json",
            ]
        )
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


def test_complete_source_selection_is_partitioned_once_across_four_groups() -> None:
    complete = _collect(None)
    shards = [_collect(group) for group in range(1, 5)]
    flattened = [node for shard in shards for node in shard]

    assert complete
    assert all(shards)
    assert Counter(flattened) == Counter(complete)
    assert len(flattened) == len(set(flattened))
