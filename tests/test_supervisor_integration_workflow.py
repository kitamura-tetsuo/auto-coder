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
from scripts.supervisor_ci_profiles import CONTAINER_REQUIRED, FRESH_REQUIRED, MANAGEMENT_SKIPS, OPERATOR_REQUIRED, PROFILES, profile_args

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
OTHER_REQUIRED_NODEIDS = (
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


def test_supervisor_planners_bootstrap_without_historical_native_durations() -> None:
    workflow = _workflow()
    steps = {
        "prepare-management": "Plan complete management collection",
        "prepare-fresh-host": "Plan complete privileged collection",
        "prepare-containers": "Plan complete container collection",
    }
    for planner, step_name in steps.items():
        run = _step(workflow["jobs"][planner], step_name)["run"]
        assert "--duration-profile" in run
        assert "--cold-start-estimate-seconds 120" in run


def test_operator_upgrade_keeps_its_complete_native_mandatory_gate() -> None:
    workflow = _workflow()
    job = workflow["jobs"]["operator-upgrade"]
    assert "needs" not in job
    assert job["env"]["SUPERVISOR_PROFILE"] == "operator-upgrade"
    assert tuple(json.loads(job["env"]["SUPERVISOR_REQUIRED_NODEIDS"])) == OPERATOR_REQUIRED
    assert len(OPERATOR_REQUIRED) == 8
    run = _step(job, "Verify operator prepare/apply/status with disposable Docker")["run"]
    assert "tests/test_management_bootstrap_upgrade_container.py" in run
    assert "-p supervisor_ci_results" in run
    assert '"SUPERVISOR_REQUIRED_NODEIDS=$SUPERVISOR_REQUIRED_NODEIDS"' in run
    assert '"RUNNER_NAME=$RUNNER_NAME"' in run
    assert '"SUPERVISOR_PROFILE=$SUPERVISOR_PROFILE"' in run
    assert "sudo chown -R" in run
    receipt = _step(job, "Bind native operator upgrade receipt")
    assert receipt["if"] == "always()"
    assert "--profile operator-upgrade" in receipt["run"]
    assert "--inventory supervisor-integration-logs/operator-upgrade.json" in receipt["run"]
    assert _step(workflow["jobs"]["supervisor-integration"], "Require every integration group to succeed")["env"]["REQUIRED_RESULTS"] == "${{ toJSON(needs) }}"


def test_operator_upgrade_executes_root_pytest_with_exact_profile(tmp_path: Path) -> None:
    """Execute the real workflow shell under inert commands, not just string checks."""
    run = _step(
        _workflow()["jobs"]["operator-upgrade"],
        "Verify operator prepare/apply/status with disposable Docker",
    )["run"]
    # Actions resolves this expression before running Bash.
    run = run.replace("${{ github.sha }}", "a" * 40)
    assert "${{" not in run
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    scripts = {
        "sudo": '#!/bin/sh\nif [ "$1" = "env" ]; then shift; exec env "$@"; fi\nexit 0\n',
        "docker": "#!/bin/sh\nexit 0\n",
        "strace": "#!/bin/sh\nexit 0\n",
        "git": '#!/bin/sh\nif [ "$1" = "rev-parse" ] && [ "$2" = "HEAD" ]; then echo "' + "a" * 40 + '"; else exit 9; fi\n',
    }
    for name, script in scripts.items():
        path = fake_bin / name
        path.write_text(script)
        path.chmod(0o755)

    executable = tmp_path / ".venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n" 'printf "profile=%s\\nrunner=%s\\nrequired=%s\\nresults=%s\\n" ' '"$SUPERVISOR_PROFILE" "$RUNNER_NAME" "$SUPERVISOR_REQUIRED_NODEIDS" "$SUPERVISOR_RESULTS_PATH" > "$CAPTURE"\n' 'printf "arg=%s\\n" "$@" >> "$CAPTURE"\n')
    executable.chmod(0o755)
    (tmp_path / "supervisor-integration-logs").mkdir()
    captured = tmp_path / "invocation.txt"
    required = json.dumps(list(OPERATOR_REQUIRED))
    environ = {
        **os.environ,
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "GITHUB_WORKSPACE": str(tmp_path),
        "GITHUB_SHA": "a" * 40,
        "GITHUB_WORKFLOW": "Supervisor Integration Tests",
        "GITHUB_RUN_ID": "4242",
        "GITHUB_RUN_ATTEMPT": "3",
        "GITHUB_JOB": "operator-upgrade",
        "RUNNER_NAME": "disposable-runner",
        "AUTO_CODER_CI_TIMING_DIR": str(tmp_path / "supervisor-integration-logs" / "timing"),
        "SUPERVISOR_PROFILE": "operator-upgrade",
        "SUPERVISOR_REQUIRED_NODEIDS": required,
        "CAPTURE": str(captured),
    }
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", run],
        env=environ,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    lines = captured.read_text().splitlines()
    assert lines[:4] == [
        "profile=operator-upgrade",
        "runner=disposable-runner",
        "required=" + required,
        "results=" + str(tmp_path / "supervisor-integration-logs" / "operator-upgrade.json"),
    ]
    arguments = [line.removeprefix("arg=") for line in lines[4:]]
    assert arguments == [
        "-m",
        "pytest",
        "-p",
        "supervisor_ci_results",
        "-q",
        "-vv",
        "-o",
        "timeout=240",
        "tests/test_management_bootstrap_upgrade_container.py",
        "--junitxml=supervisor-integration-logs/operator-upgrade.xml",
    ]


