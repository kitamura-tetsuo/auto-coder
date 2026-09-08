import json
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.util.github_action import (
    _check_github_actions_status,
    is_verified_prompt_regression_advisory_comment,
)

REPO = "owner/repo"
HEAD = "a" * 40


def workflow_run(run_id: int, path: str, conclusion: str = "failure", status: str = "completed") -> dict[str, object]:
    return {
        "id": run_id,
        "workflow_id": 100,
        "name": "changed display name",
        "path": path,
        "head_sha": HEAD,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
        "repository": {"full_name": REPO},
        "pull_requests": [{"number": 17}],
        "run_attempt": 2,
    }


@pytest.mark.parametrize(
    ("status", "conclusion"),
    [
        ("completed", "success"),
        ("completed", "failure"),
        ("completed", "timed_out"),
        ("completed", "cancelled"),
        ("completed", "skipped"),
        ("queued", None),
        ("in_progress", None),
        ("waiting", None),
        ("completed", None),
    ],
)
@patch("auto_coder.util.github_action.GitHubClient")
@patch("auto_coder.util.github_action.get_ghapi_client")
def test_real_aggregation_ignores_every_verified_advisory_state(
    get_api: MagicMock,
    github_client: MagicMock,
    status: str,
    conclusion: str | None,
) -> None:
    github_client.get_instance.return_value.token = "token"
    api = get_api.return_value
    ordinary = workflow_run(10, ".github/workflows/pr-tests.yml", "success")
    advisory = workflow_run(20, ".github/workflows/prompt-regression.yml", conclusion, status)
    api.checks.list_for_ref.return_value = {"check_runs": []}
    api.actions.list_workflow_runs_for_repo.return_value = {"workflow_runs": [ordinary, advisory]}
    api.actions.get_workflow_run.return_value = ordinary

    result = _check_github_actions_status(REPO, {"number": 17, "head": {"sha": HEAD}}, AutomationConfig())

    assert result.success is True
    assert result.in_progress is False
    assert result.ids == [10]
    api.actions.review_pending_deployments_for_run.assert_not_called()


@patch("auto_coder.util.github_action.GitHubClient")
@patch("auto_coder.util.github_action.get_ghapi_client")
def test_provenance_not_display_name_controls_isolation(get_api: MagicMock, github_client: MagicMock) -> None:
    github_client.get_instance.return_value.token = "token"
    api = get_api.return_value
    colliding_ordinary = workflow_run(30, ".github/workflows/pr-tests.yml", "failure")
    colliding_ordinary["name"] = "Selective Promptfoo Evaluations"
    advisory = workflow_run(31, ".github/workflows/prompt-regression.yml", "failure")
    advisory["name"] = "Renamed semantic evaluation"
    api.checks.list_for_ref.return_value = {"check_runs": []}
    api.actions.list_workflow_runs_for_repo.return_value = {"workflow_runs": [colliding_ordinary, advisory]}
    api.actions.get_workflow_run.return_value = colliding_ordinary

    result = _check_github_actions_status(REPO, {"number": 17, "head": {"sha": HEAD}}, AutomationConfig())

    assert result.success is False
    assert result.ids == [30]


@patch("auto_coder.util.github_action.GitHubClient")
@patch("auto_coder.util.github_action.get_ghapi_client")
def test_generated_report_requires_exact_bot_and_github_identity(get_api: MagicMock, github_client: MagicMock) -> None:
    github_client.get_instance.return_value.token = "token"
    run = workflow_run(20, ".github/workflows/prompt-regression-report.yml", "failure")
    get_api.return_value.actions.get_workflow_run.return_value = run
    identity = {
        "repository": REPO,
        "pr_number": 17,
        "head_sha": HEAD,
        "run_id": 20,
        "run_attempt": 2,
        "workflow_path": ".github/workflows/prompt-regression-report.yml",
    }
    body = "<!-- auto-coder:prompt-regression-advisory:v1 -->\nAdvisory-Identity: " + json.dumps(identity)
    comment = {"user": {"login": "github-actions[bot]"}, "body": body}

    assert is_verified_prompt_regression_advisory_comment(REPO, 17, HEAD, comment) is True
    assert is_verified_prompt_regression_advisory_comment(REPO, 17, HEAD, {**comment, "user": {"login": "human"}}) is False
    foreign = dict(identity, repository="foreign/repo")
    assert (
        is_verified_prompt_regression_advisory_comment(
            REPO,
            17,
            HEAD,
            {**comment, "body": body.splitlines()[0] + "\nAdvisory-Identity: " + json.dumps(foreign)},
        )
        is False
    )
