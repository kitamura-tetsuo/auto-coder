"""Regression coverage for Issue #2169: `Publish Beta` excludes `opencode_live`-marked
tests from its test gate without weakening publication or the dedicated live workflow.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PUBLISH_BETA_WORKFLOW = ROOT / ".github/workflows/publish-beta.yml"
OPENCODE_LIVE_WORKFLOW = ROOT / ".github/workflows/opencode-live-tests.yml"


def _workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _extract_test_sh_run_step(job: dict) -> str:
    for step in job["steps"]:
        run = step.get("run")
        if isinstance(run, str) and "scripts/test.sh" in run:
            return run.strip()
    raise AssertionError("No step invoking scripts/test.sh found")


def _extract_pytest_marker_args(test_sh_invocation: str) -> list[str]:
    """Parse the shell tokens passed to scripts/test.sh, which local_test_log_collector.py
    forwards verbatim to pytest via `"$@"`.
    """
    tokens = shlex.split(test_sh_invocation)
    assert tokens[:2] == ["bash", "scripts/test.sh"], tokens
    return tokens[2:]


def test_publish_beta_invokes_test_sh_with_opencode_live_exclusion_only():
    """REQ-001, REQ-006: The workflow-level command excludes only opencode_live, unlike
    the PR-shard selector which also excludes browser tests.
    """
    workflow = _workflow(PUBLISH_BETA_WORKFLOW)
    job = workflow["jobs"]["build-tested-artifact"]
    run_step = _extract_test_sh_run_step(job)

    assert "bash scripts/test.sh" in run_step
    forwarded_args = _extract_pytest_marker_args(run_step)
    assert forwarded_args == ["-m", "not opencode_live"], forwarded_args

    # Must not accidentally reuse the PR-shard expression, which would also drop
    # browser-only tests from the publication gate.
    assert "browser" not in run_step


def test_publish_beta_gate_runs_before_docker_build_push():
    """REQ-003, REQ-004: The test step still precedes login/build/push, and provenance
    plus the source-SHA tag remain intact.
    """
    workflow = _workflow(PUBLISH_BETA_WORKFLOW)
    job = workflow["jobs"]["build-tested-artifact"]
    step_descriptors = [step.get("run") or step.get("uses") for step in job["steps"]]

    test_index = next(i for i, d in enumerate(step_descriptors) if d and "scripts/test.sh" in d)
    build_index = next(i for i, d in enumerate(step_descriptors) if d and "build-push-action" in d)
    assert test_index < build_index

    build_step = job["steps"][build_index]
    assert build_step["with"]["provenance"] is True
    assert "sha-${{ github.sha }}" in build_step["with"]["tags"]
    assert "AUTO_CODER_SOURCE_REVISION=${{ github.sha }}" in build_step["with"]["build-args"]


def test_publish_beta_selection_excludes_only_live_tests_from_actual_collection():
    """AS-001: Using the exact marker expression extracted from the workflow, the
    effective selection on real test modules excludes opencode_live items while
    retaining static OpenCode tests and browser-only tests.
    """
    workflow = _workflow(PUBLISH_BETA_WORKFLOW)
    job = workflow["jobs"]["build-tested-artifact"]
    run_step = _extract_test_sh_run_step(job)
    marker_expr = _extract_pytest_marker_args(run_step)[1]
    assert marker_expr == "not opencode_live"

    targets = [
        "tests/test_opencode_container_runtime.py",
        "tests/test_opencode_noedit_live.py",
    ]

    beta_collect = subprocess.run(
        [sys.executable, "-m", "pytest", *targets, "-m", marker_expr, "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert beta_collect.returncode == 0, beta_collect.stdout + beta_collect.stderr
    beta_ids = {line.strip() for line in beta_collect.stdout.splitlines() if "::" in line}
    beta_names = {node_id.split("::")[-1] for node_id in beta_ids}

    live_collect = subprocess.run(
        [sys.executable, "-m", "pytest", *targets, "-m", "opencode_live", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert live_collect.returncode == 0, live_collect.stdout + live_collect.stderr
    live_ids = {line.strip() for line in live_collect.stdout.splitlines() if "::" in line}
    live_names = {node_id.split("::")[-1] for node_id in live_ids}

    default_collect = subprocess.run(
        [sys.executable, "-m", "pytest", *targets, "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert default_collect.returncode == 0, default_collect.stdout + default_collect.stderr
    default_ids = {line.strip() for line in default_collect.stdout.splitlines() if "::" in line}
    default_names = {node_id.split("::")[-1] for node_id in default_ids}

    # Disjoint and conservative: Beta selection is exactly default minus live.
    assert not (beta_names & live_names)
    assert beta_names | live_names == default_names

    static_container_tests = {
        "test_dockerfile_pins_opencode_release_and_explicit_architectures",
        "test_compose_channels_runtime_mounts_and_isolation",
        "test_effective_home_and_xdg_storage_resolution",
        "test_documentation_describes_opencode_container_runtime",
    }
    assert static_container_tests <= beta_names
    assert not (static_container_tests & live_names)


def test_publish_beta_selection_conserves_dynamically_added_live_and_browser_probes(tmp_path):
    """AS-001: A newly introduced opencode_live probe (function-level, module-level,
    and mixed with a browser marker) is excluded from the Beta selection without any
    selector update, while an ordinary/browser-only probe in the same file remains
    selected.
    """
    probe = tmp_path / "test_unrelated_publish_beta_probe.py"
    probe.write_text(
        """
