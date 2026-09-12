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

import pytest
import yaml

from tests.support.browser_launch import headless_page

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


REAL_BROWSER_MODULES = (
    "tests/test_dashboard_detail_scroll_stability.py",
    "tests/test_dashboard_slots_scroll_stability.py",
)


def test_browser_marker_is_registered_and_selects_exactly_the_real_browser_tests():
    """AS-005, REQ-001, REQ-003: the `browser` marker is the actual CI
    selection boundary -- collect against the real pyproject.toml/conftest.py,
    not a synthetic config, and confirm the marker selects precisely the
    files that launch a real Chromium and nothing else.

    Compares full node IDs, not just filenames: a filename-only check would
    still pass if a module's `pytestmark = pytest.mark.browser` were
    replaced with `@pytest.mark.browser` on only one function in that
    module -- the module would still appear in the selected file set, while
    its other real-browser tests silently fell out of `Browser Tests` and
    into the `not browser` selection's tolerant-skip fallback. Collecting
    each real-browser module without any marker filter and requiring every
    one of those nodes to appear in the `-m browser` selection catches that.
    """
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
        *REAL_BROWSER_MODULES,
        "tests/test_browser_tests_workflow.py",
    }

    unfiltered = subprocess.run(
        [sys.executable, "-m", "pytest", *REAL_BROWSER_MODULES, "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert unfiltered.returncode == 0, unfiltered.stdout + unfiltered.stderr
    unfiltered_ids = {line.strip() for line in unfiltered.stdout.splitlines() if "::" in line}
    assert unfiltered_ids, "expected the real-browser dashboard modules to contain collectible tests"
    assert unfiltered_ids <= collected_ids, "every test node in the real-browser dashboard modules must be selected by '-m browser'; " f"missing from the browser selection: {unfiltered_ids - collected_ids}"

    assert "tests/test_browser_tests_workflow.py::test_headless_page_discovers_browser_despite_per_test_home_rewrite" in collected_ids

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


def test_headless_page_helper_skips_instead_of_failing_when_browser_unusable_and_not_required(tmp_path):
    """REQ-006: browser-test separation must not force an ordinary local or
    non-dedicated pytest run to provision Chromium. Mirrors the strict-mode
    test above but with `AUTO_CODER_REQUIRE_BROWSER` absent (the ordinary/
    local default): an unusable browser must be a pytest skip, and the run
    as a whole must still succeed, not fail the process."""
    probe = tmp_path / "test_headless_page_probe_tolerant.py"
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
    env.pop("AUTO_CODER_REQUIRE_BROWSER", None)

    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(probe), "-p", "no:cacheprovider", "-q"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 skipped" in result.stdout, result.stdout + result.stderr
    assert "failed" not in result.stdout, "an unusable browser must not fail the run when AUTO_CODER_REQUIRE_BROWSER is unset"


_HOME_AT_COLLECTION = os.environ.get("HOME")


@pytest.mark.browser
def test_headless_page_discovers_browser_despite_per_test_home_rewrite():
    """REQ-004: `headless_page`'s Chromium discovery (via `PLAYWRIGHT_BROWSERS_PATH`)
    must remain valid even though `tests/conftest.py`'s autouse
    `_clear_sensitive_env` fixture rewrites `$HOME` to a fresh temporary
    directory for every test that does not request the `_use_real_home`
    fixture. All of the current `browser`-marked dashboard tests *do*
    request `_use_real_home`, so none of them actually exercises that
    rewrite while launching a real browser -- this test deliberately omits
    `_use_real_home` so the real per-test `HOME` rewrite is in effect, then
    proves Chromium still launches through the real shared helper.

    Deliberately does *not* force `AUTO_CODER_REQUIRE_BROWSER=1` itself
    (REQ-006): the dedicated `Browser Tests` workflow already supplies that
    env var, which is what turns a launch failure here into a hard failure
    there; an ordinary local run without it must still get the tolerant
    skip from `headless_page` if Chromium is unusable. See
    `test_headless_page_discovers_browser_despite_per_test_home_rewrite_is_skippable_locally`
    below for the regression oracle proving that composed behavior.
    """
    rewritten_home = os.environ.get("HOME")
    assert rewritten_home != _HOME_AT_COLLECTION, "expected the autouse HOME rewrite to be in effect for this test"
    assert rewritten_home is not None and "ac_test_home_" in rewritten_home, f"unexpected $HOME during test: {rewritten_home!r}"

    with headless_page(viewport={"width": 100, "height": 100}) as page:
        page.goto("about:blank")


def test_headless_page_discovers_browser_despite_per_test_home_rewrite_is_skippable_locally(tmp_path):
    """REQ-006: the REQ-004 regression above must not force strict mode on
    itself. Run it as an isolated pytest subprocess with an empty
    `PLAYWRIGHT_BROWSERS_PATH` and `AUTO_CODER_REQUIRE_BROWSER` absent (the
    ordinary local/non-dedicated default) and require a skip, not a
    failure -- a helper-level skip probe alone would miss a regression
    where the *caller* (not `headless_page` itself) forces strict mode.
    """
    empty_browsers_dir = tmp_path / "no-browser-installed-here"
    empty_browsers_dir.mkdir()
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(empty_browsers_dir)
    env.pop("AUTO_CODER_REQUIRE_BROWSER", None)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"{__file__}::test_headless_page_discovers_browser_despite_per_test_home_rewrite",
            "-p",
            "no:cacheprovider",
            "-q",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 skipped" in result.stdout, result.stdout + result.stderr
    assert "failed" not in result.stdout, "the REQ-004 test must not force a failure in an ordinary local run without a provisioned browser"


def test_browser_tests_step_propagates_pytest_failure_and_has_no_continue_on_error(tmp_path):
    """REQ-005: the dedicated workflow's `Run browser-marked tests` step must
    not swallow a non-zero pytest exit status. A minimal incorrect
    implementation could append `|| true` to the step's `run:` script, or
    set `continue-on-error: true` on the step -- either would let the
    dedicated check go green while a browser-marked test actually failed.

    Executes the step's real `run:` script verbatim (the same shell
    GitHub Actions uses by default: `bash --noprofile --norc -eo pipefail`)
    with a stub `uv` placed first on PATH that makes the pytest invocation
    exit non-zero, and requires the step's shell to exit non-zero too.
    """
    workflow = _workflow(BROWSER_WORKFLOW)
    job = workflow["jobs"]["browser-tests"]
    test_step = next(step for step in job["steps"] if step["name"].startswith("Run browser-marked tests"))
    assert test_step.get("continue-on-error") is not True, "the test step must not be configured to ignore failure"

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub_uv = stub_bin / "uv"
    stub_uv.write_text("#!/bin/bash\necho 'stub uv: simulating a failing pytest invocation' >&2\nexit 7\n", encoding="utf-8")
    stub_uv.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{stub_bin}:{env['PATH']}"

    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", test_step["run"]],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0, "the step's shell must propagate a failing pytest invocation's exit status"
