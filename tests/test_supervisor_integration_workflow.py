"""Static guardrails for the dedicated real-process/container integration gate."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_supervisor_integration_job_runs_independently_on_pull_requests() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/supervisor-integration-tests.yml").read_text())
    assert workflow["name"] == "Supervisor Integration Tests"
    assert "pull_request" in workflow["on"]
    assert "workflow_dispatch" in workflow["on"]
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["supervisor-integration"]
    assert job["name"] == "Supervisor Integration Tests"
    assert job["runs-on"] == "ubuntu-latest"
    assert job["env"]["UV_LOCKED"] == "1"
    steps = {step["name"]: step for step in job["steps"]}
    assert steps["Install uv"]["with"]["version"] == "0.7.22"
    assert "uv sync --locked --dev --extra test" in steps["Install locked test dependencies"]["run"]
    management = steps["Verify authenticated management self-update"]
    assert management["timeout-minutes"] == 55
    assert job["timeout-minutes"] >= management["timeout-minutes"]
    assert "-m supervisor_integration" in management["run"]
    assert "tests/test_management_self_update_integration.py" in management["run"]
    assert "tests/test_management_self_update_failures.py" in management["run"]
    assert "--junitxml=supervisor-integration-logs/management-self-update.xml" in management["run"]
    build = steps["Build the checked-out production image"]["run"]
    assert "scripts/prepare_opencode_image.py" in build
    host = steps["Verify fresh initialization with host evidence"]["run"]
    assert "tests/test_management_retained_startup_container.py" in host
    assert "== 17" in host and "skipped" in host
    assert "-o timeout=60" in steps["Test real worker processes and management bridge"]["run"]
    assert "test_forced_cancel_reaps_orphan_only_after_owned_thread_finishes" in steps["Test real worker processes and management bridge"]["run"]
    assert "test_cancelled_async_wrapper_does_not_release_running_thread" in steps["Test real worker processes and management bridge"]["run"]
    assert "set -o pipefail" in steps["Test real worker processes and management bridge"]["run"]
    assert steps["Test real worker processes and management bridge"]["run"].count("tail -n 400") == 2
    container = steps["Verify isolated Supervisor containers"]
    assert "-m supervisor_integration" in container["run"]
    assert "tests/test_supervisor_container_integration.py" in container["run"]
    assert "tests/test_management_beta_deploy_container.py" in container["run"]
    assert container["timeout-minutes"] >= 40
    assert container["env"]["AUTO_CODER_REQUIRE_SUPERVISOR_INTEGRATION"] == "1"
    assert container["env"]["AUTOCODER_SUPERVISOR_IMAGE"]


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
