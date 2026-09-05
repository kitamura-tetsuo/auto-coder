import json
import os
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

RELEASE_SHA = "a" * 40
EXPECTED_DIGEST = "sha256:" + "1" * 64
OTHER_DIGEST = "sha256:" + "2" * 64


def _workflow_run_block(workflow_name, step_name):
    import yaml

    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows" / workflow_name).read_text(encoding="utf-8"))
    steps = next(iter(workflow["jobs"].values()))["steps"]
    run_block = next(step["run"] for step in steps if step.get("name") == step_name)
    return run_block.replace("${{ github.repository }}", "owner/repo")


def _run_release_workflow(tmp_path, workflow_name, step_name, registry_responses, release_sha=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

arguments = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(arguments) + "\\n")
if arguments[:3] != ["buildx", "imagetools", "inspect"]:
    if arguments[:3] == ["buildx", "imagetools", "create"]:
        state_path = Path(os.environ["FAKE_DOCKER_STATE"])
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        tag = arguments[arguments.index("--tag") + 1]
        state[tag] = arguments[-1].rsplit("@", 1)[-1]
        state_path.write_text(json.dumps(state))
        raise SystemExit(0)
    raise SystemExit(2)
reference = arguments[3]
output_kind = "image" if any(".Image" in argument for argument in arguments) else "digest"
response = json.loads(os.environ["FAKE_REGISTRY_RESPONSES"]).get(f"{reference}|{output_kind}")
state_path = Path(os.environ["FAKE_DOCKER_STATE"])
state = json.loads(state_path.read_text()) if state_path.exists() else {}
if output_kind == "digest" and reference in state and (response is None or isinstance(response, dict)):
    response = json.dumps(state[reference])
if response is None:
    raise SystemExit(1)
if isinstance(response, dict):
    if response.get("stderr"):
        print(response["stderr"], file=sys.stderr)
    if response.get("stdout"):
        print(response["stdout"])
    raise SystemExit(response.get("status", 1))
print(response)
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    fake_gh = bin_dir / "gh"
    fake_gh.write_text("#!/bin/sh\nprintf '%s\\n' \"$RUN_ID\"\n", encoding="utf-8")
    fake_gh.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "FAKE_DOCKER_LOG": str(docker_log),
            "FAKE_DOCKER_STATE": str(tmp_path / "docker-state.json"),
            "FAKE_REGISTRY_RESPONSES": json.dumps(registry_responses),
            "IMAGE": "ghcr.io/owner/repo",
            "RUN_ID": "42",
            "SOURCE_SHA": RELEASE_SHA,
        }
    )
    if release_sha is not None:
        env["RELEASE_SHA"] = release_sha
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-c", _workflow_run_block(workflow_name, step_name)],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
    )
    operations = [json.loads(line) for line in docker_log.read_text(encoding="utf-8").splitlines()] if docker_log.exists() else []
    return result, operations


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
    assert 'DIGEST=$(python scripts/deployment_artifacts.py inspect-digest "$IMAGE:beta")' in promote
    assert 'SOURCE_SHA=$(docker buildx imagetools inspect "$IMAGE:beta"' in promote
    assert "org.opencontainers.image.revision" in promote
    assert 'TESTED_DIGEST=$(python scripts/deployment_artifacts.py inspect-digest "$IMAGE:tested-beta-$SOURCE_SHA")' in promote
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
    assert 'inspect-digest "$IMAGE:release-$RELEASE_SHA"' in rollback
    assert 'create --prefer-index=false --tag "$IMAGE:release" "$IMAGE@$DIGEST"' in rollback
    assert 'tag "$IMAGE:release-$RELEASE_SHA"' not in rollback
    assert 'require-release-postcondition "$DIGEST" "$RELEASE_DIGEST"' in rollback
    assert "docker/build-push-action" not in rollback
    assert "context:" not in rollback
    assert "group: release-channel" in promote
    assert "group: release-channel" in rollback
    for workflow in (advance, promote, rollback):
        assert "set -Eeuo pipefail" in workflow


def test_promotion_stops_before_writes_when_tested_beta_digest_differs(tmp_path):
    image = "ghcr.io/owner/repo"
    responses = {
        f"{image}:beta|digest": json.dumps(EXPECTED_DIGEST),
        f"{image}:beta|image": json.dumps({"config": {"Labels": {"org.opencontainers.image.revision": RELEASE_SHA}}}),
        f"{image}:tested-beta-{RELEASE_SHA}|digest": json.dumps(OTHER_DIGEST),
    }

    result, operations = _run_release_workflow(
        tmp_path,
        "promote-release.yml",
        "Validate and promote current beta without rebuilding",
        responses,
    )

    assert result.returncode != 0
    assert "tested beta digest mismatch" in result.stderr
    assert not any(operation[:3] == ["buildx", "imagetools", "create"] for operation in operations)


