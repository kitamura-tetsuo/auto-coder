"""Focused-verification policy for PR CI-failure repair (Issue #2423).

The workflow tests run the production repair helper with the real explicit-file
runner (`run_local_tests`) against a real script in a throwaway git repository.
Only the provider boundary and GitHub/push boundaries are controlled.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Dict, List
from unittest.mock import patch

import pytest

from src.auto_coder import pr_processor
from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.ci_repair_verification import (
    TargetStatus,
    classify_target_result,
    dedupe_targets,
    follow_up_budget_exhausted,
)
from src.auto_coder.invocation_admission import ci_repair_designated
from src.auto_coder.prompt_loader import render_prompt

SCRIPT = r"""#!/bin/bash
# Records every invocation. File state decides the outcome; unscoped runs are flagged.
echo "$*" >> "$LOG"
if [ $# -eq 0 ]; then
  echo UNSCOPED >> "$LOG"
  touch "$SENTINEL"
  exit 0
fi
case "$1" in
  tests/test_nomatch.py) echo "no tests ran"; exit 0;;
  tests/test_unsupported.py) echo "error: unrecognized arguments: $1" >&2; exit 2;;
esac
if grep -q PASS "$1"; then echo "ok $1"; exit 0; fi
echo "FAILED $1::test_it - AssertionError"
exit 1
"""


class Harness:
    def __init__(self, repo: Path):
        self.repo = repo
        self.log = repo.parent / "invocations.log"
        self.sentinel = repo.parent / "unscoped-sentinel"
        self.prompts: List[str] = []
        self.designated: List[bool] = []
        self.initial_edit: Callable[[], None] = lambda: None
        self.follow_ups: List[Callable[[], None]] = []
        self.commit_ok = True
        self.push_ok = True
        self.commits = 0
        self.pushes = 0

    def set_state(self, name: str, passing: bool) -> None:
        (self.repo / "tests" / name).write_text("PASS\n" if passing else "FAIL\n")

    def invocations(self) -> List[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []


@pytest.fixture
def harness(tmp_path, monkeypatch, _use_real_commands) -> Harness:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "scripts" / "test.sh").write_text(SCRIPT)
    for name in ("a", "b", "c", "d", "nomatch", "unsupported"):
        (repo / "tests" / f"test_{name}.py").write_text("FAIL\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], cwd=repo, check=True)
    h = Harness(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("LOG", str(h.log))
    monkeypatch.setenv("SENTINEL", str(h.sentinel))

    def initial_provider(prompt, backend_manager=None, is_noedit=False):
        h.prompts.append(prompt)
        h.designated.append(ci_repair_designated())
        h.initial_edit()
        return "ACTION_SUMMARY: initial"

    class Manager:
        def run_test_fix_prompt(self, prompt, current_test_file=None):
            h.prompts.append(prompt)
            h.designated.append(ci_repair_designated())
            if h.follow_ups:
                h.follow_ups.pop(0)()
            return "ACTION_SUMMARY: follow-up"

    def commit(msg, *a, **k):
        h.commits += 1
        return SimpleNamespace(success=h.commit_ok, stderr="" if h.commit_ok else "commit boom")

    def push(*a, **k):
        h.pushes += 1
        return SimpleNamespace(success=h.push_ok, stderr="" if h.push_ok else "push boom")

    with (
        patch.object(pr_processor, "run_llm_prompt", initial_provider),
        patch.object(pr_processor, "get_llm_backend_manager", return_value=Manager()),
        patch.object(pr_processor, "create_high_score_backend_manager", side_effect=RuntimeError("none")),
        patch.object(pr_processor, "git_commit_with_retry", commit),
        patch.object(pr_processor, "git_push", push),
        patch.object(pr_processor, "get_commit_log", return_value=""),
        patch.object(pr_processor, "get_linked_issues_context", return_value=""),
        patch("src.auto_coder.util.github_action.is_item_closed_on_github", return_value=False),
        patch("src.auto_coder.util.gh_cache.GitHubClient.get_instance", return_value=SimpleNamespace()),
    ):
        yield h


def _run(h: Harness, failed, logs="FAILED tests/test_a.py::test_it - AssertionError\nbuild step failed: lint", max_attempts=None, **cfg):
    config = AutomationConfig(**cfg)
    if max_attempts is not None:
        config.MAX_FIX_ATTEMPTS = max_attempts
    pr = {"number": 7, "title": "t", "body": "", "head": {"ref": "x", "sha": "h1"}}
    return pr_processor._fix_pr_issues_with_testing("o/r", pr, config, logs, failed)


def test_all_distinct_files_are_selected_none_unscoped(harness):
    h = harness
    h.set_state("test_a.py", True)
    h.set_state("test_b.py", False)
    h.set_state("test_c.py", True)
    h.set_state("test_d.py", True)
    h.initial_edit = lambda: (h.repo / "fix.txt").write_text("edit")
    h.follow_ups = [lambda: h.set_state("test_b.py", True)]
    files = ["tests/test_a.py", "tests/test_b.py", "tests/test_a.py", "tests/test_c.py", "tests/test_d.py"]
    actions = _run(h, files)

    first_sweep = h.invocations()[:4]
    assert first_sweep == ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py", "tests/test_d.py"]
    # the follow-up sweep re-runs the complete F, not just the failing file
    assert h.invocations()[4:] == first_sweep
    assert "UNSCOPED" not in h.invocations() and not h.sentinel.exists()
    assert h.designated == [True, True]
    assert (h.commits, h.pushes) == (1, 1)
    assert any("passed locally on the latest corrected state" in a and "full validation pending CI" in a for a in actions)
    assert not ci_repair_designated()


def test_earlier_pass_is_not_final_state_and_budget_is_shared(harness):
    h = harness
    h.set_state("test_a.py", True)
    h.set_state("test_b.py", False)

    def fix_b_break_a():
        h.set_state("test_b.py", True)
        h.set_state("test_a.py", False)

    h.follow_ups = [fix_b_break_a]
    actions = _run(h, ["tests/test_a.py", "tests/test_b.py"], max_attempts=1)
    assert h.invocations() == ["tests/test_a.py", "tests/test_b.py", "tests/test_a.py", "tests/test_b.py"]
    assert "Focused check failed: tests/test_a.py" in actions
    assert any("Max fix attempts (1)" in a for a in actions)
    assert not any("passed locally" in a for a in actions)
    assert sum("Applied local test fix" in a for a in actions) == 1

    # with room for another correction A is re-verified and the aggregate is reported
    h2_state = [lambda: (h.set_state("test_b.py", True), h.set_state("test_a.py", False)), lambda: h.set_state("test_a.py", True)]
    h.set_state("test_a.py", True)
    h.set_state("test_b.py", False)
    h.follow_ups = list(h2_state)
    h.log.unlink()
    actions = _run(h, ["tests/test_a.py", "tests/test_b.py"], max_attempts=5)
    assert h.invocations()[-2:] == ["tests/test_a.py", "tests/test_b.py"]
    assert any("passed locally on the latest corrected state" in a for a in actions)


def test_no_selectors_keeps_ci_diagnostics_and_submits_without_local_claim(harness):
    h = harness
    h.initial_edit = lambda: (h.repo / "fix.txt").write_text("edit")
    actions = _run(h, [], logs="build step failed: lint error E501")
    assert h.invocations() == [] and not h.sentinel.exists()
    assert "lint error E501" in h.prompts[0]
    assert any("Local verification not performed" in a for a in actions)
    assert not any("passed locally" in a for a in actions)
    assert any("full validation is pending CI" in a for a in actions)
    assert h.commits == 1 and h.pushes == 1


def test_operator_deferral_publishes_correction_without_local_test_claim(harness, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_DEFER_LOCAL_TESTS", "1")
    harness.initial_edit = lambda: (harness.repo / "fix.txt").write_text("edit")
    actions = _run(harness, ["tests/test_a.py"])
    assert harness.invocations() == []
    assert not harness.sentinel.exists()
    assert harness.commits == 1 and harness.pushes == 1
    assert "Local verification deferred by operator until after merge; GitHub CI remains authoritative" in actions
    assert not any("passed locally" in action for action in actions)


def test_unavailable_targets_are_unverified_and_never_trigger_repair_or_full_run(harness):
    h = harness
    h.set_state("test_a.py", True)
    actions = _run(h, ["tests/test_missing.py", "tests/test_nomatch.py", "tests/test_unsupported.py", "tests/test_a.py"])
    assert h.invocations() == ["tests/test_nomatch.py", "tests/test_unsupported.py", "tests/test_a.py"]
    assert h.designated == [True]  # only the initial CI-log correction; no follow-up from unavailability
    assert "Focused check unverified: tests/test_missing.py (target file not found in the working tree)" in actions
    assert any("tests/test_nomatch.py (runner reported no matching tests)" in a for a in actions)
    assert any("per-file selection" in a for a in actions)
    assert "Focused check passed: tests/test_a.py" in actions
    assert not any("passed locally" in a for a in actions)
    assert not h.sentinel.exists()


def test_submission_failures_are_not_reported_as_submitted(harness):
    h = harness
    h.set_state("test_a.py", True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "baseline"], cwd=h.repo, check=True)
    actions = _run(h, ["tests/test_a.py"])  # no edit produced
    assert "No changes generated by CI repair; nothing submitted" in actions
    assert (h.commits, h.pushes) == (0, 0)

    h.initial_edit = lambda: (h.repo / "fix.txt").write_text("edit")
    h.commit_ok = False
    actions = _run(h, ["tests/test_a.py"])
    assert any("Failed to commit" in a for a in actions) and not any("Pushed fixes" in a for a in actions)

    h.commit_ok, h.push_ok = True, False
    actions = _run(h, ["tests/test_a.py"])
    assert any("Failed to push" in a for a in actions) and not any("Pushed fixes" in a for a in actions)


def test_disabled_automatic_fix_starts_nothing(harness):
    h = harness
    actions = _run(h, ["tests/test_a.py"], automatic_test_fix=False)
    # focused checks stay observational; no correction starts and nothing is published
    assert h.prompts == [] and h.invocations() == ["tests/test_a.py"] and h.commits == 0 and h.pushes == 0
    assert any("Automatic test fix is disabled" in a for a in actions)


def test_delivered_prompts_defer_full_validation_and_carry_evidence(harness):
    h = harness
    h.set_state("test_a.py", False)
    h.follow_ups = [lambda: h.set_state("test_a.py", True)]
    _run(h, ["tests/test_a.py"])
    assert len(h.prompts) == 2
    for prompt in h.prompts:
        assert "Full-suite validation is performed by CI" in prompt
        assert "Do NOT run the full test suite" in prompt
        assert "tests/test_a.py" in prompt
        assert "unavailable" in prompt
        assert "Ensure all tests pass" not in prompt
    assert "build step failed: lint" in h.prompts[0]


def test_classification_and_helpers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "t.py").write_text("")
    ok = {"success": True, "output": "1 passed", "errors": "", "return_code": 0, "command": "bash s t.py", "test_file": "t.py"}
    assert classify_target_result("t.py", ok).status is TargetStatus.PASSED
    assert classify_target_result("t.py", {**ok, "output": "collected 0 items"}).status is TargetStatus.UNVERIFIED
    assert classify_target_result("t.py", {**ok, "command": "none", "success": False}).status is TargetStatus.UNVERIFIED
    assert classify_target_result("t.py", {**ok, "test_file": None}).status is TargetStatus.UNVERIFIED
    assert classify_target_result("t.py", {**ok, "success": False, "return_code": -1}).status is TargetStatus.UNVERIFIED
    assert classify_target_result("t.py", {**ok, "success": False, "return_code": 127}).status is TargetStatus.UNVERIFIED
    assert classify_target_result("t.py", {**ok, "success": False, "return_code": 1}).status is TargetStatus.FAILED
    assert dedupe_targets(["a", " a ", "b", "", "a"]) == ["a", "b"]
    assert follow_up_budget_exhausted(2, 2) and not follow_up_budget_exhausted(2, 1)
    assert not follow_up_budget_exhausted(float("inf"), 10**6)


def test_prompt_templates_render_without_targets():
    rendered = render_prompt("pr.github_actions_fix", pr_number=1, repo_name="o/r", pr_title="t", extracted_errors="E", commit_log="", linked_issues_context="", focused_targets="")
    assert "Failed test files reported by CI" not in rendered
    assert "Full-suite validation is performed by CI" in rendered


@pytest.mark.parametrize("already_on_pr_branch", [True, False])
def test_handle_pr_merge_origin_reaches_every_target_and_initial_correction(harness, already_on_pr_branch):
    """Production entry: both initial and already-on-branch processing keep CI evidence and designation."""
    from contextlib import nullcontext
    from unittest.mock import MagicMock

    from src.auto_coder.ci_repair_authority import CIRepairAuthority
    from src.auto_coder.util.github_action import DetailedChecksResult, GitHubActionsStatusResult

    h = harness
    for name in ("a", "b", "c", "d"):
        h.set_state(f"test_{name}.py", name != "c")
    h.initial_edit = lambda: (h.repo / "fix.txt").write_text("edit")
    h.follow_ups = [lambda: h.set_state("test_c.py", True)]
    real_run = pr_processor.cmd.run_command
    branch = "feature/x"

    def run_command(cmd_list, *a, **k):
        if "branch" in cmd_list and "--show-current" in cmd_list:
            return SimpleNamespace(success=True, stdout=(branch if already_on_pr_branch else "main") + "\n", stderr="", returncode=0)
        return real_run(cmd_list, *a, **k)

    files = ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py", "tests/test_d.py", "tests/test_a.py"]
    pr = {"number": 7, "title": "t", "body": "<!-- auto-coder:local-llm -->", "head": {"ref": branch, "sha": "h1"}, "base": {"ref": "main"}}
    client = MagicMock()
    client.get_pr_review_threads_strict.return_value = []
    authority = CIRepairAuthority(True, "current exact-head CI failure", "h1")
    config = AutomationConfig()
    with (
        patch.object(pr_processor.cmd, "run_command", run_command),
        patch.object(pr_processor, "current_ci_failure_authority", return_value=nullcontext(authority)),
        patch.object(pr_processor, "check_github_actions_and_exit_if_in_progress", return_value=True),
        patch.object(pr_processor, "_get_mergeable_state", return_value={"mergeable": True}),
        patch.object(pr_processor, "_is_jules_pr", return_value=False),
        patch.object(pr_processor, "_is_local_llm_pr", return_value=True),
        patch.object(pr_processor, "_check_github_actions_status", return_value=MagicMock(success=False, error=None, ids=[1])),
        patch.object(pr_processor, "get_detailed_checks_from_history", return_value=MagicMock(spec=DetailedChecksResult, success=False, failed_checks=[{"id": 1, "name": "t", "conclusion": "failure"}])),
        patch.object(pr_processor, "_checkout_pr_branch", return_value=True),
        patch.object(pr_processor, "BranchManager"),
        patch.object(pr_processor, "_create_github_action_log_summary", return_value=("FAILED tests/test_c.py::test_it\nlint failed", files)),
    ):
        actions = pr_processor._handle_pr_merge(client, "o/r", pr, config, {})

    assert h.designated == [True, True]
    assert "lint failed" in h.prompts[0]
    assert h.invocations()[:4] == ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py", "tests/test_d.py"]
    assert len(h.invocations()) == 8 and "UNSCOPED" not in h.invocations() and not h.sentinel.exists()
    assert (h.commits, h.pushes) == (1, 1)
    assert any("passed locally on the latest corrected state" in a for a in actions)


def test_production_extraction_selects_every_reported_file_including_missing(harness):
    """Two pytest FAILED summaries plus a missing file, without a preassembled target list."""
    h = harness
    h.set_state("test_a.py", True)
    h.set_state("test_b.py", False)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "baseline"], cwd=h.repo, check=True)
    h.follow_ups = [lambda: h.set_state("test_b.py", True)]
    logs = "FAILED tests/test_a.py::test_x - AssertionError\nFAILED tests/test_b.py::test_y - AssertionError\nFAILED tests/test_gone.py::test_z - AssertionError"
    actions = pr_processor._fix_pr_issues_with_testing("o/r", {"number": 7, "title": "t", "body": ""}, AutomationConfig(), logs, None)
    assert h.invocations()[:2] == ["tests/test_a.py", "tests/test_b.py"]
    assert "Focused check unverified: tests/test_gone.py (target file not found in the working tree)" in actions
    assert not any("passed locally" in a for a in actions)
    assert not h.sentinel.exists()


def test_container_launch_failure_is_unverified_and_starts_no_correction(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "t.py").write_text("")
    raw = {
        "success": False,
        "output": "",
        "errors": "Error response from daemon: No such container: app",
        "return_code": 1,
        "command": "docker exec app bash scripts/test.sh t.py",
        "test_file": "t.py",
    }
    result = classify_target_result("t.py", raw)
    assert result.status is TargetStatus.UNVERIFIED and "container" in result.reason


def test_summary_builder_keeps_every_failed_job_and_file_identity():
    """Real summary builder with GitHub retrieval controlled: a later job's distinct error survives bounding."""
    from src.auto_coder.util import github_action

    big = "FAILED tests/test_first.py::test_a - AssertionError\n" + ("noise line\n" * 8000)
    second = "=== Job: lint ===\nFAILED tests/test_second.py::test_b - AssertionError\nruff: E501 unique-lint-failure"
    jobs = [{"name": "tests", "conclusion": "failure", "html_url": "u1"}, {"name": "lint", "conclusion": "failure", "html_url": "u2"}]
    by_url = {"u1": "=== Job: tests ===\n" + big, "u2": second}
    checks = [{"name": "tests", "details_url": "https://github.com/o/r/actions/runs/1/job/1"}]
    with (
        patch.object(github_action.GitHubClient, "get_instance", return_value=SimpleNamespace(token="t")),
        patch.object(github_action, "get_ghapi_client", return_value=object()),
        patch.object(github_action, "filter_actionable_github_checks", side_effect=lambda repo, c: c),
        patch.object(github_action, "_get_playwright_artifact_logs", return_value=(None, [])),
        patch.object(github_action, "list_all_workflow_jobs", return_value=jobs),
        patch.object(github_action, "_sort_jobs_by_workflow", side_effect=lambda j, *a, **k: j),
        patch.object(github_action, "get_github_actions_logs_from_url", side_effect=lambda url: by_url[url]),
    ):
        summary, files = github_action._create_github_action_log_summary("o/r", AutomationConfig(), checks)
    assert "unique-lint-failure" in summary
    assert "tests/test_first.py" in summary
    assert files == ["tests/test_first.py", "tests/test_second.py"]
    assert len(summary) < 60000
