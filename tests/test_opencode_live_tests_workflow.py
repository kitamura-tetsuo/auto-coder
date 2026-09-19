"""Regression coverage for the dedicated `OpenCode Live Tests` GitHub Actions workflow (Issue #2152).

Verifies the workflow YAML contracts, marker classification boundaries across
ordinary shards and dedicated live CI, fail-closed enforcement, image provenance
resolution, and isolation of ordinary collection from live infrastructure.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from scripts.prepare_opencode_image import prepare_image

ROOT = Path(__file__).resolve().parents[1]
OPENCODE_LIVE_WORKFLOW = ROOT / ".github/workflows/opencode-live-tests.yml"
PR_WORKFLOW = ROOT / ".github/workflows/pr-tests.yml"
PREPARE_SCRIPT = ROOT / "scripts/prepare_opencode_image.py"

MIGRATED_CONTAINER_SCENARIOS = {
    "test_ac001_container_executes_opencode_task_against_controlled_provider",
    "test_ac002_effective_home_and_runtime_authentication",
    "test_ac003_retained_and_isolated_native_state_between_channels",
    "test_ac004_no_baked_credentials_or_unsolicited_provider_calls",
    "test_ac005_documentation_matches_production_compose_and_route",
}

STATIC_CONTAINER_RUNTIME_TESTS = {
    "test_dockerfile_pins_opencode_release_and_explicit_architectures",
    "test_compose_channels_runtime_mounts_and_isolation",
    "test_effective_home_and_xdg_storage_resolution",
    "test_documentation_describes_opencode_container_runtime",
}


def _workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_opencode_live_workflow_contract_and_limits():
    """AS-006, REQ-003, REQ-006, REQ-008: Dedicated workflow contract, triggers, and budgets."""
    workflow = _workflow(OPENCODE_LIVE_WORKFLOW)
    assert workflow["name"] == "OpenCode Live Tests"
    assert set(workflow["on"]) == {"pull_request", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}

    job = workflow["jobs"]["opencode-live-tests"]
    assert job["name"] == "OpenCode Live Tests"
    assert job["timeout-minutes"] == 40, "Containing live job must have a 40-minute limit"
    assert "needs" not in job, "OpenCode Live Tests must run independently of other workflows"

    steps = job["steps"]
    step_names = [s["name"] for s in steps]

    prep_index = step_names.index("Prepare OpenCode runtime image")
    test_index = step_names.index("Run opencode_live-marked tests")
    artifact_index = step_names.index("Upload OpenCode live test diagnostics")

    assert prep_index < test_index < artifact_index, "Image must be prepared before live tests execute"

    prep_step = steps[prep_index]
    assert prep_step["timeout-minutes"] == 10, "Cold image preparation must have a 10-minute step limit"
    assert "scripts/prepare_opencode_image.py" in prep_step["run"]

    test_step = steps[test_index]
    assert test_step["timeout-minutes"] == 20, "Live pytest execution must have a 20-minute step limit"
    assert test_step["env"]["AUTO_CODER_REQUIRE_OPENCODE_LIVE"] == "1"
    assert "-m opencode_live" in test_step["run"]
    for excluder in ("--splits", "--group", " -k ", "test_opencode_noedit_live.py", "test_opencode_container_runtime.py"):
        assert excluder not in test_step["run"], f"Selection must be purely marker-based, not {excluder!r}"

    artifact_step = steps[artifact_index]
    assert artifact_step["if"] == "always()", "Diagnostics upload must be attempted after success or failure"
    assert "opencode-live-logs/" in artifact_step["with"]["path"]


def test_ordinary_pr_tests_workflow_budgets_preserved():
    """REQ-007, AS-006: Ordinary PR Tests configuration and supervisor budgets remain intact."""
    pr_workflow = _workflow(PR_WORKFLOW)
    assert pr_workflow["name"] == "PR Tests"
    shard = pr_workflow["jobs"]["tests-shard"]
    assert shard["strategy"] == {"fail-fast": False, "matrix": {"group": [1, 2, 3, 4]}}

    test_step = next(s for s in shard["steps"] if s["name"].startswith("Run tests with coverage"))
    assert test_step["timeout-minutes"] == 12
    assert "--attempt-timeout 360" in test_step["run"]
    assert "--termination-grace 10" in test_step["run"]
    assert "--max-attempts 2" in test_step["run"]
    assert "scripts/run_pr_test_shard.py" in test_step["run"]


def test_selection_conserves_coverage_across_boundary():
    """AS-001, REQ-001, REQ-002, REQ-003: Collect ordinary vs live selections on checked-out suite."""
    # 1. Collect ordinary PR test selection (not browser and not opencode_live)
    ord_collect = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_opencode_container_runtime.py", "-m", "not browser and not opencode_live", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert ord_collect.returncode == 0, ord_collect.stdout + ord_collect.stderr
    ord_ids = {line.strip() for line in ord_collect.stdout.splitlines() if "::" in line}
    ord_names = {node_id.split("::")[-1] for node_id in ord_ids}

    # Static tests must be in ordinary selection
    assert STATIC_CONTAINER_RUNTIME_TESTS <= ord_names, f"Missing static tests from ordinary selection: {STATIC_CONTAINER_RUNTIME_TESTS - ord_names}"
    # Container scenarios must NOT be in ordinary selection
    assert not (MIGRATED_CONTAINER_SCENARIOS & ord_names), f"Container scenarios leaked into ordinary selection: {MIGRATED_CONTAINER_SCENARIOS & ord_names}"

    # 2. Collect dedicated live selection (opencode_live)
    live_collect = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_opencode_container_runtime.py", "-m", "opencode_live", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert live_collect.returncode == 0, live_collect.stdout + live_collect.stderr
    live_ids = {line.strip() for line in live_collect.stdout.splitlines() if "::" in line}
    live_names = {node_id.split("::")[-1] for node_id in live_ids}

    # Container scenarios must be in live selection
    assert MIGRATED_CONTAINER_SCENARIOS <= live_names, f"Missing container scenarios from live selection: {MIGRATED_CONTAINER_SCENARIOS - live_names}"
    # Static tests must NOT be in live selection
    assert not (STATIC_CONTAINER_RUNTIME_TESTS & live_names), f"Static tests leaked into live selection: {STATIC_CONTAINER_RUNTIME_TESTS & live_names}"

    # 3. Union covers all 9 tests in test_opencode_container_runtime.py
    assert (ord_names | live_names) == (STATIC_CONTAINER_RUNTIME_TESTS | MIGRATED_CONTAINER_SCENARIOS)

    # 4. Previously live host-CLI tests remain in live selection
    host_collect = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_opencode_noedit_live.py", "-m", "opencode_live", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert host_collect.returncode == 0, host_collect.stdout + host_collect.stderr
    host_ids = {line.strip() for line in host_collect.stdout.splitlines() if "::" in line}
    assert len(host_ids) > 0, "Expected host-CLI tests in test_opencode_noedit_live.py to be collected under opencode_live"


def test_dynamic_opencode_live_test_joins_selection_automatically(tmp_path):
    """AS-001: A newly introduced opencode_live test in an unrelated file joins dedicated live selection."""
    custom_test = tmp_path / "test_unrelated_custom_probe.py"
    custom_test.write_text(
        """
