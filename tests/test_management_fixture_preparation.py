"""Publication prerequisites fail closed before selecting complete source tests."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from scripts import prepare_management_test_image as preparation


@pytest.mark.parametrize("name,selection", [("publish-beta.yml", 'bash scripts/test.sh -m "not opencode_live"'), ("test-source-for-runtime.yml", "uv run python -m pytest -p scripts.ci_shard_plugin -p scripts.source_ci_receipt_plugin")])
def test_complete_workflow_prepares_python_fixture_before_unchanged_tests(name, selection):
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows" / name).read_text())
    selected = [job for job in workflow["jobs"].values() if any(str(step.get("run", "")).startswith(selection) for step in job["steps"])]
    assert len(selected) == 1
    commands = [step.get("run") for step in selected[0]["steps"]]
    test_command = next(command for command in commands if str(command).startswith(selection))
    assert commands.index("uv run python scripts/prepare_management_test_image.py") < commands.index(test_command)
    if name == "test-source-for-runtime.yml":
        assert "-m 'not opencode_live'" in test_command


def test_dirty_fixture_does_not_build_or_publish_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(preparation, "resolve_build_identity", lambda _: SimpleNamespace(state="dirty", source_revision="a" * 40))
    build = Mock()
    monkeypatch.setattr(preparation.subprocess, "run", build)
    output = tmp_path / "github-env"
    with pytest.raises(RuntimeError, match="clean source identity"):
        preparation.prepare_image(tmp_path, output)
    build.assert_not_called()
    assert not output.exists()


def test_source_fixture_exports_buildable_tag_only_after_immutable_verification(tmp_path, monkeypatch):
    identity = SimpleNamespace(state="clean", source_revision="a" * 40, version="1.0")
    image_id = "sha256:" + "b" * 64
    monkeypatch.setattr(preparation, "resolve_build_identity", lambda _: identity)
    monkeypatch.setattr(preparation, "serialize_build_identity", lambda _: '{"source_revision":"' + identity.source_revision + '"}')
    monkeypatch.setattr(preparation.subprocess, "run", Mock())
    monkeypatch.setattr(preparation.subprocess, "check_output", Mock(return_value=image_id))
    verify = Mock(return_value=image_id)
    monkeypatch.setattr(preparation, "verify_image", verify)
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim AS python-environment\n")
    github_env = tmp_path / "github-env"

    assert preparation.prepare_image(tmp_path, github_env) == image_id
    verify.assert_called_once_with(tmp_path, image_id)
    assert github_env.read_text() == "AUTOCODER_FRESH_TEST_BASE_IMAGE=auto-coder:management-fixture-aaaaaaaaaaaa\n"


@pytest.mark.parametrize("failure", ["build", "malformed_identity", "provenance_mismatch"])
def test_failed_fixture_never_exports_a_publishable_image(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(preparation, "resolve_build_identity", lambda _: SimpleNamespace(state="clean", source_revision="a" * 40, version="1.0"))
    monkeypatch.setattr(preparation, "serialize_build_identity", lambda _: '{"source_revision":"' + "a" * 40 + '"}')
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim AS python-environment\n")
    build = Mock(side_effect=subprocess.CalledProcessError(1, ["docker", "build"]) if failure == "build" else None)
    inspect = Mock(side_effect=["sha256:" + "a" * 64, "sha256:" + "a" * 64, json.dumps({"source_revision": {"value": "b" * 40}, "distribution_version": {"value": "1.0"}})]) if failure == "provenance_mismatch" else Mock(return_value="sha256:incomplete")
    monkeypatch.setattr(preparation.subprocess, "run", build)
    monkeypatch.setattr(preparation.subprocess, "check_output", inspect)
    output = tmp_path / "github-env"
    with pytest.raises((subprocess.CalledProcessError, RuntimeError)):
        preparation.prepare_image(tmp_path, output)
    assert not output.exists()
    arguments = build.call_args.args[0]
    assert arguments[arguments.index("--target") + 1] == "management-test-fixture"
    if failure == "build":
        inspect.assert_not_called()

    dockerfile = build.call_args.kwargs["input"]
    assert "FROM python-environment AS management-test-fixture" in dockerfile
    assert "build_provenance embed" in dockerfile
    assert build.call_args.kwargs["text"] is True


def test_source_fixture_precedes_checkout_local_chromium_download():
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/test-source-for-runtime.yml").read_text())
    commands = [step.get("run") for step in workflow["jobs"]["complete-source-shard"]["steps"]]
    assert commands.index("uv run python scripts/prepare_management_test_image.py") < commands.index("uv run playwright install --with-deps chromium")


@pytest.mark.parametrize("failure", ["missing", "wrong_identity"])
def test_consumer_rechecks_immutable_local_image_before_running_container(tmp_path, monkeypatch, failure):
    expected = "sha256:" + "a" * 64
    monkeypatch.setattr(preparation, "resolve_build_identity", lambda _: SimpleNamespace(state="clean", source_revision="a" * 40, version="1.0"))
    inspect = Mock(
        side_effect=subprocess.CalledProcessError(1, ["docker", "image", "inspect"]) if failure == "missing" else None,
        return_value="sha256:" + "b" * 64,
    )
    monkeypatch.setattr(preparation.subprocess, "check_output", inspect)
    with pytest.raises(subprocess.CalledProcessError if failure == "missing" else RuntimeError):
        preparation.verify_image(tmp_path, expected)
    assert inspect.call_count == 1
    assert inspect.call_args.args[0] == ["docker", "image", "inspect", "--format", "{{.Id}}", expected]


def test_consumer_rejects_nonimmutable_reference_before_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(preparation, "resolve_build_identity", lambda _: SimpleNamespace(state="clean", source_revision="a" * 40, version="1.0"))
    inspect = Mock()
    monkeypatch.setattr(preparation.subprocess, "check_output", inspect)
    with pytest.raises(RuntimeError, match="malformed"):
        preparation.verify_image(tmp_path, "auto-coder:management-local-abc65544")
    inspect.assert_not_called()


def test_real_retained_startup_cannot_fall_back_to_registry_pull():
    source = (Path(__file__).parents[1] / "tests/test_management_retained_startup_container.py").read_text()
    assert '"--pull",' in source
    assert '"never",' in source
    assert "verify_image(" in source


def test_default_tag_consumer_resolves_verified_immutable_id(monkeypatch, tmp_path):
    """The no-environment entry path must not consume a mutable tag."""
    from tests import test_management_retained_startup_container as retained

    monkeypatch.delenv("AUTOCODER_FRESH_TEST_BASE_IMAGE", raising=False)
    image_id = "sha256:" + "a" * 64
    inspect = Mock(return_value=image_id + "\n")
    verify = Mock(return_value=image_id)
    exercise = Mock()
    monkeypatch.setattr(retained.subprocess, "check_output", inspect)
    monkeypatch.setattr(retained, "verify_image", verify)
    monkeypatch.setattr(retained, "_exercise_real_retained_startup", exercise)

    retained.test_retained_startup_uses_real_readonly_bind_and_shared_owner_lock(tmp_path, None)

    inspect.assert_called_once_with(
        ["docker", "image", "inspect", "--format", "{{.Id}}", "auto-coder:management-local-abc65544"],
        text=True,
    )
    assert verify.call_args.args[1] == image_id
    assert exercise.call_args.kwargs["image"] == image_id


def test_default_tag_consumer_rejects_wrong_source_before_assertion_docker(monkeypatch, tmp_path):
    """Even a present historical tag must establish checkout provenance."""
    from tests import test_management_retained_startup_container as retained

    monkeypatch.delenv("AUTOCODER_FRESH_TEST_BASE_IMAGE", raising=False)
    image_id = "sha256:" + "b" * 64
    exercise = Mock()
    monkeypatch.setattr(retained.subprocess, "check_output", Mock(return_value=image_id))
    monkeypatch.setattr(retained, "verify_image", Mock(side_effect=RuntimeError("installed provenance differs")))
    monkeypatch.setattr(retained, "_exercise_real_retained_startup", exercise)

    with pytest.raises(RuntimeError, match="installed provenance differs"):
        retained.test_retained_startup_uses_real_readonly_bind_and_shared_owner_lock(tmp_path, None)
    exercise.assert_not_called()


def test_fresh_host_workflow_passes_verified_image_across_sudo():
    """Each executing shard independently binds the installed image to its source."""
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/supervisor-integration-tests.yml").read_text())
    steps = workflow["jobs"]["fresh-host"]["steps"]
    runs = [str(step.get("run", "")) for step in steps]
    handoff = next(i for i, step in enumerate(steps) if step.get("name") == "Verify and export retained-startup image identity")
    execution = next(i for i, step in enumerate(steps) if step.get("name") == "Verify fresh initialization with host evidence")
    assert handoff < execution
    assert "AUTOCODER_OPENCODE_IMAGE_ID" in runs[handoff]
    assert "verify_image(" in runs[handoff]
    assert "AUTOCODER_FRESH_TEST_BASE_IMAGE=$AUTOCODER_FRESH_TEST_BASE_IMAGE" in runs[execution]
    assert "tests/test_management_retained_startup_container.py" in (Path(__file__).parents[1] / "scripts/supervisor_ci_profiles.py").read_text()


def test_dirty_consumer_reports_actual_checkout_changes_and_never_runs_docker(tmp_path, monkeypatch):
    """A CI-dirtied checkout must identify the offender without running an image."""
    monkeypatch.setattr(
        preparation,
        "resolve_build_identity",
        lambda _: SimpleNamespace(state="dirty", source_revision="a" * 40, reason=None),
    )
    status = Mock(return_value=SimpleNamespace(stdout=" M src/auto_coder/example.py\n?? accidental-output/\n"))
    inspect = Mock()
    monkeypatch.setattr(preparation.subprocess, "run", status)
    monkeypatch.setattr(preparation.subprocess, "check_output", inspect)
    with pytest.raises(RuntimeError, match="git_status") as raised:
        preparation.verify_image(tmp_path, "sha256:" + "a" * 64)
    assert "src/auto_coder/example.py" in str(raised.value)
    assert "accidental-output/" in str(raised.value)
    status.assert_called_once_with(
        ["git", "-C", str(tmp_path), "status", "--porcelain=v1", "--untracked-files=normal"],
        capture_output=True,
        text=True,
        check=True,
    )
    inspect.assert_not_called()


def test_consumer_identity_ci_artifacts_live_outside_checkout():
    """Test-time caches and downloaded shard plans may not dirty source identity."""
    root = Path(__file__).parents[1]
    source = yaml.safe_load((root / ".github/workflows/test-source-for-runtime.yml").read_text())
    shard = source["jobs"]["complete-source-shard"]
    assert shard["env"]["PLAYWRIGHT_BROWSERS_PATH"] == "/tmp/auto-coder-playwright-${{ github.run_id }}-${{ github.run_attempt }}-${{ matrix.shard }}"
    assert shard["env"]["PLAYWRIGHT_BROWSERS_PATH"] != "${{ github.workspace }}/.playwright-browsers"

    supervisor = yaml.safe_load((root / ".github/workflows/supervisor-integration-tests.yml").read_text())
    fresh_host = supervisor["jobs"]["fresh-host"]
    assert fresh_host["env"]["SUPERVISOR_PLAN_PATH"] == "/tmp/auto-coder-supervisor-plan-${{ github.run_id }}-${{ github.run_attempt }}-${{ matrix.shard }}/plan.json"
    download = next(step for step in fresh_host["steps"] if step.get("name") == "Download immutable fresh-host plan")
    assert download["with"]["path"] == "/tmp/auto-coder-supervisor-plan-${{ github.run_id }}-${{ github.run_attempt }}-${{ matrix.shard }}"
    assert "tests/test_management_retained_startup_container.py" in (root / "scripts/supervisor_ci_profiles.py").read_text()
