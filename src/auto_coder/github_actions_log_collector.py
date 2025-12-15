import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .gh_logger import get_gh_logger
from .git_info import get_current_repo_name
from .log_utils import GitHubActionsLogEntry, get_sanitized_repo_name
from .logger_config import get_logger

logger = get_logger(__name__)


def get_log_dir() -> Optional[Path]:
    repo_name = get_sanitized_repo_name()
    if not repo_name:
        return None
    log_dir = Path.home() / ".auto-coder" / repo_name / "test_log"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _fetch_failed_jobs(run_id: int, repo_name: str) -> List[Dict[str, Any]]:
    """Fetches the failed jobs for a given GitHub Actions run."""
    gh_logger = get_gh_logger()
    command = [
        "gh",
        "run",
        "view",
        str(run_id),
        "--json",
        "jobs",
        "-R",
        repo_name,
    ]
    result = gh_logger.execute_with_logging(command, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"Failed to get jobs for run {run_id}: {result.stderr}")
        return []

    try:
        data = json.loads(result.stdout)
        jobs = data.get("jobs", [])
        failed_jobs = [job for job in jobs if job.get("conclusion") == "failure"]
        return failed_jobs
    except json.JSONDecodeError:
        logger.error(f"Failed to parse JSON for run {run_id} jobs.")
        return []


def _fetch_job_log(job_id: int, repo_name: str) -> str:
    """Fetches the log for a given job."""
    gh_logger = get_gh_logger()
    command = [
        "gh",
        "run",
        "view",
        "--job",
        str(job_id),
        "--log",
        "-R",
        repo_name,
    ]
    result = gh_logger.execute_with_logging(command, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"Failed to get log for job {job_id}: {result.stderr}")
        return ""
    return result.stdout


def _save_log(log_entry: GitHubActionsLogEntry):
    """Saves the GitHub Actions log entry to a file."""
    log_dir = get_log_dir()
    if not log_dir:
        logger.error("Could not determine the log directory.")
        return

    timestamp = datetime.fromtimestamp(log_entry.timestamp).strftime("%Y%m%d_%H%M%S")
    sanitized_title = log_entry.action_title.replace(" ", "_").replace("/", "_")
    log_file_name = f"{timestamp}_github_{sanitized_title}_{log_entry.run_id}_{log_entry.job_id}.json"
    log_file_path = log_dir / log_file_name

    with open(log_file_path, "w") as f:
        json.dump(log_entry.to_dict(), f, indent=4)
    logger.info(f"Saved GitHub Actions log to {log_file_path}")


def collect_and_save_github_actions_logs(run_id: int):
    """Collects and saves logs for failed jobs in a GitHub Actions run."""
    repo_name = get_current_repo_name()
    if not repo_name:
        logger.error("Could not determine repository name.")
        return

    failed_jobs = _fetch_failed_jobs(run_id, repo_name)
    if not failed_jobs:
        logger.info(f"No failed jobs found for run {run_id}.")
        return

    for job in failed_jobs:
        job_id = job.get("id")
        job_name = job.get("name")
        if not job_id or not job_name:
            continue

        logs = _fetch_job_log(job_id, repo_name)
        if not logs:
            continue

        log_entry = GitHubActionsLogEntry(
            timestamp=time.time(),
            action_title=job_name,
            run_id=run_id,
            job_id=job_id,
            logs=logs,
            success=False,
        )
        _save_log(log_entry)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Collect logs from GitHub Actions runs.")
    parser.add_argument("run_id", type=int, help="The ID of the GitHub Actions run.")
    args = parser.parse_args()

    collect_and_save_github_actions_logs(args.run_id)
