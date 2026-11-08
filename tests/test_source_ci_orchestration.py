"""Real source-selected pytest collection, native receipts and failure paths."""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from auto_coder.ci_shard_plan import ShardContext, read_plan
from auto_coder.source_ci_evidence import receipt_complete, validate_receipt
from tests.test_ci_shard_plan import _history, _inventory, _plan_cli, _project, _run

pytestmark = pytest.mark.usefixtures("_use_real_commands")

GOOD_TESTS = """
import pytest

@pytest.mark.parametrize("value", [1, 2], ids=["new-one", "new-two"])
def test_parameterized(value):
    assert value > 0

@pytest.fixture
def no_prerequisite():
    pytest.skip("documented prerequisite unavailable")

def test_prerequisite_skip(no_prerequisite):
    raise AssertionError("skip must precede call")

@pytest.mark.xfail(reason="known behavior")
def test_expected_failure():
    assert False
"""

BAD_TESTS = """
import pytest

def test_native_failure():
    assert False, "real pytest failure must not qualify"

@pytest.fixture
def teardown_error():
    yield
    raise RuntimeError("real teardown failure must not qualify")

def test_native_teardown_error(teardown_error):
    assert True
"""


def _source_project(tmp_path: Path, source: str) -> tuple[Path, Path, ShardContext]:
    project, context_path, previous = _project(tmp_path, source=source)
    context = ShardContext(
        previous.repository,
        "source-required-v1",
        previous.checkout_sha,
        ("-m", "not opencode_live"),
        previous.harness_files,
        (),
        {
            "repository": previous.repository,
            "workflow": "Test Source for Runtime",
            "run_id": "7",
            "run_attempt": "1",
            "planning_attempt": "source-plan-7-1",
            "event_sha": "f" * 40,
        },
    )
    context_path.write_text(json.dumps(context.json()), encoding="utf-8")
    return project, context_path, context


def _apply_receipt(project: Path, plan_path: Path, shard: int, receipt: Path, *, attempt: str = "1") -> subprocess.CompletedProcess[str]:
    return _run(
        project,
        "-m",
        "pytest",
        "-p",
        "scripts.ci_shard_plugin",
        "-p",
        "scripts.source_ci_receipt_plugin",
        f"--ci-plan={plan_path}",
        f"--ci-shard={shard}",
        f"--source-ci-receipt={receipt}",
        "-m",
        "not opencode_live",
        env_patch={"GITHUB_WORKFLOW": "Test Source for Runtime", "GITHUB_JOB": "complete-source-shard", "GITHUB_RUN_ID": "7", "GITHUB_RUN_ATTEMPT": attempt, "GITHUB_SHA": "f" * 40},
    )


def test_actual_source_plan_and_all_native_shard_receipts_cover_exact_full_inventory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, context_path, context = _source_project(tmp_path, GOOD_TESTS)
    profile, collected = _inventory(project, context_path, context, monkeypatch)
    assert any("new-one" in node for node in collected) and any("new-two" in node for node in collected)
    history = tmp_path / "profile.json"
    _history(history, profile, collected, seconds=250)
    plan_path = tmp_path / "plan.json"
    created = _plan_cli(project, context_path, plan_path, history, "-m", "not opencode_live")
    assert created.returncode == 0, created.stdout + created.stderr
    plan, _raw = read_plan(plan_path)
    assert len(plan.shards) == len(collected)
    observed: list[str] = []
    for shard in range(1, len(plan.shards) + 1):
        receipt = tmp_path / f"receipt-{shard}.json"
        applied = _apply_receipt(project, plan_path, shard, receipt)
        assert applied.returncode == 0, applied.stdout + applied.stderr
        data = json.loads(receipt.read_text(encoding="utf-8"))
        assert data["complete"] is True
        validate_receipt(data, plan, shard, 7, 1)
        assert receipt_complete(data, plan, shard)
        assert data["collected"] == list(collected)
        assert data["selected"] == data["assigned"] == data["executed"]
        observed.extend(data["executed"])
        if any("prerequisite_skip" in node for node in data["executed"]):
            assert [report["outcome"] for report in data["reports"]] == ["skipped", "passed"]
        if any("expected_failure" in node for node in data["executed"]):
            assert any(report["outcome"] == "skipped" and report["wasxfail"] for report in data["reports"])
    assert observed == list(collected) and Counter(observed) == Counter(collected)


def test_real_native_failures_and_teardown_errors_never_create_qualifying_receipts(tmp_path: Path) -> None:
    project, context_path, _context = _source_project(tmp_path, BAD_TESTS)
    plan_path = tmp_path / "plan.json"
    created = _plan_cli(project, context_path, plan_path, None, "-m", "not opencode_live")
    assert created.returncode == 0, created.stdout + created.stderr
    plan, _raw = read_plan(plan_path)
    receipt = tmp_path / "failed-receipt.json"
    run = _apply_receipt(project, plan_path, 1, receipt)
    assert run.returncode != 0, run.stdout + run.stderr
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["complete"] is False
    assert payload["native_exit_status"] != 0
    assert any(report["phase"] == "teardown" and report["outcome"] == "failed" for report in payload["reports"])
    with pytest.raises(ValueError, match="incomplete or failed"):
        validate_receipt(payload, plan, 1, 7, 1)


def test_wrong_producing_attempt_and_receipt_overwrite_are_native_failures(tmp_path: Path) -> None:
    project, context_path, _context = _source_project(tmp_path, "def test_one(): assert True\n")
    plan_path = tmp_path / "plan.json"
    assert _plan_cli(project, context_path, plan_path, None, "-m", "not opencode_live").returncode == 0
    wrong = _apply_receipt(project, plan_path, 1, tmp_path / "wrong.json", attempt="2")
    assert wrong.returncode != 0
    assert "actual producing Actions identity" in wrong.stderr
    receipt = tmp_path / "preexisting.json"
    receipt.write_text("user data", encoding="utf-8")
    overwritten = _apply_receipt(project, plan_path, 1, receipt)
    assert overwritten.returncode != 0
    assert receipt.read_text(encoding="utf-8") == "user data"
