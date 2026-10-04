"""Regression coverage for CI failures beyond the first REST jobs page."""

from unittest.mock import Mock, call, patch

import pytest

from auto_coder.util.gh_cache import list_all_workflow_jobs
from auto_coder.util.github_action import GitHubActionsStatusResult, get_detailed_checks_from_history, get_github_actions_logs_from_url


def _jobs(count):
    return [{"id": index + 1, "name": f"job-{index}", "status": "completed", "conclusion": "success"} for index in range(count)]


@pytest.mark.parametrize("count", [37, 101, 200])
def test_details_find_failure_after_default_first_page(count):
    jobs = _jobs(count)
    jobs[-1]["name"] = "e2e-test / e2e-test (tables)"
    jobs[-1]["conclusion"] = "failure"
    api = Mock()
    api.actions.get_workflow_run.return_value = {"path": ".github/workflows/ci.yml"}
    api.actions.list_jobs_for_workflow_run.side_effect = [{"total_count": count, "jobs": jobs[start : start + 100]} for start in range(0, count, 100)]
    with patch("auto_coder.util.github_action.GitHubClient"), patch("auto_coder.util.github_action.get_ghapi_client", return_value=api):
        result = get_detailed_checks_from_history(GitHubActionsStatusResult(success=False, ids=[5467]), "owner/repo")

    assert result.success is False
    assert result.total_checks == count
    assert result.has_in_progress is False
    assert result.run_ids == [5467]
    assert result.failed_checks == [
        {
            "name": "e2e-test / e2e-test (tables) (run 5467)",
            "job_name": "e2e-test / e2e-test (tables)",
            "conclusion": "failure",
            "details_url": f"https://github.com/owner/repo/actions/runs/5467/job/{count}",
            "run_id": 5467,
            "job_id": count,
            "status": "completed",
            "completed_at": None,
            "started_at": None,
        }
    ]
    assert api.actions.list_jobs_for_workflow_run.call_args_list == [call("owner", "repo", 5467, per_page=100, page=page) for page in range(1, (count + 99) // 100 + 1)]


def test_run_log_lookup_fetches_failed_job_on_second_page():
    jobs = _jobs(101)
    jobs[-1]["conclusion"] = "failure"
    api = Mock()
    api.actions.list_jobs_for_workflow_run.side_effect = [{"total_count": 101, "jobs": jobs[:100]}, {"total_count": 101, "jobs": jobs[100:]}]
    with patch("auto_coder.util.github_action.GitHubClient"), patch("auto_coder.util.github_action.get_ghapi_client", return_value=api), patch("auto_coder.util.github_action.get_github_actions_logs_from_url", return_value="AssertionError: exact width") as job_logs:
        result = get_github_actions_logs_from_url("https://github.com/owner/repo/actions/runs/5467")
    assert result == "AssertionError: exact width"
    job_logs.assert_called_once_with("https://github.com/owner/repo/actions/runs/5467/job/101")


@pytest.mark.parametrize("second_page", [RuntimeError("page unavailable"), {"total_count": 101, "jobs": []}])
def test_partial_job_listing_is_never_returned(second_page):
    api = Mock()
    api.actions.list_jobs_for_workflow_run.side_effect = [{"total_count": 101, "jobs": _jobs(100)}, second_page]
    with pytest.raises(RuntimeError):
        list_all_workflow_jobs(api, "owner", "repo", 5467)
    assert api.actions.list_jobs_for_workflow_run.call_count == 2


def test_missing_total_count_reads_until_short_page():
    api = Mock()
    jobs = _jobs(101)
    api.actions.list_jobs_for_workflow_run.side_effect = [{"jobs": jobs[:100]}, {"jobs": jobs[100:]}]
    assert list_all_workflow_jobs(api, "owner", "repo", 5467) == jobs
    assert api.actions.list_jobs_for_workflow_run.call_count == 2


def test_advisory_workflow_never_reads_job_pages():
    api = Mock()
    api.actions.get_workflow_run.return_value = {"path": ".github/workflows/prompt-regression.yml"}
    with patch("auto_coder.util.github_action.GitHubClient"), patch("auto_coder.util.github_action.get_ghapi_client", return_value=api):
        result = get_detailed_checks_from_history(GitHubActionsStatusResult(success=False, ids=[5467]), "owner/repo")
    assert result.failed_checks == []
    assert result.run_ids == []
    api.actions.list_jobs_for_workflow_run.assert_not_called()


def test_local_pr_repair_receives_failure_beyond_first_page(tmp_path, monkeypatch):
    """Exercise production CI routing with the real paginated details reader."""
    from contextlib import ExitStack, nullcontext

    from auto_coder.automation_config import AutomationConfig
    from auto_coder.ci_repair_authority import CIRepairAuthority
    from auto_coder.pr_processor import _handle_pr_merge

    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    jobs = _jobs(101)
    jobs[-1]["name"] = "e2e-test / e2e-test (tables)"
    jobs[-1]["conclusion"] = "failure"
    api = Mock()
    api.actions.get_workflow_run.return_value = {"path": ".github/workflows/ci.yml"}
    api.actions.list_jobs_for_workflow_run.side_effect = [{"total_count": 101, "jobs": jobs[:100]}, {"total_count": 101, "jobs": jobs[100:]}]
    client = Mock()
    config = AutomationConfig()
    config.SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL = True
    pr = {"number": 5467, "head": {"ref": "issue-5457", "sha": "a" * 40}, "base": {"ref": "main"}, "body": "<!-- auto-coder:local-llm -->"}
    authority = CIRepairAuthority(True, "current exact-head CI failure", "a" * 40)
    with ExitStack() as stack:
        stack.enter_context(patch("auto_coder.util.github_action.GitHubClient"))
        stack.enter_context(patch("auto_coder.util.github_action.get_ghapi_client", return_value=api))
        for name, value in {
            "_is_pr_review_thread_gate_enabled": False,
            "_apply_review_adjudication_effects": ([], False),
            "check_github_actions_and_exit_if_in_progress": True,
            "_get_mergeable_state": {"mergeable": True},
            "_check_github_actions_status": GitHubActionsStatusResult(success=False, ids=[5467]),
            "check_pr_repair_exhaustion": None,
            "_checkout_pr_branch": True,
            "current_ci_failure_authority": nullcontext(authority),
        }.items():
            stack.enter_context(patch(f"auto_coder.pr_processor.{name}", return_value=value))
        stack.enter_context(patch("auto_coder.pr_processor.cmd.run_command", return_value=Mock(success=True, stdout="main\n")))
        stack.enter_context(patch("auto_coder.pr_processor.BranchManager"))
        summary = stack.enter_context(patch("auto_coder.pr_processor._create_github_action_log_summary", return_value=("Exact width failed", ["grid-widths.spec.ts"])))
        repair = stack.enter_context(patch("auto_coder.pr_processor._fix_pr_issues_with_testing", return_value=["Repair invoked"]))
        actions = _handle_pr_merge(client, "owner/repo", pr, config, {})

    assert "GitHub Actions checks failed for PR #5467: 1 failed" in actions
    assert "Repair invoked" in actions
    assert not any("No specific failed checks" in action for action in actions)
    assert summary.call_args.args[2][0]["job_id"] == 101
    repair.assert_called_once_with("owner/repo", pr, config, "Exact width failed", ["grid-widths.spec.ts"], skip_github_actions_fix=False)
    client.merge_pr.assert_not_called()
