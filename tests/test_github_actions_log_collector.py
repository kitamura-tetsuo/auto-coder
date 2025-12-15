import json
import re
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.github_actions_log_collector import (
    _fetch_failed_jobs,
    _fetch_job_log,
    _save_log,
    collect_and_save_github_actions_logs,
)
from auto_coder.log_utils import GitHubActionsLogEntry


@pytest.fixture
def mock_gh_logger():
    with patch("auto_coder.github_actions_log_collector.get_gh_logger") as mock_get:
        mock_logger = MagicMock()
        mock_get.return_value = mock_logger
        yield mock_logger


def test_fetch_failed_jobs(mock_gh_logger):
    run_id = 123
    repo_name = "test/repo"
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps(
        {
            "jobs": [
                {"conclusion": "success", "name": "build"},
                {"conclusion": "failure", "name": "test"},
            ]
        }
    )
    mock_gh_logger.execute_with_logging.return_value = mock_result

    failed_jobs = _fetch_failed_jobs(run_id, repo_name)

    assert len(failed_jobs) == 1
    assert failed_jobs[0]["name"] == "test"


def test_fetch_failed_jobs_error(mock_gh_logger):
    run_id = 123
    repo_name = "test/repo"
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "Error fetching jobs"
    mock_gh_logger.execute_with_logging.return_value = mock_result

    failed_jobs = _fetch_failed_jobs(run_id, repo_name)

    assert len(failed_jobs) == 0


def test_fetch_job_log(mock_gh_logger):
    job_id = 456
    repo_name = "test/repo"
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "This is a test log."
    mock_gh_logger.execute_with_logging.return_value = mock_result

    log = _fetch_job_log(job_id, repo_name)

    assert log == "This is a test log."


def test_fetch_job_log_error(mock_gh_logger):
    job_id = 456
    repo_name = "test/repo"
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "Error fetching log"
    mock_gh_logger.execute_with_logging.return_value = mock_result

    log = _fetch_job_log(job_id, repo_name)

    assert log == ""


@patch("auto_coder.github_actions_log_collector.get_log_dir")
def test_save_log(mock_get_log_dir, tmp_path):
    mock_get_log_dir.return_value = tmp_path
    log_entry = GitHubActionsLogEntry(
        timestamp=time.time(),
        action_title="test job",
        run_id=123,
        job_id=456,
        logs="This is a test log.",
        success=False,
    )

    _save_log(log_entry)

    log_files = list(tmp_path.glob("*.json"))
    assert len(log_files) == 1
    log_file = log_files[0]

    # Verify naming convention
    assert re.match(r"\d{8}_\d{6}_github_test_job_123_456\.json", log_file.name)

    with open(log_file, "r") as f:
        data = json.load(f)
        assert data["action_title"] == "test job"
        assert data["run_id"] == 123
        assert data["job_id"] == 456
        assert data["logs"] == "This is a test log."


@patch("auto_coder.github_actions_log_collector.get_current_repo_name")
@patch("auto_coder.github_actions_log_collector._fetch_failed_jobs")
@patch("auto_coder.github_actions_log_collector._fetch_job_log")
@patch("auto_coder.github_actions_log_collector._save_log")
def test_collect_and_save_github_actions_logs(
    mock_save_log,
    mock_fetch_job_log,
    mock_fetch_failed_jobs,
    mock_get_current_repo_name,
):
    run_id = 123
    mock_get_current_repo_name.return_value = "test/repo"
    mock_fetch_failed_jobs.return_value = [{"id": 456, "name": "test", "conclusion": "failure"}]
    mock_fetch_job_log.return_value = "This is a test log."

    collect_and_save_github_actions_logs(run_id)

    mock_fetch_failed_jobs.assert_called_once_with(run_id, "test/repo")
    mock_fetch_job_log.assert_called_once_with(456, "test/repo")
    mock_save_log.assert_called_once()
    log_entry = mock_save_log.call_args[0][0]
    assert log_entry.run_id == run_id
    assert log_entry.job_id == 456
    assert log_entry.action_title == "test"
    assert log_entry.logs == "This is a test log."


@patch("auto_coder.github_actions_log_collector.get_current_repo_name")
@patch("auto_coder.github_actions_log_collector._fetch_failed_jobs")
@patch("auto_coder.github_actions_log_collector._fetch_job_log")
@patch("auto_coder.github_actions_log_collector._save_log")
def test_collect_and_save_github_actions_logs_no_failed_jobs(
    mock_save_log,
    mock_fetch_job_log,
    mock_fetch_failed_jobs,
    mock_get_current_repo_name,
):
    run_id = 123
    mock_get_current_repo_name.return_value = "test/repo"
    mock_fetch_failed_jobs.return_value = []

    collect_and_save_github_actions_logs(run_id)

    mock_fetch_failed_jobs.assert_called_once_with(run_id, "test/repo")
    mock_fetch_job_log.assert_not_called()
    mock_save_log.assert_not_called()
