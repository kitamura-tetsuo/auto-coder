import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.util.github_action import _check_github_actions_status_from_history


@patch("src.auto_coder.util.github_action.GitHubClient")
def test_history_handles_missing_pullRequests_field_gracefully(mock_gh_client):
    """run view's pullRequests field can be evaluated from job information even if missing."""
    mock_gh_client.get_instance.return_value.token = "dummy_token"
    config = AutomationConfig()

    pr_data = {
        "number": 42,
        "head": {
            "ref": "feature-branch",
            "sha": "abc123def456",
        },
    }

    # 2) Normal run list finds 1 PR event run for target branch
    run_id = 424242
    run_list_payload = [
        {
            "id": run_id,
            "head_branch": "feature-branch",
            "conclusion": "success",
            "created_at": "2025-11-05T10:00:00Z",
            "status": "completed",
            "display_title": "CI",
            "html_url": f"https://github.com/owner/repo/actions/runs/{run_id}",
            "head_sha": "abc123def456",
            "event": "pull_request",
        }
    ]

    # 3) run view does not return pullRequests field (missing)
    jobs_payload = {
        "jobs": [
            {
                "id": 1001,
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

    # 3. get_workflow_run (returns run details, missing pull_requests)
    mock_api.actions.get_workflow_run.return_value = {"id": run_id}

    # 4. list_jobs_for_workflow_run
    mock_api.actions.list_jobs_for_workflow_run.return_value = jobs_payload

    with patch("src.auto_coder.util.github_action.get_ghapi_client", return_value=mock_api):
        result = _check_github_actions_status_from_history("owner/repo", pr_data, config)

    assert result.success is True
    assert run_id in result.ids


@patch("src.auto_coder.util.github_action.GitHubClient")
def test_history_handles_empty_pullRequests_list_gracefully(mock_gh_client):
    """run view can be evaluated from job information even if it returns pullRequests: []."""
    mock_gh_client.get_instance.return_value.token = "dummy_token"
    config = AutomationConfig()

    pr_data = {
        "number": 43,
        "head": {
            "ref": "topic-branch",
            "sha": "cafebabefeed",
        },
    }

    run_id = 434343
    run_list_payload = [
        {
            "id": run_id,
            "head_branch": "topic-branch",
            "conclusion": "success",
            "created_at": "2025-11-05T10:00:00Z",
            "status": "completed",
            "display_title": "CI",
            "html_url": f"https://github.com/owner/repo/actions/runs/{run_id}",
            "head_sha": "cafebabefeed",
            "event": "pull_request",
        }
    ]

    # pullRequests is empty array
    jobs_payload = {
        "jobs": [
            {
                "id": 2002,
                "name": "CI",
                "conclusion": "success",
                "status": "completed",
            }
        ],
    }

    mock_api = MagicMock()
    # 1. list_commits
    mock_api.pulls.list_commits.return_value = [{"sha": "cafebabefeed"}]

    # 2. list_workflow_runs_for_repo
    mock_api.actions.list_workflow_runs_for_repo.return_value = {"workflow_runs": run_list_payload}

    # 3. get_workflow_run (returns empty pull_requests)
    mock_api.actions.get_workflow_run.return_value = {"id": run_id, "pull_requests": []}

    # 4. list_jobs_for_workflow_run
    mock_api.actions.list_jobs_for_workflow_run.return_value = jobs_payload

    with patch("src.auto_coder.util.github_action.get_ghapi_client", return_value=mock_api):
        result = _check_github_actions_status_from_history("owner/repo", pr_data, config)

    assert result.success is True
    assert run_id in result.ids
