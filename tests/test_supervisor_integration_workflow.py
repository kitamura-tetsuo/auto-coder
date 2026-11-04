"""Static guardrails for the dedicated real-process/container integration gate."""

import copy
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]


def _required_fresh_host_gate(workflow: dict) -> dict:
    """Inspect the production job, prerequisites and mandatory host gate."""
    job = workflow["jobs"]["supervisor-integration"]
    assert "if" not in job
    assert job.get("continue-on-error", False) is False
    host_index = next(i for i, step in enumerate(job["steps"]) if step.get("name") == "Verify fresh initialization with host evidence")
    for step in job["steps"][: host_index + 1]:
        assert "if" not in step
        assert step.get("continue-on-error", False) is False
    host = job["steps"][host_index]
    assert host["timeout-minutes"] == 15
    return host


def test_required_fresh_host_gate_propagates_failures():
    workflow = yaml.safe_load((ROOT / ".github/workflows/supervisor-integration-tests.yml").read_text())
    _required_fresh_host_gate(workflow)


@pytest.mark.parametrize("target", ["job", "host", "build", "dependencies"])
@pytest.mark.parametrize("setting,value", [("continue-on-error", True), ("if", False)])
def test_fresh_host_gate_guard_rejects_failure_tolerance_and_skipping(target, setting, value):
    workflow = yaml.safe_load((ROOT / ".github/workflows/supervisor-integration-tests.yml").read_text())
    modified = copy.deepcopy(workflow)
    job = modified["jobs"]["supervisor-integration"]
    if target == "job":
        node = job
    else:
        name = {"host": "Verify fresh initialization with host evidence", "build": "Build the checked-out production image", "dependencies": "Install locked test dependencies"}[target]
        node = next(step for step in job["steps"] if step.get("name") == name)
    node[setting] = value
    with pytest.raises(AssertionError):
        _required_fresh_host_gate(modified)


@pytest.mark.parametrize("scenario", ["success", "tag-failure", "pull-failure", "pytest-failure", "skipped-host", "incomplete-host", "missing-junit"])
def test_production_fresh_host_command_rejects_failed_or_skipped_results(tmp_path, scenario):
    """Run the extracted production shell; substitute only external producers."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/supervisor-integration-tests.yml").read_text())
    host = _required_fresh_host_gate(workflow)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    scripts = {
        "strace": "#!/bin/sh\nexit 0\n",
        "sudo": '#!/bin/sh\nexec "$@"\n',
        "docker": '#!/bin/sh\necho "$*" >> "$GITHUB_WORKSPACE/docker-invocations"\nif [ "$1-failure" = "$HOST_SCENARIO" ]; then exit 41; fi\n',
        "python3": f'#!/bin/sh\nexec "{sys.executable}" "$@"\n',
    }
    for name, text in scripts.items():
        path = binaries / name
        path.write_text(text)
        path.chmod(0o755)
    interpreter = tmp_path / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text(
        f"#!{sys.executable}\n"
        "import os,sys\nfrom pathlib import Path\n"
        "scenario=os.environ['HOST_SCENARIO']\n"
        "Path('pytest-invocation').write_text(' '.join(sys.argv[1:]))\n"
        "if scenario=='pytest-failure':sys.exit(42)\n"
        "if scenario=='missing-junit':sys.exit(0)\n"
        "tests=18 if scenario=='incomplete-host' else 19\n"
        "skipped=1 if scenario=='skipped-host' else 0\n"
        "p=Path('supervisor-integration-logs/fresh-initialization.xml');p.parent.mkdir(exist_ok=True)\n"
        'p.write_text(f\'<testsuites><testsuite tests="{tests}" skipped="{skipped}"/></testsuites>\')\n'
    )
    interpreter.chmod(0o755)
    environment = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"], "GITHUB_WORKSPACE": str(tmp_path), "HOST_SCENARIO": scenario}
    command = host["run"].replace("${{ github.sha }}", "fixture-exact-revision")
    result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", command], cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=5)
    assert (result.returncode == 0) is (scenario == "success"), result.stdout + result.stderr
    docker = (tmp_path / "docker-invocations").read_text().splitlines()
    assert docker[0] == "tag auto-coder:supervisor-fixture-exact-revision auto-coder:management-local-abc65544"
    if scenario == "tag-failure":
        assert len(docker) == 1
    else:
        assert len(docker) == 2
    assert (tmp_path / "pytest-invocation").exists() is (scenario not in {"tag-failure", "pull-failure"})


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
    assert "tests/test_management_proxy_watcher_process_integration.py" in host
    assert "== 19" in host and "skipped" in host
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
