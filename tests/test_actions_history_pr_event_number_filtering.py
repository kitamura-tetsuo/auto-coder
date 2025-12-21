import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.util.github_action import _check_github_actions_status_from_history


@patch("src.auto_coder.util.github_action.GitHubClient")
def test_history_prefers_pull_request_event_runs(mock_gh_client):
    """When event field exists, prefer pull_request runs. Also, only adopt runs where run view's pullRequests include the target PR."""
    mock_gh_client.get_instance.return_value.token = "dummy_token"
    config = AutomationConfig()

    pr_number = 321
    pr_data = {
        "number": pr_number,
        "head_branch": "feature-branch",
        "head": {
            "ref": "feature-branch",
            "sha": "abc123",
        },
    }

    # run list: new push run and old pull_request run (same branch)
    push_run_id = 4400
    pr_run_id = 4300
    run_list_payload = [
        {
            "id": push_run_id,
            "head_branch": "feature-branch",
            "conclusion": "success",
            "created_at": "2025-11-06T10:00:00Z",
            "status": "completed",
            "display_title": "CI",
            "html_url": f"https://github.com/owner/repo/actions/runs/{push_run_id}",
            "head_sha": "abc123",
            "event": "push",
        },
        {
            "id": pr_run_id,
            "head_branch": "feature-branch",
            "conclusion": "success",
            "created_at": "2025-11-05T10:00:00Z",
            "status": "completed",
            "display_title": "CI",
            "html_url": f"https://github.com/owner/repo/actions/runs/{pr_run_id}",
            "head_sha": "abc123",
            "event": "pull_request",
        },
    ]

    # run view response: push does not include target PR / pull_request includes it
    jobs_payload = {
        "jobs": [
            {
                "id": 999,
                "name": "CI",
                "conclusion": "success",
                "status": "completed",
            }
        ]
    }

    mock_api = MagicMock()
    # 1. list_commits
    mock_api.pulls.list_commits.return_value = [{"sha": "abc123"}]

    # 2. list_workflow_runs_for_repo
    mock_api.actions.list_workflow_runs_for_repo.return_value = {"workflow_runs": run_list_payload}

    # 3. get_workflow_run (to check pull_requests)
    def get_run_side_effect(owner, repo, run_id):
        if run_id == pr_run_id:
            return {"pull_requests": [{"number": pr_number}]}
        if run_id == push_run_id:
            return {"pull_requests": []}
        return {}

    mock_api.actions.get_workflow_run.side_effect = get_run_side_effect

    # 4. list_jobs_for_workflow_run
    mock_api.actions.list_jobs_for_workflow_run.return_value = jobs_payload

    with patch("src.auto_coder.util.github_action.get_ghapi_client", return_value=mock_api):
        result = _check_github_actions_status_from_history("owner/repo", pr_data, config)

    assert result.success is True
    assert pr_run_id in result.ids


@patch("src.auto_coder.util.github_action.GitHubClient")
def test_history_limits_to_runs_referencing_target_pr(mock_gh_client):
    """Skip runs that don't include target PR number in pullRequests, only adopt runs that do."""
    mock_gh_client.get_instance.return_value.token = "dummy_token"
    config = AutomationConfig()

    pr_number = 555
    pr_data = {
        "number": pr_number,
        "head_branch": "topic-branch",
        "head": {
            "ref": "topic-branch",
            "sha": "def456",
        },
    }

    run_a = 5100  # New but doesn't include target PR
    run_b = 5000  # Old but includes target PR
    run_list_payload = [
        {
            "id": run_a,
            "head_branch": "topic-branch",
            "conclusion": "success",
            "created_at": "2025-11-06T15:00:00Z",
            "status": "completed",
            "display_title": "CI",
            "html_url": f"https://github.com/owner/repo/actions/runs/{run_a}",
            "head_sha": "def456",
            "event": "pull_request",
        },
        {
            "id": run_b,
            "head_branch": "topic-branch",
            "conclusion": "success",
            "created_at": "2025-11-05T15:00:00Z",
            "status": "completed",
            "display_title": "CI",
            "html_url": f"https://github.com/owner/repo/actions/runs/{run_b}",
            "head_sha": "def456",
            "event": "pull_request",
        },
    ]

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
    mock_api.pulls.list_commits.return_value = [{"sha": "def456"}]

    # 2. list_workflow_runs_for_repo
    mock_api.actions.list_workflow_runs_for_repo.return_value = {"workflow_runs": run_list_payload}

    # 3. get_workflow_run (to check pull_requests)
    def get_run_side_effect(owner, repo, run_id):
        if run_id == run_a:
            return {"pull_requests": [{"number": 999}]}
        if run_id == run_b:
            return {"pull_requests": [{"number": pr_number}]}
        return {}

    mock_api.actions.get_workflow_run.side_effect = get_run_side_effect

    # 4. list_jobs_for_workflow_run
    mock_api.actions.list_jobs_for_workflow_run.return_value = jobs_payload

    with patch("src.auto_coder.util.github_action.get_ghapi_client", return_value=mock_api):
        result = _check_github_actions_status_from_history("owner/repo", pr_data, config)

    assert result.success is True
    assert run_b in result.ids
