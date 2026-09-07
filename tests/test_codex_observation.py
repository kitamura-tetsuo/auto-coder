"""Production-boundary regressions for authoritative Codex observations."""

from pathlib import Path
from unittest.mock import Mock, patch

from auto_coder.cloud_run import CloudRun, CloudRunRepository
from auto_coder.cloud_task_client_base import CloudTaskState
from auto_coder.codex_observation import CodexObservationService, PullRequestPresence
from auto_coder.codex_wham_client import CodexWhamClient

TASK = "task_e_6a26c19ac8a88326af83ebfb44b89fe2"


def run_record() -> CloudRun:
    return CloudRun("owner/repo", 1863, 2, "codex-cloud", TASK, "codex-prod", "env-prod", "main", task_url=f"https://chatgpt.com/codex/tasks/{TASK}")


class GitHubReads:
    def __init__(self, prs=None, timeline=None):
        self.prs = prs or []
        self.timeline = timeline or []
        self.details = {pr["number"]: pr for pr in self.prs}

    def get_issue_dispatch_snapshot_strict(self, repo, issue):
        assert (repo, issue) == ("owner/repo", 1863)
        return {"number": 1863, "state": "open"}

    def get_issue_timeline_strict(self, repo, issue):
        return self.timeline

    def get_open_pull_requests_strict(self, repo):
        return self.prs

    def get_pull_request_metadata_strict(self, repo, number):
        return self.details[number]


def wham_response(status="completed", latest="completed", user_status=""):
    user = {"id": f"{TASK}~user_1", "turn_status": user_status} if user_status else {"id": f"{TASK}~user_1"}
    return {
        "task": {"id": TASK, "environment_id": "env-prod"},
        "current_user_turn": user,
        "current_assistant_turn": {"id": f"{TASK}~asst_1", "turn_status": status},
        "task_status_display": {"latest_turn_status_display": {"turn_status": latest}},
    }


def make_service(tmp_path: Path, github: GitHubReads, payload):
    repo = CloudRunRepository("owner/repo", tmp_path / "runs.json")
    run = run_record()
    repo.save(run)
    response = Mock(status_code=200)
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    credentials = Mock(access_token="secret", account_id="account")
    with patch("auto_coder.codex_wham_client.load_codex_oauth_credentials", return_value=credentials), patch("auto_coder.codex_wham_client.httpx.get", return_value=response):
        result = CodexObservationService(github, repo, CodexWhamClient()).observe(run)
    request = response
    return result, request


def test_structured_completed_fixture_crosses_transport_and_empty_github(tmp_path):
    result, _ = make_service(tmp_path, GitHubReads(), wham_response())
    assert result.execution.state is CloudTaskState.COMPLETED
    assert result.execution.assistant_turn_id.endswith("~asst_1")
    assert result.execution.recovery_eligible is True
    assert result.pull_request.presence is PullRequestPresence.NO_MATCHING_PR
    assert result.binding.environment_id == "env-prod"


def test_keywords_old_turn_and_contradiction_do_not_complete(tmp_path):
    payload = wham_response("in_progress", "in_progress")
    payload["task"]["title"] = "Ready to create the completed-status test"
    payload["task"]["turns"] = [{"id": f"{TASK}~old", "turn_status": "completed"}]
    result, _ = make_service(tmp_path, GitHubReads(), payload)
    assert result.execution.state is CloudTaskState.RUNNING
    assert result.execution.recovery_eligible is False

    result, _ = make_service(tmp_path, GitHubReads(), wham_response("completed", "running"))
    assert result.execution.state is CloudTaskState.UNKNOWN
    assert result.execution.recovery_eligible is False


def test_empty_local_metadata_still_finds_open_pr_by_closing_reference(tmp_path):
    pr = {"number": 44, "state": "open", "body": "Fixes #1863", "html_url": "https://github.com/owner/repo/pull/44"}
    result, _ = make_service(tmp_path, GitHubReads([pr]), wham_response())
    assert result.pull_request == result.pull_request.__class__(PullRequestPresence.PR_PRESENT, 44, pr["html_url"])


def test_canonical_task_url_and_closed_publication(tmp_path):
    pr = {"number": 45, "state": "closed", "merged": True, "body": f"Work from https://chatgpt.com/codex/tasks/{TASK}", "html_url": "https://github.com/owner/repo/pull/45"}
    result, _ = make_service(tmp_path, GitHubReads([pr]), wham_response())
    assert result.pull_request.presence is PullRequestPresence.PREVIOUSLY_PUBLISHED


def test_pending_new_user_turn_blocks_old_completed_assistant(tmp_path):
    result, _ = make_service(tmp_path, GitHubReads(), wham_response("completed", "completed", "queued"))
    assert result.execution.state is CloudTaskState.UNKNOWN
    assert not result.execution.recovery_eligible


def test_failed_github_page_never_proves_absence(tmp_path):
    github = GitHubReads()
    github.get_open_pull_requests_strict = Mock(side_effect=PermissionError("denied"))
    result, _ = make_service(tmp_path, github, wham_response())
    assert result.pull_request.presence is PullRequestPresence.UNKNOWN
    assert result.errors == ("GitHub observation unavailable: PermissionError",)


def test_closed_reference_without_attempt_ownership_is_ambiguous(tmp_path):
    pr = {"number": 46, "state": "closed", "body": "Resolved #1863", "html_url": "https://github.com/owner/repo/pull/46"}
    result, _ = make_service(tmp_path, GitHubReads([pr]), wham_response())
    assert result.pull_request.presence is PullRequestPresence.AMBIGUOUS


def test_conflicting_environment_fails_closed(tmp_path):
    payload = wham_response()
    payload["task"]["environment_id"] = "other-environment"
    result, _ = make_service(tmp_path, GitHubReads(), payload)
    assert result.execution.state is CloudTaskState.UNKNOWN
    assert not result.execution.recovery_eligible
