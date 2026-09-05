import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from auto_coder.automation_engine import AutomationEngine
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


def test_running_old_channel_cannot_dispatch_after_idle_reassignment(monkeypatch, tmp_path):
    runtime, ownership = _external_environment(monkeypatch, tmp_path, channel="beta")

    class GitHubStub:
        def get_issue_dispatch_snapshot_strict(self, repo_name, number):
            assert (repo_name, number) == ("owner/repo", 101)
            return {"number": number, "body": "", "labels": [{"name": "implementation-ready"}]}

        def get_item_type_strict(self, repo_name, number):
            assert (repo_name, number) == ("owner/repo", 101)
            return "issue"

    beta = AutomationEngine(GitHubStub(), config=AutomationConfig())
    beta.implementation_slots = ImplementationSlotRepository("owner/repo", 1)
    beta._process_single_candidate_reserved = lambda *_args: pytest.fail("revoked beta dispatched work")
    candidate = Candidate(type="issue", data={"number": 101, "title": "New work", "author_id": 1}, priority=0)

    assign_repository("owner/repo", "release", ownership, tmp_path / "runtime")

    with pytest.raises(DeploymentChannelError, match="release.*refuses to dispatch"):
        beta._process_single_candidate_unified("owner/repo", candidate, beta.config)
    assert beta.implementation_slots.active_owners() == ()

    monkeypatch.setenv("AUTO_CODER_CHANNEL", "release")
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(tmp_path / "runtime/release"))
    release = AutomationEngine(GitHubStub(), config=AutomationConfig())
    release.implementation_slots = ImplementationSlotRepository("owner/repo", 1)
    release._process_single_candidate_reserved = lambda *_args: CandidateProcessingResult(type="issue", number=101, title="New work", success=True)

    result = release._process_single_candidate_unified("owner/repo", candidate, release.config)
    assert result.success is True
    assert release.implementation_slots.storage_path != beta.implementation_slots.storage_path


def test_external_artifact_disables_internal_update(monkeypatch, tmp_path):
    _external_environment(monkeypatch, tmp_path)
    result = maybe_run_auto_update()
    assert result.attempted is False
    assert result.reason == "disabled"


def test_artifact_workflow_validation_gates_fail_closed():
    script = Path(__file__).parents[1] / "scripts/deployment_artifacts.py"

    newest = subprocess.run([sys.executable, str(script), "require-latest-successful-run", "202", "202"], capture_output=True, text=True)
    stale = subprocess.run([sys.executable, str(script), "require-latest-successful-run", "101", "202"], capture_output=True, text=True)
    tested = subprocess.run([sys.executable, str(script), "require-tested-beta", "sha256:tested", "sha256:tested"], capture_output=True, text=True)
    unrelated = subprocess.run([sys.executable, str(script), "require-tested-beta", "sha256:manual", "sha256:tested"], capture_output=True, text=True)
    valid_sha = subprocess.run([sys.executable, str(script), "require-release-sha", "a" * 40], capture_output=True, text=True)
    abbreviated_sha = subprocess.run([sys.executable, str(script), "require-release-sha", "a" * 12], capture_output=True, text=True)
    changed_history = subprocess.run([sys.executable, str(script), "require-release-history", "sha256:candidate", "sha256:existing"], capture_output=True, text=True)
    failed_postcondition = subprocess.run([sys.executable, str(script), "require-release-postcondition", "sha256:expected", "sha256:actual"], capture_output=True, text=True)

    assert newest.returncode == 0
    assert stale.returncode != 0
    assert "latest successful beta run mismatch" in stale.stderr
    assert tested.returncode == 0
    assert unrelated.returncode != 0
    assert "tested beta digest mismatch" in unrelated.stderr
    assert valid_sha.returncode == 0
    assert abbreviated_sha.returncode != 0
    assert "invalid release commit SHA" in abbreviated_sha.stderr
    assert changed_history.returncode != 0
    assert "immutable release history digest mismatch" in changed_history.stderr
    assert failed_postcondition.returncode != 0
    assert "release tag digest mismatch" in failed_postcondition.stderr


