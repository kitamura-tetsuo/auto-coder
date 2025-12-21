import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.util.github_action import _check_github_actions_status_from_history


def _cmd_result(success: bool = True, stdout: str = "", stderr: str = "", returncode: int = 0):
    return SimpleNamespace(success=success, stdout=stdout, stderr=stderr, returncode=returncode)


@patch("src.auto_coder.util.github_action.GitHubClient")
def test_commit_search_prefers_pull_request_runs_without_event_flag(mock_gh_client):
    """Commit search filters on Python side, and can prioritize pull_request runs even without --event/--commit flag."""
    mock_gh_client.get_instance.return_value.token = "dummy_token"
    config = AutomationConfig()

    pr_data = {
        "number": 101,
        "head": {"ref": "feat-commit-event", "sha": "feedc0ffee"},
    }

    run_id = 777001
    commit_runs_payload = [
        {
            "id": run_id,
            "head_branch": "feat-commit-event",
            "conclusion": "failure",
            "created_at": "2025-11-05T10:00:00Z",
            "status": "completed",
            "display_title": "CI",
            "html_url": f"https://github.com/owner/repo/actions/runs/{run_id}",
            "head_sha": "feedc0ffee",
            "event": "pull_request",
        }
    ]

    mock_api = MagicMock()
    # 1. list_commits
    mock_api.pulls.list_commits.return_value = [{"sha": "feedc0ffee"}]

    # 2. list_workflow_runs_for_repo
    mock_api.actions.list_workflow_runs_for_repo.return_value = {"workflow_runs": commit_runs_payload}

    jobs_payload = {
        "jobs": [
            {
                "id": 1,
                "name": "CI",
                "conclusion": "failure",
                "status": "completed",
            }
        ]
    }
    mock_api.actions.list_jobs_for_workflow_run.return_value = jobs_payload
    # get_workflow_run - assume no PR refs logic needed (or mock it blank)
    mock_api.actions.get_workflow_run.return_value = {"id": run_id, "pull_requests": []}  # Or simply return {} if not checking pr refs

    with patch("src.auto_coder.util.github_action.get_ghapi_client", return_value=mock_api):
        result = _check_github_actions_status_from_history("owner/repo", pr_data, config)

    # Check that we called list_workflow_runs_for_repo with just branch, no event argument
    mock_api.actions.list_workflow_runs_for_repo.assert_called()
    # verify kwargs
    call_args = mock_api.actions.list_workflow_runs_for_repo.call_args
    # call_args.kwargs should contain 'branch', possibly 'per_page', but NOT 'event'
    assert "event" not in call_args.kwargs
    assert call_args.kwargs.get("branch") == "feat-commit-event"

    assert result.success is False
    assert result.ids == [777001], f"expected [777001] but got {result.ids}"
    # failed_checks is not available in GitHubActionsStatusResult


@patch("src.auto_coder.util.github_action.GitHubClient")
def test_fallback_search_works_without_event_flag(mock_gh_client):
    """Even if commit-equivalent search doesn't hit, can prioritize pull_request in fallback run list without --event."""
    mock_gh_client.get_instance.return_value.token = "dummy_token"
    config = AutomationConfig()

    pr_data = {
        "number": 102,
        "head": {"ref": "feat-fallback-event", "sha": "abc123"},
    }

    run_id = 777002
    run_list_payload = [
        {
            "id": run_id,
            "head_branch": "feat-fallback-event",
            "conclusion": "success",
            "created_at": "2025-11-05T10:00:00Z",
            "status": "completed",
            "display_title": "CI",
            "html_url": f"https://github.com/owner/repo/actions/runs/{run_id}",
            "head_sha": "abc123",
            "event": "pull_request",
        }
    ]

    mock_api = MagicMock()
    # 1. list_commits
    mock_api.pulls.list_commits.return_value = [{"sha": "abc123"}]

    # 2. list_workflow_runs_for_repo
    # The original test simulated first call empty, second call full.
    # Our new implementation calls only once per branch.
    # So we simply return the runs.
    mock_api.actions.list_workflow_runs_for_repo.return_value = {"workflow_runs": run_list_payload}

    jobs_payload = {
        "jobs": [
            {
                "id": 2,
                "name": "CI",
                "conclusion": "success",
                "status": "completed",
            }
        ]
    }
    mock_api.actions.list_jobs_for_workflow_run.return_value = jobs_payload
    mock_api.actions.get_workflow_run.return_value = {"id": run_id, "pull_requests": []}

    with patch("src.auto_coder.util.github_action.get_ghapi_client", return_value=mock_api):
        result = _check_github_actions_status_from_history("owner/repo", pr_data, config)

    # Check argument
    call_args = mock_api.actions.list_workflow_runs_for_repo.call_args
    assert "event" not in call_args.kwargs
    assert call_args.kwargs.get("branch") == "feat-fallback-event"

    assert result.success is True
    assert result.ids == [777002], f"expected [777002] but got {result.ids}"
    # checks and failed_checks are not available in GitHubActionsStatusResult