def test_parallel_groups_and_stable_fail_closed_aggregate() -> None:
    workflow = _workflow()
    assert workflow["name"] == "Supervisor Integration Tests"
    assert set(workflow["on"]) == {"pull_request", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    assert set(workflow["jobs"]) == GROUPS | {"supervisor-integration"}
    aggregate = workflow["jobs"]["supervisor-integration"]
    assert aggregate["name"] == "Supervisor Integration Tests"
    assert aggregate["if"] == "always()"
    assert set(aggregate["needs"]) == GROUPS
    assert _step(aggregate, "Checkout")["uses"] == "actions/checkout@v7"
    gate = _step(aggregate, "Require every integration group to succeed")["run"]
    assert gate == "uv run python scripts/validate_supervisor_aggregate.py"
    assert _step(aggregate, "Download all current-run native diagnostic artifacts")["with"]["merge-multiple"] is False
    assert _step(aggregate, "Require every integration group to succeed")["env"]["GITHUB_TOKEN"] == "${{ github.token }}"
    for group in GROUPS:
        job = workflow["jobs"][group]
        assert ("needs" not in job) is (group in PLANNERS | {"worker-bridge", "operator-upgrade"})
        assert job["runs-on"] == "ubuntu-latest"
        assert job.get("continue-on-error", False) is False
    for profile, planner in zip(PROFILES, ("prepare-management", "prepare-fresh-host", "prepare-containers")):
        job = workflow["jobs"][profile]
        assert job["needs"] == planner
        assert job["strategy"]["max-parallel"] == 4
        assert job["strategy"]["fail-fast"] is False
        assert f"fromJSON(needs.{planner}.outputs.matrix)" in job["strategy"]["matrix"]


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
        if group not in PLANNERS:
            assert upload["if"] == "always()"
        assert upload["with"]["if-no-files-found"] == "error"
        assert "${{ github.run_id }}-${{ github.run_attempt }}" in upload["with"]["name"]
        artifact_names.append(upload["with"]["name"])
        if group not in PLANNERS:
            assert job["env"]["AUTO_CODER_CI_TIMING_DIR"] == "${{ github.workspace }}/supervisor-integration-logs/timing"
            summary = _step(job, "Summarize integration timing")
            assert summary["if"] == "always()"
            assert summary["continue-on-error"] is True
            assert "scripts/summarize_ci_timing.py supervisor-integration-logs/timing" in summary["run"]
            assert any("supervisor_native_evidence.py" in step.get("run", "") for step in job["steps"])
    assert len(set(artifact_names)) == len(GROUPS)
    for group in ("prepare-fresh-host", "prepare-containers", "fresh-host", "isolated-containers", "operator-upgrade"):
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
    assert "AUTO_CODER_CI_TIMING_INVOCATION=worker-bridge" in worker
    assert "AUTO_CODER_CI_TIMING_INVOCATION=worker-daemon" in worker
    assert workflow["jobs"]["worker-bridge"]["env"]["AUTO_CODER_CI_TIMING_EXPECTED_INVOCATIONS"] == "worker-bridge,worker-daemon"
    assert "test_actual_daemon_uds_observes_admitted_drain_and_persists_receipt" in worker
    assert "-o timeout=60" in worker and "set -o pipefail" in worker

    management = profile_args("management-self-update")
    # Python module mode keeps the repository package importable by the CI pytest plugin.
    for group, step_name in (
        ("management-self-update", "Verify authenticated management self-update"),
        ("isolated-containers", "Verify isolated Supervisor containers"),
    ):
        command = _step(workflow["jobs"][group], step_name)["run"]
        assert "uv run python -m pytest -p scripts.ci_shard_plugin" in command
    for path in ("test_management_self_update_integration.py", "test_management_self_update_failures.py", "test_management_clean_restart_integration.py", "test_management_fresh_host_integration.py"):
        assert any(path in value for value in management)
    assert management[2:4] == ("-m", "supervisor_integration")

    host = _step(workflow["jobs"]["fresh-host"], "Verify fresh initialization with host evidence")["run"]
    plan = _step(workflow["jobs"]["prepare-fresh-host"], "Plan complete privileged collection")["run"]
    assert "trap 'sudo chown -R \"$(id -u):$(id -g)\" supervisor-plan/fresh-host' EXIT" in plan
    assert plan.index('sudo chown -R "$(id -u):$(id -g)" supervisor-plan/fresh-host\n') < plan.index("uv run python scripts/supervisor_ci_profiles.py matrix")
    assert "trap 'sudo chown -R \"$(id -u):$(id -g)\" supervisor-integration-logs' EXIT" in host
    for path in ("test_management_fresh_end_to_end.py", "test_management_retained_startup_container.py", "test_management_proxy_watcher_process_integration.py"):
        assert any(path in value for value in profile_args("fresh-host"))
    assert "sudo env" in host and "PYTHONPATH=$GITHUB_WORKSPACE/src" in host
    assert 'checkout_sha="$(git rev-parse HEAD)"' in host
    for name in ("AUTO_CODER_CI_TIMING_CHECKOUT_SHA", "GITHUB_SHA", "GITHUB_WORKFLOW", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB"):
        assert f'"{name}=$' in host
    assert "SUPERVISOR_ALLOWED_SKIPS" not in host
    assert "strace" in _step(workflow["jobs"]["fresh-host"], "Prepare privileged fresh-host prerequisites")["run"]
    assert len(MANAGEMENT_SKIPS) == 5
    assert sum("host root is required" in reason for reason in MANAGEMENT_SKIPS.values()) == 4
    assert sum("production-profile image is not available locally" in reason for reason in MANAGEMENT_SKIPS.values()) == 1
    assert len(FRESH_REQUIRED) == 10
    assert all(node in profile_args("fresh-host") or node.split("::")[0] in profile_args("fresh-host") for node in FRESH_REQUIRED)

    containers = _step(workflow["jobs"]["isolated-containers"], "Verify isolated Supervisor containers")
    for path in ("test_supervisor_container_integration.py", "test_worker_network_integration.py", "test_management_beta_deploy_container.py", "test_beta_runtime_smoke_container.py"):
        assert any(path in value for value in profile_args("isolated-containers"))
    assert profile_args("isolated-containers")[2:4] == ("-m", "supervisor_integration")
    assert workflow["jobs"]["isolated-containers"]["env"]["AUTO_CODER_REQUIRE_SUPERVISOR_INTEGRATION"] == "1"
    required = CONTAINER_REQUIRED
    assert len(required) == len(set(required))
    assert set(OTHER_REQUIRED_NODEIDS) | {f"tests/test_beta_runtime_smoke_container.py::{name}" for name in SMOKE_CASES} <= set(required)
    assert all(nodeid.startswith("tests/") for nodeid in required)


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