def test_workflows_route_only_latest_successful_build_through_tested_provenance():
    workflow_root = Path(__file__).parents[1] / ".github/workflows"
    publish = (workflow_root / "publish-beta.yml").read_text(encoding="utf-8")
    advance = (workflow_root / "advance-beta.yml").read_text(encoding="utf-8")
    promote = (workflow_root / "promote-release.yml").read_text(encoding="utf-8")
    rollback = (workflow_root / "rollback-release.yml").read_text(encoding="utf-8")

    assert "bash scripts/test.sh" in publish
    assert ":sha-${{ github.sha }}" in publish
    assert ":beta" not in publish
    assert "workflow_run:" in advance
    assert "status=success&per_page=1" in advance
    assert 'require-latest-successful-run "$RUN_ID" "$LATEST_SUCCESSFUL_RUN"' in advance
    assert 'tested-beta-$SOURCE_SHA" "$IMAGE@$DIGEST' in advance
    assert 'tag "$IMAGE:beta" "$IMAGE@$DIGEST' in advance
    assert "inputs:" not in promote
    assert 'DIGEST=$(docker buildx imagetools inspect "$IMAGE:beta"' in promote
    assert 'SOURCE_SHA=$(docker buildx imagetools inspect "$IMAGE:beta"' in promote
    assert "org.opencontainers.image.revision" in promote
    assert 'TESTED_DIGEST=$(docker buildx imagetools inspect "$IMAGE:tested-beta-$SOURCE_SHA"' in promote
    assert 'require-tested-beta "$DIGEST" "$TESTED_DIGEST"' in promote
    assert promote.index('require-tested-beta "$DIGEST" "$TESTED_DIGEST"') < promote.index('tag "$IMAGE:release-$SOURCE_SHA"')
    assert 'require-release-history "$DIGEST" "$HISTORY_DIGEST"' in promote
    assert promote.index('require-release-history "$DIGEST" "$HISTORY_DIGEST"') < promote.index('tag "$IMAGE:release"')
    assert 'require-release-postcondition "$DIGEST" "$RELEASE_DIGEST"' in promote
    assert promote.count("docker buildx imagetools create --prefer-index=false") == 2
    assert "docker/build-push-action" not in promote
    assert "context:" not in promote

    assert rollback.count("release_sha:") == 1
    assert "digest:" not in rollback
    assert "tested-beta-" not in rollback
    assert 'inspect "$IMAGE:release-$RELEASE_SHA"' in rollback
    assert 'create --prefer-index=false --tag "$IMAGE:release" "$IMAGE@$DIGEST"' in rollback
    assert 'tag "$IMAGE:release-$RELEASE_SHA"' not in rollback
    assert 'require-release-postcondition "$DIGEST" "$RELEASE_DIGEST"' in rollback
    assert "docker/build-push-action" not in rollback
    assert "context:" not in rollback
    assert "group: release-channel" in promote
    assert "group: release-channel" in rollback


def test_compose_channels_use_disjoint_private_storage_and_artifacts():
    import yaml

    compose = yaml.safe_load((Path(__file__).parents[1] / "compose.channels.yml").read_text(encoding="utf-8"))
    release = compose["services"]["release"]
    beta = compose["services"]["beta"]

    assert release["environment"]["AUTO_CODER_CHANNEL"] == "release"
    assert beta["environment"]["AUTO_CODER_CHANNEL"] == "beta"
    assert release["image"] == "${AUTO_CODER_IMAGE}@${RELEASE_DIGEST}"
    assert beta["image"] == "${AUTO_CODER_IMAGE}@${BETA_DIGEST}"
    assert release["environment"]["HOME"] == beta["environment"]["HOME"] == "/runtime/home"
    assert release["environment"]["AUTO_CODER_RUNTIME_ROOT"] == beta["environment"]["AUTO_CODER_RUNTIME_ROOT"] == "/runtime"
    assert release["command"][-1] == beta["command"][-1] == "/runtime/logs/auto-coder.log"

    release_private = {mount.split(":", 1)[0] for mount in release["volumes"] if not mount.endswith(":/routing")}
    beta_private = {mount.split(":", 1)[0] for mount in beta["volumes"] if not mount.endswith(":/routing")}
    assert release_private == {"./runtime/release", "./runtime/workspaces/release"}
    assert beta_private == {"./runtime/beta", "./runtime/workspaces/beta"}
    assert release_private.isdisjoint(beta_private)
