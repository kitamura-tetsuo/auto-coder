"""Regression coverage for the dedicated `Browser Tests` GitHub Actions
workflow and the `browser` pytest marker that feeds it (Issue: "Run browser
tests in a dedicated GitHub Actions workflow").

These tests exercise the real production boundaries the Issue's acceptance
scenarios describe: the actual workflow YAML files on disk, the real pytest
marker registration/collection machinery (no synthetic in-memory config),
and the real `scripts/browser_preflight.py` script executed as a subprocess
exactly as CI invokes it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BROWSER_WORKFLOW = ROOT / ".github/workflows/browser-tests.yml"
PR_WORKFLOW = ROOT / ".github/workflows/pr-tests.yml"
PREFLIGHT_SCRIPT = ROOT / "scripts" / "browser_preflight.py"


def _workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_browser_tests_workflow_contract():
    """REQ-003, REQ-004, REQ-005: the dedicated workflow installs Chromium at
    a stable path, preflights it, and fails closed rather than skipping."""
    workflow = _workflow(BROWSER_WORKFLOW)
    assert workflow["name"] == "Browser Tests"
    assert set(workflow["on"]) == {"pull_request", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}

    job = workflow["jobs"]["browser-tests"]
    assert job["name"] == "Browser Tests"
    assert "needs" not in job, "Browser Tests must not depend on PR Tests completing"

    browsers_path = job["env"]["PLAYWRIGHT_BROWSERS_PATH"]
    assert browsers_path == "${{ github.workspace }}/.playwright-browsers"

    steps = job["steps"]
    names = [step["name"] for step in steps]
    install_index = names.index("Install Playwright Chromium")
    preflight_index = next(i for i, name in enumerate(names) if name.startswith("Preflight"))
    test_index = next(i for i, name in enumerate(names) if name.startswith("Run browser-marked tests"))
    assert install_index < preflight_index < test_index, "Chromium must be installed and preflighted before tests run"

    preflight_step = steps[preflight_index]
    assert "scripts/browser_preflight.py" in preflight_step["run"]

    test_step = steps[test_index]
    assert test_step["env"]["AUTO_CODER_REQUIRE_BROWSER"] == "1"
    assert "-m browser" in test_step["run"]
    for excluder in ("--splits", "--group", " -k ", "test_dashboard_detail_scroll_stability.py", "test_dashboard_slots_scroll_stability.py"):
        assert excluder not in test_step["run"], f"selection must be marker-based only, not {excluder!r}"


def test_pr_tests_and_browser_tests_have_no_cross_workflow_dependency():
    """REQ-003, REQ-006: the two workflows run independently; PR Tests no
    longer installs a Chromium binary now that browser tests moved out."""
    pr_workflow = _workflow(PR_WORKFLOW)
    browser_workflow = _workflow(BROWSER_WORKFLOW)

    assert "needs" not in browser_workflow["jobs"]["browser-tests"]
    for job in pr_workflow["jobs"].values():
        assert "browser-tests" not in str(job.get("needs", ""))

    shard = pr_workflow["jobs"]["tests-shard"]
    step_names = [step["name"] for step in shard["steps"]]
    assert not any("Playwright" in name or "Chromium" in name for name in step_names)
    for step in shard["steps"]:
        assert "playwright install" not in step.get("run", "")


def test_browser_marker_is_registered_and_selects_exactly_the_real_browser_tests():
    """AS-005: the `browser` marker is the actual CI selection boundary --
    collect against the real pyproject.toml/conftest.py, not a synthetic
    config, and confirm the marker selects precisely the files that launch a
    real Chromium and nothing else."""
    collect = subprocess.run(
        [sys.executable, "-m", "pytest", "-m", "browser", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert collect.returncode == 0, collect.stdout + collect.stderr
    collected_ids = {line.strip() for line in collect.stdout.splitlines() if "::" in line}
    assert collected_ids, "expected at least one browser-marked test to be collected"
    files = {node_id.split("::", 1)[0] for node_id in collected_ids}
    assert files == {
        "tests/test_dashboard_detail_scroll_stability.py",
        "tests/test_dashboard_slots_scroll_stability.py",
    }

    exclude = subprocess.run(
        [sys.executable, "-m", "pytest", "-m", "not browser", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert exclude.returncode == 0, exclude.stdout + exclude.stderr
    excluded_ids = {line.strip() for line in exclude.stdout.splitlines() if "::" in line}
    assert not (excluded_ids & collected_ids), "PR Tests' '-m not browser' must not select any browser-marked test"


def test_browser_preflight_fails_closed_when_chromium_is_unusable(tmp_path):
    """AS-003: a missing/unusable Chromium must make the dedicated preflight
    step fail with launch evidence, run for real (no mocked launch)."""
    empty_browsers_dir = tmp_path / "no-browser-installed-here"
    empty_browsers_dir.mkdir()
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(empty_browsers_dir)

    result = subprocess.run(
        [sys.executable, str(PREFLIGHT_SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode != 0
    assert "FAILED" in result.stderr
    assert "could not launch a real headless Chromium" in result.stderr


def test_headless_page_helper_fails_pytest_when_browser_unusable_and_required(tmp_path):
    """REQ-005: the fail-closed contract belongs to `headless_page` itself,
    not only to the standalone preflight script -- a selected `browser` test
    hitting an unusable Chromium after preflight has already passed must
    still turn into a real pytest failure (never a skip) whenever
    `AUTO_CODER_REQUIRE_BROWSER=1` is set. Exercises the actual Playwright
    launch path (no mocking): an isolated pytest subprocess runs a throwaway
    test that calls the real `headless_page` context manager against a
    deliberately empty `PLAYWRIGHT_BROWSERS_PATH`.
    """
    probe = tmp_path / "test_headless_page_probe.py"
    probe.write_text(
        f"""
import sys
sys.path.insert(0, {str(ROOT)!r})

from tests.support.browser_launch import headless_page


def test_real_launch_attempt():
    with headless_page(viewport={{"width": 100, "height": 100}}):
        pass
""",
        encoding="utf-8",
    )

    empty_browsers_dir = tmp_path / "no-browser-installed-here"
    empty_browsers_dir.mkdir()
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(empty_browsers_dir)
    env["AUTO_CODER_REQUIRE_BROWSER"] = "1"

    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(probe), "-p", "no:cacheprovider", "-q"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "1 failed" in result.stdout, result.stdout + result.stderr
    assert "skipped" not in result.stdout, "an unusable browser must fail, not skip, when AUTO_CODER_REQUIRE_BROWSER=1"
    assert "BrowserType.launch" in result.stdout or "Executable doesn't exist" in result.stdout, result.stdout
