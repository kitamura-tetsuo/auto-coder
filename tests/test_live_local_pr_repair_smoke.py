"""Deterministic contract checks for the opt-in live smoke harness.

These tests intentionally do not claim to be the credentialed live E2E.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "live_local_pr_repair_smoke.py"
SPEC = importlib.util.spec_from_file_location("live_local_pr_repair_smoke", SCRIPT)
assert SPEC and SPEC.loader
smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke
SPEC.loader.exec_module(smoke)


@pytest.mark.parametrize("repository", sorted(smoke.FORBIDDEN_REPOSITORIES))
def test_production_repository_is_refused_before_live_work(repository, tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(smoke.Blocked, match="must not"):
        smoke.validate_target(repository, tmp_path)


def test_ci_requires_terminal_failure_on_captured_head():
    snapshot = smoke.Snapshot("h1", "b1", True, 7, "success")
    with pytest.raises(smoke.Blocked, match="terminal-failing"):
        smoke.require_initial_state("ci", snapshot)


def test_known_empty_is_only_admitted_for_affirmative_conflict():
    snapshot = smoke.Snapshot("h1", "b1", False, None, "KNOWN_EMPTY")
    smoke.require_initial_state("conflict", snapshot)
    with pytest.raises(smoke.Blocked):
        smoke.require_initial_state("ci", snapshot)


def test_wait_requires_new_head_and_terminal_current_head_run():
    snapshots = iter(
        [
            smoke.Snapshot("h1", "b1", False, 1, "failure"),
            smoke.Snapshot("h2", "b1", True, 2, "in_progress"),
            smoke.Snapshot("h2", "b1", True, 2, "success"),
        ]
    )
    observer = SimpleNamespace(snapshot=lambda _pr: next(snapshots))
    result = smoke.wait_for_result(observer, 42, "h1", 30, sleep=lambda _seconds: None)
    assert result.head == "h2"
    assert result.run_id == 2
    assert result.conclusion == "success"