import pytest


@pytest.mark.opencode_live
def test_function_level_live_probe():
    assert True


@pytest.mark.browser
@pytest.mark.opencode_live
def test_browser_and_live_probe():
    assert True


@pytest.mark.browser
def test_browser_only_probe():
    assert True


def test_ordinary_probe():
    assert True


@pytest.mark.opencode_live
class TestClassLevelLive:
    def test_inherited_marker_probe(self):
        assert True
""",
        encoding="utf-8",
    )

    beta_collect = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--rootdir",
            str(ROOT),
            "-c",
            str(ROOT / "pyproject.toml"),
            str(probe),
            "-m",
            "not opencode_live",
            "--collect-only",
            "-q",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert beta_collect.returncode == 0, beta_collect.stdout + beta_collect.stderr
    beta_names = {line.strip().split("::")[-1] for line in beta_collect.stdout.splitlines() if "::" in line}

    assert beta_names == {"test_browser_only_probe", "test_ordinary_probe"}


def test_local_default_and_explicit_live_invocations_are_unaffected():
    """REQ-006: A bare local scripts/test.sh call keeps default discovery (both
    categories); an explicit local `-m opencode_live` call still selects live items.
    Only the Publish Beta workflow-level invocation applies the new exclusion.
    """
    script = (ROOT / "scripts/test.sh").read_text(encoding="utf-8")
    # The script itself must not hardcode any marker filtering; it only forwards
    # whatever arguments its caller supplies to the collector/pytest.
    assert "opencode_live" not in script
    assert 'src/auto_coder/local_test_log_collector.py "$@"' in script


def test_opencode_live_tests_workflow_unaffected_by_publish_beta_change():
    """REQ-005: The dedicated live workflow keeps its independent triggers, live
    selection, and required-live environment untouched by the Beta exclusion.
    """
    workflow = _workflow(OPENCODE_LIVE_WORKFLOW)
    assert set(workflow["on"]) == {"pull_request", "workflow_dispatch"}
    job = workflow["jobs"]["opencode-live-tests"]
    assert "needs" not in job

    test_step = next(s for s in job["steps"] if s["name"] == "Run opencode_live-marked tests")
    assert test_step["env"]["AUTO_CODER_REQUIRE_OPENCODE_LIVE"] == "1"
    assert "-m opencode_live" in test_step["run"]
