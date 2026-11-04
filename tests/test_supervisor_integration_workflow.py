"""Static and executable guardrails for the parallel Supervisor integration gate."""

import copy
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
GROUPS = {"worker-bridge", "management-self-update", "fresh-host", "isolated-containers"}


def _workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/supervisor-integration-tests.yml").read_text())


def _step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def test_parallel_groups_and_stable_fail_closed_aggregate() -> None:
    workflow = _workflow()
    assert workflow["name"] == "Supervisor Integration Tests"
    assert set(workflow["on"]) == {"pull_request", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == GROUPS | {"supervisor-integration"}
    aggregate = workflow["jobs"]["supervisor-integration"]
    assert aggregate["name"] == "Supervisor Integration Tests"
    assert aggregate["if"] == "always()"
    assert set(aggregate["needs"]) == GROUPS
    assert _step(aggregate, "Checkout")["uses"] == "actions/checkout@v7"
    gate = _step(aggregate, "Require every integration group to succeed")["run"]
    assert gate == "python3 scripts/validate_supervisor_aggregate.py"
    for group in GROUPS:
        job = workflow["jobs"][group]
        assert "needs" not in job
        assert job["runs-on"] == "ubuntu-latest"
        assert job.get("continue-on-error", False) is False


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", None])
def test_actual_aggregate_decision_rejects_every_incomplete_result(result: str | None) -> None:
    values = {name: {"result": "success"} for name in GROUPS}
    if result is None:
        values.pop("fresh-host")
    else:
        values["fresh-host"]["result"] = result
    environment = {**os.environ, "REQUIRED_RESULTS": json.dumps(values)}
    result_process = subprocess.run(
        [sys.executable, str(ROOT / "scripts/validate_supervisor_aggregate.py")],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result_process.returncode != 0


def test_actual_aggregate_decision_accepts_all_successful_groups() -> None:
    values = {name: {"result": "success"} for name in GROUPS}
    environment = {**os.environ, "REQUIRED_RESULTS": json.dumps(values)}
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/validate_supervisor_aggregate.py")],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_each_group_pins_tools_checkout_and_uploads_unique_diagnostics() -> None:
    workflow = _workflow()
    artifact_names = []
    for group in GROUPS:
        job = workflow["jobs"][group]
        steps = job["steps"]
        assert _step(job, "Setup Python")["with"]["python-version"] == "3.12"
        assert _step(job, "Install uv")["with"]["version"] == "0.7.22"
        assert "uv sync --locked --dev --extra test" in _step(job, "Install locked test dependencies")["run"]
        checkout = _step(job, "Checkout")
        assert checkout["uses"] == "actions/checkout@v7"
        upload = next(step for step in steps if step.get("uses") == "actions/upload-artifact@v7")
        assert upload["if"] == "always()"
        assert upload["with"]["if-no-files-found"] == "error"
        assert "${{ github.run_id }}-${{ github.run_attempt }}" in upload["with"]["name"]
        artifact_names.append(upload["with"]["name"])
        assert job["env"]["AUTO_CODER_CI_TIMING_DIR"] == "${{ github.workspace }}/supervisor-integration-logs/timing"
        summary = _step(job, "Summarize integration timing")
        assert summary["if"] == "always()"
        assert summary["continue-on-error"] is True
        assert "scripts/summarize_ci_timing.py supervisor-integration-logs/timing" in summary["run"]
        if group != "fresh-host":
            assert job["env"]["PYTHONPATH"] == "${{ github.workspace }}/scripts"
    assert len(set(artifact_names)) == len(GROUPS)
    for group in ("fresh-host", "isolated-containers"):
        build = _step(workflow["jobs"][group], "Build the checked-out production image")["run"]
        assert "scripts/prepare_opencode_image.py" in build
        assert "auto-coder:supervisor-${{ github.sha }}" in build


def test_all_baseline_selectors_and_execution_contexts_are_preserved() -> None:
    workflow = _workflow()
    worker = _step(workflow["jobs"]["worker-bridge"], "Test real worker processes and management bridge")["run"]
    for selector in (
        "tests/test_worker_supervisor.py",
        "tests/test_worker_supervisor_bridge.py",
        "tests/test_worker_versions.py",
        "test_forced_cancel_reaps_orphan_only_after_owned_thread_finishes",
        "test_cancelled_async_wrapper_does_not_release_running_thread",
        "test_cancelled_wrapper_before_submission_does_not_retain_unstarted_work",
        "test_cancelled_wrapper_keeps_queued_executor_work_owned",
        "test_rejected_executor_submission_does_not_retain_unstarted_work",
        "test_real_unix_socket_observes_same_engine_and_read_does_not_heartbeat",
        "test_graceful_receipt_requires_stopped_closed_and_settled",
    ):
        assert selector in worker
    assert worker.count("uv run pytest") == 2
    assert "test_actual_daemon_uds_observes_admitted_drain_and_persists_receipt" in worker
    assert "-o timeout=60" in worker and "set -o pipefail" in worker

    management = _step(workflow["jobs"]["management-self-update"], "Verify authenticated management self-update")["run"]
    for path in ("test_management_self_update_integration.py", "test_management_self_update_failures.py", "test_management_clean_restart_integration.py", "test_management_fresh_host_integration.py"):
        assert path in management
    assert "-m supervisor_integration" in management

    host = _step(workflow["jobs"]["fresh-host"], "Verify fresh initialization with host evidence")["run"]
    for path in ("test_management_fresh_end_to_end.py", "test_management_retained_startup_container.py", "test_management_proxy_watcher_process_integration.py"):
        assert path in host
    assert "sudo env" in host and "PYTHONPATH=$GITHUB_WORKSPACE/src" in host
    assert "SUPERVISOR_ALLOWED_SKIPS" not in host and "strace" in host
    allowed = workflow["jobs"]["management-self-update"]["env"]["SUPERVISOR_ALLOWED_SKIPS"]
    assert "host root is required" in allowed
    assert "production-profile image is not available locally" in allowed

    containers = _step(workflow["jobs"]["isolated-containers"], "Verify isolated Supervisor containers")
    for path in ("test_supervisor_container_integration.py", "test_worker_network_integration.py", "test_management_beta_deploy_container.py", "test_beta_runtime_smoke_container.py"):
        assert path in containers["run"]
    assert "-m supervisor_integration" in containers["run"]
    assert containers["env"]["AUTO_CODER_REQUIRE_SUPERVISOR_INTEGRATION"] == "1"
    required = json.loads(workflow["jobs"]["isolated-containers"]["env"]["SUPERVISOR_REQUIRED_NODEIDS"])
    assert len(required) == 7
    assert all(nodeid.startswith("tests/") for nodeid in required)


def test_result_inventory_fails_closed_and_discovers_whole_file_cases(tmp_path: Path, _use_real_commands) -> None:
    test_file = tmp_path / "test_sample.py"
    test_file.write_text("def test_first(): pass\ndef test_added_later(): pass\n")
    report = tmp_path / "report.json"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "scripts"), "SUPERVISOR_RESULTS_PATH": str(report)}
    result = subprocess.run(["uv", "run", "pytest", "-p", "supervisor_ci_results", "-q", str(test_file)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(report.read_text())
    assert len(payload["collected"]) == 2
    assert {node.rsplit("::", 1)[-1] for node in payload["collected"]} == {"test_first", "test_added_later"}
    assert set(payload["outcomes"].values()) == {"passed"}
    assert not payload["missing"]
    assert payload["first_case_phase_epoch"] <= payload["last_case_phase_epoch"]


def test_result_inventory_rejects_mandatory_skip(tmp_path: Path, _use_real_commands) -> None:
    test_file = tmp_path / "test_skip.py"
    test_file.write_text("import pytest\n@pytest.mark.skip(reason='missing prerequisite')\ndef test_required(): pass\n")
    report = tmp_path / "report.json"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "scripts"), "SUPERVISOR_RESULTS_PATH": str(report)}
    result = subprocess.run(["uv", "run", "pytest", "-p", "supervisor_ci_results", "-q", str(test_file)], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert json.loads(report.read_text())["outcomes"].popitem()[1] == "forbidden-skip"


def test_result_inventory_rejects_deselected_baseline_and_discovers_new_marked_case(tmp_path: Path, _use_real_commands) -> None:
    test_file = tmp_path / "test_container_group.py"
    report = tmp_path / "report.json"
    required = ["test_container_group.py::test_baseline", "test_container_group.py::test_lost_marker"]
    test_file.write_text("import pytest\n" "@pytest.mark.supervisor_integration\n" "def test_baseline(): pass\n" "def test_lost_marker(): pass\n" "@pytest.mark.supervisor_integration\n" "def test_added_marked_case(): pass\n")
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "scripts"),
        "SUPERVISOR_RESULTS_PATH": str(report),
        "SUPERVISOR_REQUIRED_NODEIDS": json.dumps(required),
    }
    command = ["uv", "run", "pytest", "-p", "supervisor_ci_results", "--rootdir", str(tmp_path), "-q", "-m", "supervisor_integration", str(test_file)]
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    payload = json.loads(report.read_text())
    assert payload["missing_selection"] == ["test_container_group.py::test_lost_marker"]
    assert "test_container_group.py::test_added_marked_case" in payload["collected"]

    test_file.write_text(test_file.read_text().replace("def test_lost_marker", "@pytest.mark.supervisor_integration\ndef test_lost_marker"))
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(report.read_text())
    assert not payload["missing_selection"]
    assert len(payload["collected"]) == 3


