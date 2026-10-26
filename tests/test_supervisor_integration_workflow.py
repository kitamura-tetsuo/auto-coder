"""Static guardrails for the dedicated real-process/container integration gate."""

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
    steps = {step["name"]: step for step in job["steps"]}
    build = steps["Build the checked-out production image"]["run"]
    assert "scripts/prepare_opencode_image.py" in build
    container = steps["Verify isolated Supervisor containers"]
    assert "-m supervisor_integration" in container["run"]
    assert "tests/test_supervisor_container_integration.py" in container["run"]
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
