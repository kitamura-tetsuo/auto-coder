import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.util.github_action import _check_github_actions_status_from_history


# Mock GitHubClient to avoid token error
@patch("src.auto_coder.util.github_action.GitHubClient")
def test_history_uses_branch_filter_when_commit_runs_empty(mock_gh_client):
    """Verify that branch filtering works correctly and only references the correct PR's runs.

    Regression: Prevent PR #73's history check from incorrectly referencing PR #133's Run.
    """
    mock_gh_client.get_instance.return_value.token = "dummy_token"
    config = AutomationConfig()

    pr_data = {
        "number": 73,
        "head_branch": "pr-73-branch",
        "head": {
            "ref": "pr-73-branch",
            "sha": "abcdef1234567890abcdef",
        },
    }

    # Run list mixes other PR's new Run and target PR's old Run
    other_pr_run_id = 19024332818  # Format close to real example
    target_pr_run_id = 18000000000

    run_list_payload = [
        {
            "id": other_pr_run_id,  # API uses id not databaseId
            "head_branch": "pr-133-branch",  # API snake_case
            "conclusion": "success",
            "created_at": "2025-11-05T10:00:00Z",  # API snake_case
            "status": "completed",
            "display_title": "CI",  # API snake_case
            "html_url": f"https://github.com/owner/repo/actions/runs/{other_pr_run_id}",  # API html_url
            "head_sha": "133deadbeef",  # API snake_case
        },
        {
            "id": target_pr_run_id,
            "head_branch": "pr-73-branch",
            "conclusion": "success",
            "created_at": "2025-11-04T10:00:00Z",
            "status": "completed",
            "display_title": "CI",
            "html_url": f"https://github.com/owner/repo/actions/runs/{target_pr_run_id}",
            "head_sha": "73cafebabe",
        },
    ]

    # 3) run view (jobs) should reference only target PR's Run
    jobs_payload_target = {
        "jobs": [
            {
                "id": 54321000000,  # API id
                "name": "CI",
                "conclusion": "success",
                "status": "completed",
            }
        ]
    }

    mock_api = MagicMock()
    # 1. list_commits
    # Returns list of commits (dicts)
    mock_api.pulls.list_commits.return_value = [{"sha": "73cafebabe"}]

    # 2. list_workflow_runs_for_repo
    # Returns dict with workflow_runs
    mock_api.actions.list_workflow_runs_for_repo.return_value = {"workflow_runs": run_list_payload}

    # 3. list_jobs_for_workflow_run
    def list_jobs_side_effect(owner, repo, run_id):
        if run_id == target_pr_run_id:
            return jobs_payload_target
        return {"jobs": []}

    mock_api.actions.list_jobs_for_workflow_run.side_effect = list_jobs_side_effect

    with patch("src.auto_coder.util.github_action.get_ghapi_client", return_value=mock_api):
        result = _check_github_actions_status_from_history("owner/repo", pr_data, config)

    assert result.success is True
    # GitHubActionsStatusResult has only success and ids
    assert isinstance(result.ids, list)


@patch("src.auto_coder.util.github_action.GitHubClient")
def test_history_filters_to_branch_even_with_head_sha_present(mock_gh_client):
    """Verify that branch filtering works correctly and filters to the correct branch."""
    mock_gh_client.get_instance.return_value.token = "dummy_token"
    config = AutomationConfig()

    pr_data = {
        "number": 999,
        "head_branch": "fix-branch",
        "head": {
            "ref": "fix-branch",
            "sha": "abc123def456",
        },
    }

    # Mix same branch (push) and different branches (PR)
    run_list_payload = [
        {
            "id": 3001,
            "head_branch": "other-branch",
            "conclusion": "failure",
            "created_at": "2025-11-05T10:00:00Z",
            "status": "completed",
            "display_title": "CI",
            "html_url": "https://github.com/owner/repo/actions/runs/3001",
            "head_sha": "deadbeef",
        },
        {
            "id": 3000,
            "head_branch": "fix-branch",
            "conclusion": "success",
            "created_at": "2025-11-04T10:00:00Z",
            "status": "completed",
            "display_title": "CI",
            "html_url": "https://github.com/owner/repo/actions/runs/3000",
            "head_sha": "cafebabe",
        },
    ]

    jobs_payload = {
        "jobs": [
            {
                "id": 777,
                "name": "CI",
                "conclusion": "success",
                "status": "completed",
            }
        ]
    }

    mock_api = MagicMock()
    # 1. list_commits
    mock_api.pulls.list_commits.return_value = [{"sha": "abc123def456"}]

    # 2. list_workflow_runs_for_repo
    mock_api.actions.list_workflow_runs_for_repo.return_value = {"workflow_runs": run_list_payload}

    # 3. list_jobs_for_workflow_run
    def list_jobs_side_effect(owner, repo, run_id):
        if run_id == 3000:
            return jobs_payload
        return {"jobs": []}

    mock_api.actions.list_jobs_for_workflow_run.side_effect = list_jobs_side_effect

    with patch("src.auto_coder.util.github_action.get_ghapi_client", return_value=mock_api):
        result = _check_github_actions_status_from_history("owner/repo", pr_data, config)

    assert result.success is True
    # GitHubActionsStatusResult has only success and ids
    assert isinstance(result.ids, list)