import pytest

@pytest.mark.opencode_live
def test_newly_marked_live_probe():
    assert True

def test_ordinary_control_probe():
    assert False
""",
        encoding="utf-8",
    )

    # Collect with live marker
    live_res = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--rootdir",
            str(ROOT),
            "-c",
            str(ROOT / "pyproject.toml"),
            str(custom_test),
            "-m",
            "opencode_live",
            "--collect-only",
            "-q",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert live_res.returncode == 0
    live_lines = [line.strip() for line in live_res.stdout.splitlines() if "::" in line]
    assert len(live_lines) == 1
    assert "test_newly_marked_live_probe" in live_lines[0]

    # Collect with ordinary filter (not opencode_live)
    ord_res = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--rootdir",
            str(ROOT),
            "-c",
            str(ROOT / "pyproject.toml"),
            str(custom_test),
            "-m",
            "not browser and not opencode_live",
            "--collect-only",
            "-q",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ord_res.returncode == 0
    ord_lines = [line.strip() for line in ord_res.stdout.splitlines() if "::" in line]
    assert len(ord_lines) == 1
    assert "test_ordinary_control_probe" in ord_lines[0]


def test_default_collection_triggers_zero_docker_operations():
    """AS-005, REQ-001: Importing module and running static tests must not build image or invoke Docker."""
    from tests import test_opencode_container_runtime

    # Running static tests via pytest in subprocess with interceptor
    runner_code = """