@pytest.mark.parametrize(
    "filename,allowed_nodeid,expected_success",
    [
        ("test_management_self_update_integration.py", "tests/test_management_fresh_host_integration.py::test_real_prior_supervisor_binds_instance_and_published_port", False),
        ("test_management_fresh_host_integration.py", "test_management_fresh_host_integration.py::test_real_prior_supervisor_binds_instance_and_published_port", True),
    ],
)
def test_management_skip_permission_is_scoped_to_duplicate_host_case(tmp_path: Path, _use_real_commands, filename: str, allowed_nodeid: str, expected_success: bool) -> None:
    test_file = tmp_path / filename
    test_file.write_text("import pytest\n" "def test_real_prior_supervisor_binds_instance_and_published_port():\n" "    pytest.skip('production-profile image is not available locally')\n")
    report = tmp_path / "report.json"
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "scripts"),
        "SUPERVISOR_RESULTS_PATH": str(report),
        "SUPERVISOR_ALLOWED_SKIPS": json.dumps({allowed_nodeid: "production-profile image is not available locally"}),
    }
    result = subprocess.run(
        ["uv", "run", "pytest", "-p", "supervisor_ci_results", "--rootdir", str(tmp_path), "-q", str(test_file)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is expected_success, result.stdout + result.stderr


def test_container_fixture_has_no_host_docker_or_external_network_access() -> None:
    compose = yaml.safe_load((ROOT / "tests/fixtures/compose.supervisor-integration.yml").read_text())
    assert set(compose["services"]) == {"release", "beta"}
    volumes = []
    for service in compose["services"].values():
        assert service["network_mode"] == "none"
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert service["environment"]["AUTO_CODER_SUPERVISOR_AUTO_START"] == "1"
        assert service["entrypoint"] == ["python", "/fixtures/supervisor_container_entry.py"]
        volumes.extend(service["volumes"])
    assert all("docker.sock" not in mount for mount in volumes)
    assert all("/runtime" in mount or "/workspace" in mount or "/fixtures" in mount for mount in volumes)


def test_production_acceptance_covers_real_ready_and_offline_production_cli() -> None:
    acceptance = (ROOT / "tests/test_supervisor_container_integration.py").read_text()
    preparer = (ROOT / "tests/fixtures/supervisor_stage_ready.py").read_text()
    offline = (ROOT / "tests/fixtures/supervisor_offline_process_issues.py").read_text()
    assert "def test_real_process_issues_bootstrap_and_staged_ready_binding" in acceptance
    assert "def test_independently_prepared_staged_restart_preserves_held_intent" in acceptance
    assert "supervisor_stage_ready.py" in acceptance
    assert re.search(r'"--network",\s*"none"', acceptance)
    assert re.search(r'"--entrypoint",\s*"python"', acceptance)
    assert "supervisor_offline_pid1.py" in acceptance
    assert '"process-issues"' in offline
    assert '"--github-token"' in offline
    assert '"--skip-codex-auth-secret-sync"' in offline
    assert "os.execv(sys.executable, command)" in offline
    assert "WorkerVersionPreparer(" in preparer
    assert ".prepare(PrepareIntent(" in preparer
    assert ".read_ready(ready_path)" in preparer


def test_authenticated_beta_deployment_acceptance_is_in_live_docker_gate() -> None:
    source = (ROOT / "tests/test_management_beta_deploy_container.py").read_text()
    assert '"auto_coder_deploy_beta_source"' in source
    assert "supervisor_stage_ready.py" in source
    assert "create_management_app(" in source
    assert "WorkerNetworkClient(config)" in source
    assert '"producer:"' not in source  # Producer evidence must be read from the child.
