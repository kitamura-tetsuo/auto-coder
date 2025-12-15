import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from auto_coder.log_utils import LogEntry, get_sanitized_repo_name


def get_log_dir() -> Optional[Path]:
    repo_name = get_sanitized_repo_name()
    if not repo_name:
        return None
    return Path.home() / ".auto-coder" / repo_name / "test_log"


def get_raw_log_dir() -> Optional[Path]:
    log_dir = get_log_dir()
    if not log_dir:
        return None
    raw_dir = log_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    return raw_dir


def run_tests(test_file: Optional[str] = None) -> Tuple[str, str, int]:
    is_ci = os.getenv("GITHUB_ACTIONS") == "true" or os.getenv("CI") == "true"

    use_uv = False
    try:
        subprocess.run(["uv", "--version"], capture_output=True, check=True)
        use_uv = True
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    command = []
    if use_uv:
        command.extend(["uv", "run"])

    command.append("pytest")

    if test_file:
        command.append(test_file)
        command.extend(["-vv"] if is_ci else ["-v"])
    else:
        command.extend(["-q"] if not is_ci else ["-vv"])

    command.extend(["-n", "auto", "--tb=short", "--timeout=60", "--cov=src/auto_coder", "--cov-report=term-missing"])

    process = subprocess.run(command, capture_output=True, text=True)
    return process.stdout, process.stderr, process.returncode


def save_log(log_entry: LogEntry):
    log_dir = get_log_dir()
    if not log_dir:
        print("Error: Could not determine the log directory.")
        return

    log_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    test_file_sanitized = (log_entry.test_file or "all").replace("/", "_")
    log_file_name = f"{timestamp}_local_{test_file_sanitized}.json"
    log_file_path = log_dir / log_file_name

    with open(log_file_path, "w") as f:
        json.dump(log_entry.to_dict(), f, indent=4)


def collect_and_save_logs(test_file: Optional[str] = None):
    start_time = time.time()
    stdout, stderr, exit_code = run_tests(test_file)
    end_time = time.time()

    success = exit_code == 0

    raw_log_files = []
    raw_log_dir = get_raw_log_dir()
    if raw_log_dir:
        source_log_dir = Path("./logs/tests")
        if source_log_dir.exists():
            for logfile in source_log_dir.glob("*.log"):
                destination = raw_log_dir / logfile.name
                logfile.rename(destination)
                raw_log_files.append(str(destination))
                print(f"Moved {logfile} to {destination}")

    log_entry = LogEntry(
        timestamp=start_time,
        test_file=test_file,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        success=success,
        raw_log_files=raw_log_files,
    )

    save_log(log_entry)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run tests and collect logs.")
    parser.add_argument("test_file", nargs="?", default=None, help="The specific test file to run.")
    args = parser.parse_args()

    collect_and_save_logs(args.test_file)
