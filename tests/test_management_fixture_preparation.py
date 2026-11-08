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


@pytest.mark.parametrize("failure", ["build", "malformed_identity", "provenance_mismatch"])
def test_failed_fixture_never_exports_a_publishable_image(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(preparation, "resolve_build_identity", lambda _: SimpleNamespace(state="clean", source_revision="a" * 40, version="1.0"))
    monkeypatch.setattr(preparation, "serialize_build_identity", lambda _: '{"source_revision":"' + "a" * 40 + '"}')
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim AS python-environment\n")
    build = Mock(side_effect=subprocess.CalledProcessError(1, ["docker", "build"]) if failure == "build" else None)
    inspect = Mock(side_effect=["sha256:" + "a" * 64, json.dumps({"source_revision": {"value": "b" * 40}, "distribution_version": {"value": "1.0"}})]) if failure == "provenance_mismatch" else Mock(return_value="sha256:incomplete")
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
