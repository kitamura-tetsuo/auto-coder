"""Static and executable guardrails for the parallel Supervisor integration gate."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from scripts.supervisor_ci_profiles import CONTAINER_REQUIRED, FRESH_REQUIRED, PROFILES

ROOT = Path(__file__).parents[1]
PLANNERS = {"prepare-management", "prepare-fresh-host", "prepare-containers"}
GROUPS = {"worker-bridge", "operator-upgrade", *PROFILES, *PLANNERS}
SMOKE_CASES = (
    "test_local_smoke_uses_installed_worker_real_progress_and_original_journal",
    "test_local_smoke_diagnostics_do_not_advance_real_worker_progress",
    "test_local_smoke_preserves_original_evidence_across_real_faults",
    "test_local_smoke_later_selection_preserves_proof_and_publication_authority",
    "test_local_smoke_selected_retry_and_same_sha_supersession",
    "test_local_smoke_distinct_revision_and_worker_lifecycle_outcomes",
)
ORIGINAL_HISTORY_NODEIDS = (
    "tests/test_management_archived_cutover_container.py::test_real_second_cutover_preserves_all_owner_and_key_exclusions[original-lineage]",
    "tests/test_management_fresh_end_to_end.py::test_fresh_first_launch_via_host_guard_and_authenticated_http[False-False-None-True-False-False-True]",
    "tests/test_management_fresh_end_to_end.py::test_fresh_first_launch_via_host_guard_and_authenticated_http[False-True-None-True-False-False-True]",
    "tests/test_management_fresh_end_to_end.py::test_fresh_first_launch_via_host_guard_and_authenticated_http[False-False-after-claim-False-False-False-True]",
) + tuple(f"tests/test_management_fresh_end_to_end.py::test_original_host_durable_boundaries[{boundary}]" for boundary in ("anchor", "claimed", "never-created", "grant-possible"))
OTHER_REQUIRED_NODEIDS = (
    "tests/test_release_worker_acceptance_container.py::test_unready_live_release_candidate_recovers_by_docker_pid1_restart",
    "tests/test_release_worker_acceptance_container.py::test_original_reboot_intent_refuses_actual_same_pid1_reconstruction[False]",
    "tests/test_release_worker_acceptance_container.py::test_original_reboot_intent_refuses_actual_same_pid1_reconstruction[True]",
    "tests/test_supervisor_container_integration.py::test_production_entrypoint_is_private_pid1_without_docker_socket",
    "tests/test_supervisor_container_integration.py::test_release_beta_held_drain_replay_restart_and_crash_reconstruction",
    "tests/test_supervisor_container_integration.py::test_real_process_issues_bootstrap_and_staged_ready_binding",
    "tests/test_supervisor_container_integration.py::test_independently_prepared_staged_restart_preserves_held_intent",
    "tests/test_worker_network_integration.py::test_three_target_manager_restart_peer_isolation_and_lost_response",
    "tests/test_management_beta_deploy_container.py::test_authenticated_beta_source_installs_real_version_and_reconciles_after_lost_reply",
)


def _workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/supervisor-integration-tests.yml").read_text())


def _step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def test_retained_private_engine_fixture_keeps_negative_scenarios() -> None:
    """Retain fixture coverage while its retired management CI gate is removed."""
    assert not (ROOT / "tests/test_management_new_instance_first_launch_docker.py").exists()
    regression = (ROOT / "tests/test_management_new_private_engine_docker.py").read_text()
    assert "def test_kernel_pending_unix_client_in_separate_network_namespace_is_refused" in regression
    for scenario in (
        "changed-executable",
        "changed-application",
        "different-os-uid",
        "pre-gate-live-sibling",
        "preconnected-private-client",
        "pre-seal-start-stop-selected",
        "restart-policy-after-claim",
        "sealed-gate-outside-client",
        "wrong-pid1-command",
    ):
        assert f'"{scenario}"' in regression


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


def test_green_job_counts_alone_cannot_pass_without_native_receipts() -> None:
    values = {name: {"result": "success"} for name in GROUPS}
    environment = {**os.environ, "REQUIRED_RESULTS": json.dumps(values)}
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/validate_supervisor_aggregate.py")],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


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
    assert "AUTO_CODER_CI_TIMING_INVOCATION=worker-bridge" in worker
    assert "AUTO_CODER_CI_TIMING_INVOCATION=worker-daemon" in worker
    assert workflow["jobs"]["worker-bridge"]["env"]["AUTO_CODER_CI_TIMING_EXPECTED_INVOCATIONS"] == "worker-bridge,worker-daemon"
    assert "test_actual_daemon_uds_observes_admitted_drain_and_persists_receipt" in worker
    assert "-o timeout=60" in worker and "set -o pipefail" in worker


def test_all_six_smoke_families_are_collected_with_the_required_marker(_use_real_commands) -> None:
    """A removed case or marker fails before the Docker integration run begins."""
    command = [
        "uv",
        "run",
        "pytest",
        "--collect-only",
        "-q",
        "-m",
        "supervisor_integration",
        "tests/test_beta_runtime_smoke_container.py",
    ]
    collected = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=90)
    assert collected.returncode == 0, collected.stdout + collected.stderr
    nodeids = set(collected.stdout.splitlines())
    required = CONTAINER_REQUIRED
    for name in SMOKE_CASES:
        nodeid = f"tests/test_beta_runtime_smoke_container.py::{name}"
        assert nodeid in nodeids
        assert nodeid in required


def test_original_history_acceptance_is_collected_and_required(_use_real_commands) -> None:
    """Real source node IDs cannot silently disappear from the mandatory plan."""
    command = [
        "uv",
        "run",
        "pytest",
        "--collect-only",
        "-q",
        "tests/test_management_fresh_end_to_end.py",
        "tests/test_management_archived_cutover_container.py",
    ]
    collected = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=90)
    assert collected.returncode == 0, collected.stdout + collected.stderr
    nodeids = collected.stdout.splitlines()
    for nodeid in ORIGINAL_HISTORY_NODEIDS:
        assert nodeids.count(nodeid) == 1
        assert nodeid in FRESH_REQUIRED


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
