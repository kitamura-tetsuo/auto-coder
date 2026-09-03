import json

import pytest
from click.testing import CliRunner

from auto_coder.cli_commands_deployment import deployment_group
from auto_coder.cli_commands_main import process_issues
from auto_coder.deployment_channel import DeploymentChannelError, assign_repository, validate_repository_ownership
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.update_manager import maybe_run_auto_update


def _external_environment(monkeypatch, tmp_path, channel="beta"):
    runtime = tmp_path / "runtime" / channel
    ownership = tmp_path / "routing" / "ownership.json"
    ownership.parent.mkdir(parents=True)
    ownership.write_text(json.dumps({"owner/repo": channel}))
    monkeypatch.setenv("AUTO_CODER_CHANNEL", channel)
    monkeypatch.setenv("AUTO_CODER_ARTIFACT_DIGEST", "sha256:abc")
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(runtime))
    monkeypatch.setenv("AUTO_CODER_OWNERSHIP_FILE", str(ownership))
    return runtime, ownership


def test_startup_identity_routes_repo_and_isolates_state(monkeypatch, tmp_path):
    runtime, _ = _external_environment(monkeypatch, tmp_path)

    identity = validate_repository_ownership("owner/repo")
    slots = ImplementationSlotRepository("owner/repo", 1)
    slots.reserve(ImplementationOwner("issue", 100))

    assert identity is not None
    assert identity.channel == "beta"
    assert json.loads((runtime / "running-artifact.json").read_text()) == {"channel": "beta", "artifact": "sha256:abc"}
    assert slots.storage_path == runtime / "state" / "owner/repo" / "implementation_slots.json"


def test_startup_fails_closed_for_other_channels_repository(monkeypatch, tmp_path):
    _external_environment(monkeypatch, tmp_path, channel="beta")
    ownership = tmp_path / "routing" / "ownership.json"
    ownership.write_text(json.dumps({"owner/repo": "release"}))

    with pytest.raises(DeploymentChannelError, match="assigned to release"):
        validate_repository_ownership("owner/repo")


def test_process_issues_production_entrypoint_rejects_ownership_overlap(monkeypatch, tmp_path):
    _external_environment(monkeypatch, tmp_path, channel="beta")
    ownership = tmp_path / "routing" / "ownership.json"
    ownership.write_text(json.dumps({"owner/repo": "release"}))

    result = CliRunner().invoke(process_issues, ["--repo", "owner/repo"])

    assert result.exit_code == 1
    assert isinstance(result.exception, DeploymentChannelError)
    assert "assigned to release" in str(result.exception)


def test_cli_reassignment_is_blocked_until_active_work_is_terminal(tmp_path):
    ownership = tmp_path / "routing" / "ownership.json"
    runtime_parent = tmp_path / "runtime"
    ownership.parent.mkdir(parents=True)
    ownership.write_text(json.dumps({"owner/repo": "beta"}))
    slots = ImplementationSlotRepository("owner/repo", 1, runtime_parent / "beta/state/owner/repo/implementation_slots.json")
    owner = ImplementationOwner("issue", 100)
    slots.reserve(owner)

    runner = CliRunner()
    blocked = runner.invoke(deployment_group, ["assign", "owner/repo", "--channel", "release", "--ownership-file", str(ownership), "--runtime-parent", str(runtime_parent)])
    assert blocked.exit_code == 1
    assert "still has active work" in blocked.output
    assert json.loads(ownership.read_text()) == {"owner/repo": "beta"}

    slots.release(owner)
    switched = runner.invoke(deployment_group, ["assign", "owner/repo", "--channel", "release", "--ownership-file", str(ownership), "--runtime-parent", str(runtime_parent)])
    assert switched.exit_code == 0
    assert json.loads(ownership.read_text()) == {"owner/repo": "release"}


def test_external_artifact_disables_internal_update(monkeypatch, tmp_path):
    _external_environment(monkeypatch, tmp_path)
    result = maybe_run_auto_update()
    assert result.attempted is False
    assert result.reason == "disabled"