@pytest.mark.parametrize("history", ["missing", "matching"])
def test_promotion_creates_only_confirmed_missing_history_then_updates_release(tmp_path, history):
    image = "ghcr.io/owner/repo"
    responses = {
        f"{image}:beta|digest": json.dumps(EXPECTED_DIGEST),
        f"{image}:beta|image": json.dumps({"config": {"Labels": {"org.opencontainers.image.revision": RELEASE_SHA}}}),
        f"{image}:tested-beta-{RELEASE_SHA}|digest": json.dumps(EXPECTED_DIGEST),
    }
    if history == "missing":
        responses[f"{image}:release-{RELEASE_SHA}|digest"] = {
            "status": 1,
            "stderr": f"ERROR: {image}:release-{RELEASE_SHA}: not found",
        }
    else:
        responses[f"{image}:release-{RELEASE_SHA}|digest"] = json.dumps(EXPECTED_DIGEST)

    result, operations = _run_release_workflow(tmp_path, "promote-release.yml", "Validate and promote current beta without rebuilding", responses)

    writes = [operation for operation in operations if operation[:3] == ["buildx", "imagetools", "create"]]
    assert result.returncode == 0, result.stderr
    assert [operation[operation.index("--tag") + 1] for operation in writes] == ([f"{image}:release-{RELEASE_SHA}", f"{image}:release"] if history == "missing" else [f"{image}:release"])


@pytest.mark.parametrize(
    ("history_response", "error_text"),
    [
        (json.dumps(OTHER_DIGEST), "immutable release history digest mismatch"),
        ({"status": 1, "stderr": "ERROR: unauthorized: authentication required"}, "unable to inspect"),
        ({"status": 0, "stdout": '""'}, "invalid digest output"),
    ],
)
def test_promotion_blocks_all_writes_for_conflicting_or_unavailable_history(tmp_path, history_response, error_text):
    image = "ghcr.io/owner/repo"
    responses = {
        f"{image}:beta|digest": json.dumps(EXPECTED_DIGEST),
        f"{image}:beta|image": json.dumps({"config": {"Labels": {"org.opencontainers.image.revision": RELEASE_SHA}}}),
        f"{image}:tested-beta-{RELEASE_SHA}|digest": json.dumps(EXPECTED_DIGEST),
        f"{image}:release-{RELEASE_SHA}|digest": history_response,
    }

    result, operations = _run_release_workflow(tmp_path, "promote-release.yml", "Validate and promote current beta without rebuilding", responses)

    assert result.returncode != 0
    assert error_text in result.stderr
    assert not any(operation[:3] == ["buildx", "imagetools", "create"] for operation in operations)


def test_advance_beta_inspection_failure_precedes_all_registry_writes(tmp_path):
    image = "ghcr.io/owner/repo"
    responses = {f"{image}:sha-{RELEASE_SHA}|digest": {"status": 1, "stderr": "ERROR: registry transport unavailable"}}

    result, operations = _run_release_workflow(tmp_path, "advance-beta.yml", "Reject an out-of-order older successful run", responses)

    assert result.returncode != 0
    assert "unable to inspect" in result.stderr
    assert not any(operation[:3] == ["buildx", "imagetools", "create"] for operation in operations)


def test_rollback_stops_before_write_when_release_history_is_missing(tmp_path):
    image = "ghcr.io/owner/repo"
    responses = {f"{image}:tested-beta-{RELEASE_SHA}|digest": json.dumps(EXPECTED_DIGEST)}

    result, operations = _run_release_workflow(
        tmp_path,
        "rollback-release.yml",
        "Roll back to immutable release history without rebuilding",
        responses,
        release_sha=RELEASE_SHA,
    )

    assert result.returncode != 0
    assert ["buildx", "imagetools", "inspect", f"{image}:release-{RELEASE_SHA}"] == operations[0][:4]
    assert not any(f"tested-beta-{RELEASE_SHA}" in argument for operation in operations for argument in operation)
    assert not any(operation[:3] == ["buildx", "imagetools", "create"] for operation in operations)


@pytest.mark.parametrize(
    ("workflow_name", "step_name", "release_sha", "responses"),
    [
        (
            "promote-release.yml",
            "Validate and promote current beta without rebuilding",
            None,
            {
                "ghcr.io/owner/repo:beta|digest": json.dumps(EXPECTED_DIGEST),
                "ghcr.io/owner/repo:beta|image": json.dumps({"config": {"Labels": {"org.opencontainers.image.revision": RELEASE_SHA}}}),
                f"ghcr.io/owner/repo:tested-beta-{RELEASE_SHA}|digest": json.dumps(EXPECTED_DIGEST),
                f"ghcr.io/owner/repo:release-{RELEASE_SHA}|digest": json.dumps(EXPECTED_DIGEST),
                "ghcr.io/owner/repo:release|digest": json.dumps(OTHER_DIGEST),
            },
        ),
        (
            "rollback-release.yml",
            "Roll back to immutable release history without rebuilding",
            RELEASE_SHA,
            {
                f"ghcr.io/owner/repo:release-{RELEASE_SHA}|digest": json.dumps(EXPECTED_DIGEST),
                "ghcr.io/owner/repo:release|digest": json.dumps(OTHER_DIGEST),
            },
        ),
    ],
)
def test_release_workflow_reports_failure_when_post_write_digest_differs(tmp_path, workflow_name, step_name, release_sha, responses):
    result, operations = _run_release_workflow(tmp_path, workflow_name, step_name, responses, release_sha=release_sha)

    assert any(operation[:3] == ["buildx", "imagetools", "create"] for operation in operations)
    assert result.returncode != 0
    assert "release tag digest mismatch" in result.stderr


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