import sys, subprocess

# Spy on subprocess.run and Popen
docker_invocations = []
orig_run = subprocess.run
orig_popen = subprocess.Popen

def spy_run(cmd, *args, **kwargs):
    prog = cmd[0] if isinstance(cmd, (list, tuple)) else cmd.split()[0]
    if "docker" in str(prog):
        docker_invocations.append(cmd)
    return orig_run(cmd, *args, **kwargs)

subprocess.run = spy_run

import pytest
exit_code = pytest.main([
    "tests/test_opencode_container_runtime.py",
    "-m", "not browser and not opencode_live",
    "-vv"
])

assert exit_code == 0, f"pytest failed with {exit_code}"
assert len(docker_invocations) == 0, f"Unexpected docker calls: {docker_invocations}"
print("SUCCESS: 0 docker invocations")
"""
    proc = subprocess.run(
        [sys.executable, "-c", runner_code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SUCCESS: 0 docker invocations" in proc.stdout


def test_prepare_image_fails_closed_when_docker_unavailable(tmp_path):
    """AS-004, REQ-005: Missing/unusable Docker causes prepare_image to fail with status: failed."""
    log_dir = tmp_path / "logs"

    # Mock docker command failure
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["docker", "info"],
            returncode=1,
            stdout="",
            stderr="Cannot connect to the Docker daemon",
        )
        rc = prepare_image(log_dir)

    assert rc != 0
    identity_file = log_dir / "image-identity.json"
    assert identity_file.exists()
    data = json.loads(identity_file.read_text(encoding="utf-8"))
    assert data["status"] == "failed"
    assert "Docker daemon is unavailable" in data["error"]


def test_prepare_image_records_provenance_and_exports_github_env(tmp_path):
    """AS-003, REQ-004, REQ-008: Image preparation resolves commit revision, image id, and exports to env."""
    log_dir = tmp_path / "logs"
    env_file = tmp_path / "github_env"
    env_file.touch()

    with patch("subprocess.run") as mock_run, patch("subprocess.Popen") as mock_popen:
        # Mock git rev-parse HEAD
        def fake_run(cmd, *args, **kwargs):
            if cmd == ["docker", "info"]:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="OK", stderr="")
            if cmd == ["git", "rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="b" * 40 + "\n", stderr="")
            if len(cmd) > 2 and cmd[0] == "docker" and cmd[1] == "inspect":
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="sha256:1234567890abcdef\n", stderr="")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        mock_run.side_effect = fake_run

        # Mock Popen for docker build
        class DummyPopen:
            returncode = 0
            stdout = ["Step 1/10 : FROM python\n", "Successfully built abcdef\n"]

            def wait(self):
                return 0

        mock_popen.return_value = DummyPopen()

        rc = prepare_image(log_dir, github_env_file=str(env_file))

    assert rc == 0
    identity_file = log_dir / "image-identity.json"
    assert identity_file.exists()
    data = json.loads(identity_file.read_text(encoding="utf-8"))
    assert data["status"] == "success"
    assert data["checkout_commit"] == "b" * 40
    assert data["image_tag"] == f"auto-coder:opencode-{('b' * 40)[:12]}"
    assert data["image_id"] == "sha256:1234567890abcdef"

    env_content = env_file.read_text(encoding="utf-8")
    assert f"AUTOCODER_OPENCODE_IMAGE=auto-coder:opencode-{('b' * 40)[:12]}" in env_content
    assert "AUTOCODER_OPENCODE_IMAGE_ID=sha256:1234567890abcdef" in env_content


def test_ensure_image_built_rejects_stale_fallback_when_prepared_image_missing():
    """AS-003, REQ-004: In dedicated live CI, failing to resolve prepared image fails closed without stale fallback."""
    from tests.test_opencode_container_runtime import _ensure_image_built

    # If AUTO_CODER_REQUIRE_OPENCODE_LIVE is set and AUTOCODER_OPENCODE_IMAGE is missing:
    with patch.dict(os.environ, {"AUTO_CODER_REQUIRE_OPENCODE_LIVE": "1"}, clear=False):
        os.environ.pop("AUTOCODER_OPENCODE_IMAGE", None)
        os.environ.pop("AUTO_CODER_OPENCODE_IMAGE", None)
        with pytest.raises(RuntimeError, match="Dedicated OpenCode live CI requires an explicitly prepared image"):
            _ensure_image_built()

    # If AUTOCODER_OPENCODE_IMAGE points to a nonexistent image:
    with patch.dict(os.environ, {"AUTOCODER_OPENCODE_IMAGE": "nonexistent:tag"}, clear=False):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["docker", "image", "inspect", "nonexistent:tag"],
                returncode=1,
                stdout="",
                stderr="Error: No such image",
            )
            with pytest.raises(RuntimeError, match="Specified OpenCode image 'nonexistent:tag' does not exist"):
                _ensure_image_built()


def test_opencode_cli_fails_closed_in_dedicated_live_ci():
    """AS-004, REQ-005: In dedicated CI, missing opencode CLI raises RuntimeError rather than skipping."""
    from tests.test_opencode_noedit_live import opencode_cli

    with patch.dict(os.environ, {"AUTO_CODER_REQUIRE_OPENCODE_LIVE": "1"}), patch("tests.test_opencode_noedit_live._find_or_install_opencode", return_value=None):
        with pytest.raises(RuntimeError, match="opencode CLI is required but not installed or usable"):
            # Call underlying fixture generator function if needed, or function directly
            opencode_cli.__wrapped__() if hasattr(opencode_cli, "__wrapped__") else opencode_cli()


def test_deliberate_skip_in_dedicated_ci_results_in_non_success(tmp_path):
    """AS-004, REQ-005: A skipped migrated scenario causes dedicated CI session to fail closed with exit status 1."""
    probe = tmp_path / "test_skip_probe.py"
    probe.write_text(
        """
