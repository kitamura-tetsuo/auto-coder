"""Regression guards for pull-request-only workflow supersession (Issue #2728)."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = {
    "PR Tests": ROOT / ".github/workflows/pr-tests.yml",
    "Browser Tests": ROOT / ".github/workflows/browser-tests.yml",
    "OpenCode Live Tests": ROOT / ".github/workflows/opencode-live-tests.yml",
    "Muse Live Tests": ROOT / ".github/workflows/muse-live-tests.yml",
    "Supervisor Integration Tests": ROOT / ".github/workflows/supervisor-integration-tests.yml",
    "Test Source for Runtime": ROOT / ".github/workflows/test-source-for-runtime.yml",
}
GROUP_EXPRESSION = "${{ github.workflow }}-${{ github.event_name == 'pull_request' && format('pr-{0}', github.event.pull_request.number) || format('{0}-{1}', github.event_name, github.run_id) }}"
CANCEL_EXPRESSION = "${{ github.event_name == 'pull_request' }}"


def _load(name: str) -> dict:
    return yaml.safe_load(WORKFLOWS[name].read_text(encoding="utf-8"))


def _effective_group(workflow: str, event: str, *, run_id: int, pr: int | None = None) -> str:
    """Express the two branches of the production Actions expression for its matrix oracle."""
    suffix = f"pr-{pr}" if event == "pull_request" else f"{event}-{run_id}"
    return f"{workflow}-{suffix}"


def test_all_six_production_workflows_have_pr_only_supersession() -> None:
    """REQ-001/REQ-002: inspect every production definition, not a copied fixture."""
    for expected_name in WORKFLOWS:
        workflow = _load(expected_name)
        assert workflow["name"] == expected_name
        assert workflow["concurrency"] == {
            "group": GROUP_EXPRESSION,
            "cancel-in-progress": CANCEL_EXPRESSION,
        }
        expected_permissions = {"contents": "read", "actions": "read"} if expected_name in {"Test Source for Runtime", "Supervisor Integration Tests"} else {"contents": "read"}
        assert workflow["permissions"] == expected_permissions


def test_group_event_matrix_isolates_workflows_prs_and_non_pr_runs() -> None:
    """REQ-002/REQ-004: only arrival in an identical workflow/PR group supersedes."""
    old = _effective_group("PR Tests", "pull_request", pr=41, run_id=100)
    newer_revision = _effective_group("PR Tests", "pull_request", pr=41, run_id=101)
    other_pr = _effective_group("PR Tests", "pull_request", pr=42, run_id=102)
    other_workflow = _effective_group("Browser Tests", "pull_request", pr=41, run_id=103)

    assert old == newer_revision
    assert len({old, other_pr, other_workflow}) == 3
    assert _effective_group("PR Tests", "workflow_dispatch", run_id=201) != _effective_group("PR Tests", "workflow_dispatch", run_id=202)
    assert _effective_group("Test Source for Runtime", "push", run_id=301) != _effective_group("Test Source for Runtime", "push", run_id=302)


def test_existing_trigger_checkout_and_check_identities_are_preserved() -> None:
    """REQ-003/REQ-004: retain event, revision, permission, and check boundaries."""
    pr = _load("PR Tests")
    assert pr["on"] == {
        "pull_request": {"types": ["opened", "synchronize", "reopened"]},
        "workflow_dispatch": None,
    }
    assert {job["name"] for job in pr["jobs"].values()} >= {
        "Lint & Type Check",
        "Tests with Coverage (${{ matrix.group }}/4)",
    }
    shard = pr["jobs"]["tests-shard"]
    assert shard["strategy"] == {"fail-fast": False, "matrix": {"group": [1, 2, 3, 4]}}
    shard_run = next(step["run"] for step in shard["steps"] if step.get("name", "").startswith("Run tests with coverage"))
    assert "scripts/run_pr_test_shard.py" in shard_run

    for name in ("Browser Tests", "OpenCode Live Tests", "Muse Live Tests", "Supervisor Integration Tests"):
        assert set(_load(name)["on"]) == {"pull_request", "workflow_dispatch"}

    source = _load("Test Source for Runtime")
    assert source["on"] == {"push": {"branches": ["main"]}, "pull_request": None}
    assert source["jobs"]["source-quality"]["name"] == "Source identity and quality"
    assert source["jobs"]["complete-source-tests"]["name"] == "Complete source tests with Chromium"
    source_checkout = source["jobs"]["complete-source-shard"]["steps"][0]
    assert source_checkout["with"]["ref"] == "${{ github.event.pull_request.head.sha || github.sha }}"


def test_required_profiles_and_fail_closed_prerequisites_are_unchanged() -> None:
    """REQ-003/REQ-005/REQ-006: the scheduling policy cannot weaken test or cleanup gates."""
    browser_steps = {step.get("name", ""): step for step in _load("Browser Tests")["jobs"]["browser-tests"]["steps"]}
    assert "playwright install --with-deps chromium" in browser_steps["Install Playwright Chromium"]["run"]
    assert "scripts/browser_preflight.py" in browser_steps["Preflight - verify a real headless Chromium launches"]["run"]
    assert browser_steps["Run browser-marked tests"]["env"]["AUTO_CODER_REQUIRE_BROWSER"] == "1"
    assert "pytest -m browser" in browser_steps["Run browser-marked tests"]["run"]

    opencode_steps = {step.get("name", ""): step for step in _load("OpenCode Live Tests")["jobs"]["opencode-live-tests"]["steps"]}
    assert "opencode-ai@1.18.31" in opencode_steps["Install pinned OpenCode CLI"]["run"]
    assert "scripts/prepare_opencode_image.py" in opencode_steps["Prepare OpenCode runtime image"]["run"]
    assert opencode_steps["Run opencode_live-marked tests"]["env"]["AUTO_CODER_REQUIRE_OPENCODE_LIVE"] == "1"
    assert "pytest -m opencode_live" in opencode_steps["Run opencode_live-marked tests"]["run"]

    muse_steps = {step.get("name", ""): step for step in _load("Muse Live Tests")["jobs"]["muse-live-tests"]["steps"]}
    assert "version=1.4.6-R6000.1" in muse_steps["Install pinned Muse Code host"]["run"]
    assert "sha256sum --check" in muse_steps["Install pinned Muse Code host"]["run"]
    assert muse_steps["Run real-host conformance"]["run"] == "uv run pytest -m muse_live -vv tests/test_muse_background_live.py"

    supervisor_workflow = _load("Supervisor Integration Tests")
    supervisor_steps = {step.get("name", ""): step for step in supervisor_workflow["jobs"]["isolated-containers"]["steps"]}
    required_container = supervisor_steps["Verify isolated Supervisor containers"]
    assert supervisor_workflow["jobs"]["isolated-containers"]["env"]["AUTO_CODER_REQUIRE_SUPERVISOR_INTEGRATION"] == "1"
    assert "pytest" in required_container["run"]

    source_job = _load("Test Source for Runtime")["jobs"]["complete-source-shard"]
    assert source_job["env"]["AUTO_CODER_REQUIRE_BROWSER"] == "1"
    source_steps = {step.get("name", ""): step for step in source_job["steps"]}
    assert "scripts/prepare_management_test_image.py" in source_steps["Prepare Python-only management test fixture"]["run"]
    assert "scripts/browser_preflight.py" in source_steps["Verify real headless Chromium"]["run"]
    assert "-m 'not opencode_live'" in source_steps["Run complete required source selection"]["run"]

    runner = (ROOT / "scripts/run_pr_test_shard.py").read_text(encoding="utf-8")
    assert "os.killpg(pgid, signal.SIGTERM)" in runner
    assert "os.killpg(pgid, signal.SIGKILL)" in runner
    assert 'result = "cancelled" if _stop_group(process, pgid, grace) else "cleanup-failure"' in runner
    assert 'if outcome != "timeout" or attempt == args.max_attempts or cancel_signal is not None:' in runner
