"""Real pytest-split checks for bounded PR shard duration inputs."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
from scripts.run_pr_test_shard import _normalized_durations

ROOT = Path(__file__).resolve().parents[1]
SELECTOR = "not browser and not opencode_live and not muse_live and not supervisor_integration"


def test_reviewed_snapshot_matches_its_provenance() -> None:
    data = ROOT / "tests/data/pr-test-durations.json"
    provenance = json.loads((ROOT / "tests/data/pr-test-durations.provenance.json").read_text())
    assert hashlib.sha256(data.read_bytes()).hexdigest() == provenance["snapshot_sha256"]
    assert len(_normalized_durations(data)) == provenance["nodes"]


@pytest.mark.parametrize("text", ["[]", '{"x": -1}', '{"x": NaN}', '{"x": Infinity}', '{"x": true}', '{"x": "1"}', '{"x": 1, "x": 2}', '{"x": 1e308, "y": 1e308}'])
def test_invalid_duration_input_fails_closed(tmp_path: Path, text: str) -> None:
    snapshot = tmp_path / "durations.json"
    snapshot.write_text(text)
    with pytest.raises(ValueError):
        _normalized_durations(snapshot)


def test_missing_duration_snapshot_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _normalized_durations(tmp_path / "missing.json")


def test_zero_is_floored(tmp_path: Path) -> None:
    snapshot = tmp_path / "durations.json"
    snapshot.write_text('{"x": 0}')
    assert _normalized_durations(snapshot) == {"x": 0.001}


def test_invalid_snapshot_stops_production_runner_before_launch(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests/data").mkdir(parents=True)
    shutil.copy(ROOT / "scripts/run_pr_test_shard.py", tmp_path / "scripts")
    (tmp_path / "tests/data/pr-test-durations.json").write_text('{"x": -1}')
    (tmp_path / "scripts/test.sh").write_text("#!/bin/bash\ntouch launched\n")
    result = subprocess.run([sys.executable, "scripts/run_pr_test_shard.py", "--group", "3", "--log-dir", "logs"], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "duration snapshot unavailable or invalid" in result.stderr
    assert not (tmp_path / "launched").exists()


def _collect(directory: Path, snapshot: Path, group: int | None) -> list[str]:
    args = [sys.executable, "-m", "pytest", "-p", "pytest_split.plugin", "--collect-only", "-q"]
    if group is not None:
        args += ["--splits", "4", "--group", str(group), "--splitting-algorithm", "duration_based_chunks", "--durations-path", str(snapshot)]
    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""}
    result = subprocess.run(args, cwd=directory, env=env, capture_output=True, text=True, timeout=30, check=True)
    return [line.strip() for line in result.stdout.splitlines() if line.startswith("test_sample.py::")]


def test_real_plugin_partition_unknown_stale_and_empty_weights(tmp_path: Path) -> None:
    (tmp_path / "test_sample.py").write_text("\n".join(f"def test_case_{i}(): pass" for i in range(8)))
    snapshot = tmp_path / "weights.json"
    names = _collect(tmp_path, snapshot, None)
    assert len(names) == 8

    def groups(weights: dict[str, float]) -> list[list[str]]:
        snapshot.write_text(json.dumps(weights))
        return [_collect(tmp_path, snapshot, group) for group in range(1, 5)]

    baseline = groups({names[0]: 2.0, names[1]: 4.0})
    assert baseline == groups({names[0]: 2.0, names[1]: 4.0, "removed.py::test_old": 10000.0})
    assert baseline == groups({names[0]: 2.0, names[1]: 4.0, **{name: 3.0 for name in names[2:]}})
    empty = groups({})
    assert [len(group) for group in empty] == [2, 2, 2, 2]
    snapshot.write_text(json.dumps({name: 0 for name in names}))
    zero = groups(_normalized_durations(snapshot))
    assert [len(group) for group in zero] == [2, 2, 2, 2]
    for partition in (baseline, empty, zero):
        assert Counter(names) == Counter(item for group in partition for item in group)
        assert sum(len(set(group)) for group in partition) == len(set().union(*map(set, partition)))
        for group in partition:
            selected = set(group)
            assert group == [name for name in names if name in selected]


def test_production_collection_is_complete_disjoint_and_ordered() -> None:
    env = {**os.environ, "PYTEST_ADDOPTS": ""}

    def collect(group: int | None) -> list[str]:
        args = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", SELECTOR]
        if group is not None:
            args += ["--splits", "4", "--group", str(group), "--splitting-algorithm", "duration_based_chunks", "--durations-path", "tests/data/pr-test-durations.json"]
        result = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120, check=True)
        return [line.strip() for line in result.stdout.splitlines() if line.startswith("tests/") and "::" in line]

    all_nodes = collect(None)
    groups = [collect(group) for group in range(1, 5)]
    assert all_nodes
    assert Counter(all_nodes) == Counter(node for group in groups for node in group)
    assert sum(len(set(group)) for group in groups) == len(set().union(*map(set, groups)))
    for group in groups:
        selected = set(group)
        assert group == [node for node in all_nodes if node in selected]