import pytest

@pytest.mark.opencode_live
def test_ac001_container_executes_opencode_task_against_controlled_provider():
    pytest.skip("deliberate skip simulation")

@pytest.mark.opencode_live
def test_ac002_effective_home_and_runtime_authentication():
    pass

@pytest.mark.opencode_live
def test_ac003_retained_and_isolated_native_state_between_channels():
    pass

@pytest.mark.opencode_live
def test_ac004_no_baked_credentials_or_unsolicited_provider_calls():
    pass

@pytest.mark.opencode_live
def test_ac005_documentation_matches_production_compose_and_route():
    pass
""",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["AUTO_CODER_REQUIRE_OPENCODE_LIVE"] = "1"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.conftest",
            "--rootdir",
            str(ROOT),
            "-c",
            str(ROOT / "pyproject.toml"),
            str(probe),
            "-m",
            "opencode_live",
            "-vv",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode != 0, f"Expected non-zero exit code due to skipped scenario, got {proc.returncode}"
    assert "FAIL-CLOSED VALIDATION FAILED" in proc.stderr or "FAIL-CLOSED VALIDATION FAILED" in proc.stdout
    assert "test_ac001_container_executes_opencode_task_against_controlled_provider" in (proc.stderr + proc.stdout)


def test_zero_collected_live_tests_fails_closed():
    """AS-004, REQ-005: Zero collected live tests produces non-success exit code."""
    env = os.environ.copy()
    env["AUTO_CODER_REQUIRE_OPENCODE_LIVE"] = "1"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--rootdir",
            str(ROOT),
            "-c",
            str(ROOT / "pyproject.toml"),
            "-m",
            "opencode_live and not opencode_live",
            "-vv",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode != 0, f"Expected non-zero exit code when 0 tests are collected, got {proc.returncode}"
