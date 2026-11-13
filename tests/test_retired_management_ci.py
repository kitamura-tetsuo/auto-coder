"""CI retirement must not silently remove the retained development gates."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_retired_native_management_workflows_are_not_scheduled() -> None:
    workflows = ROOT / ".github/workflows"
    retired_names = {"Management Recovery Epoch Acceptance", "NEW Docker Private Engine Acceptance"}
    for path in workflows.glob("*.yml"):
        workflow = yaml.safe_load(path.read_text())
        assert workflow["name"] not in retired_names
    assert not (workflows / "management-recovery-epoch.yml").exists()
    assert not (workflows / "new-instance-private-engine.yml").exists()


def test_development_and_mixed_supervisor_gates_remain() -> None:
    for filename in (
        "pr-tests.yml",
        "browser-tests.yml",
        "opencode-live-tests.yml",
        "muse-live-tests.yml",
        "test-source-for-runtime.yml",
        "supervisor-integration-tests.yml",
    ):
        workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
        assert "pull_request" in workflow["on"]
        assert workflow["jobs"]


def test_supervisor_retains_worker_execution_and_evidence_gate() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/supervisor-integration-tests.yml").read_text())
    assert set(workflow["jobs"]) == {"worker-bridge", "supervisor-integration"}
    worker = workflow["jobs"]["worker-bridge"]
    assert not worker.get("continue-on-error", False)
    steps = {step["name"]: step for step in worker["steps"]}
    execution = steps["Test real worker processes and management bridge"]
    assert "set -o pipefail" in execution["run"]
    assert execution["run"].count("-p supervisor_ci_results") == 2
    assert not execution.get("continue-on-error", False)
    receipt = steps["Bind native worker result to the producing job"]
    assert receipt["if"] == "always()"
    assert not receipt.get("continue-on-error", False)
    assert "--profile worker" in receipt["run"]
    assert "--inventory supervisor-integration-logs/worker-bridge.json" in receipt["run"]
    assert "--inventory supervisor-integration-logs/worker-daemon.json" in receipt["run"]


def test_supervisor_aggregate_rejects_every_non_success_result(_use_real_commands) -> None:
    import os
    import subprocess

    workflow = yaml.safe_load((ROOT / ".github/workflows/supervisor-integration-tests.yml").read_text())
    aggregate = workflow["jobs"]["supervisor-integration"]
    assert aggregate["if"] == "always()"
    assert aggregate["needs"] == ["worker-bridge"]
    assert not aggregate.get("continue-on-error", False)
    assert len(aggregate["steps"]) == 1
    step = aggregate["steps"][0]
    assert step["env"] == {"WORKER_RESULT": "${{ needs.worker-bridge.result }}"}
    assert not step.get("continue-on-error", False)
    for result in ("success", "failure", "cancelled", "skipped", "", "unknown"):
        process = subprocess.run(["bash", "-c", step["run"]], env={**os.environ, "WORKER_RESULT": result}, capture_output=True)
        assert (process.returncode == 0) is (result == "success")
