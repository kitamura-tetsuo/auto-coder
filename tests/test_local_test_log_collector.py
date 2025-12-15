import json
import os
import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.local_test_log_collector import (
    collect_and_save_logs,
    get_log_dir,
    get_raw_log_dir,
    run_tests,
)


@pytest.fixture
def mock_repo_name():
    with patch(
        "auto_coder.local_test_log_collector.get_sanitized_repo_name"
    ) as mock:
        mock.return_value = "test_owner_test_repo"
        yield mock


@pytest.fixture
def temp_log_dir(tmp_path):
    with patch("pathlib.Path.home", return_value=tmp_path):
        yield tmp_path


def test_get_log_dir_creates_directory(temp_log_dir, mock_repo_name):
    log_dir = get_log_dir()
    expected_dir = (
        temp_log_dir / ".auto-coder" / "test_owner_test_repo" / "test_log"
    )
    assert log_dir == expected_dir


def test_get_raw_log_dir_creates_directory(temp_log_dir, mock_repo_name):
    raw_log_dir = get_raw_log_dir()
    expected_dir = (
        temp_log_dir / ".auto-coder" / "test_owner_test_repo" / "test_log" / "raw"
    )
    assert raw_log_dir == expected_dir
    assert expected_dir.exists()


@patch("auto_coder.local_test_log_collector.run_tests")
def test_collect_and_save_logs(mock_run_tests, temp_log_dir, mock_repo_name, monkeypatch):
    mock_run_tests.return_value = ("stdout", "stderr", 0)

    # Set up source log directory inside the temp directory
    source_log_dir = temp_log_dir / "logs" / "tests"
    source_log_dir.mkdir(parents=True)
    source_log_file = source_log_dir / "test.log"
    source_log_file.write_text("raw log data")

    # Change current working directory to the temp directory for the test
    monkeypatch.chdir(temp_log_dir)

    collect_and_save_logs("tests/test_file.py")

    log_dir = get_log_dir()
    raw_log_dir = get_raw_log_dir()
    moved_log_path = raw_log_dir / "test.log"

    json_files = list(log_dir.glob("*.json"))
    assert len(json_files) == 1
    log_file = json_files[0]

    # Verify naming convention
    assert re.match(
        r"\d{8}_\d{6}_local_tests_test_file\.py\.json", log_file.name
    )

    with open(log_file, "r") as f:
        log_data = json.load(f)

    assert log_data["test_file"] == "tests/test_file.py"
    assert log_data["stdout"] == "stdout"
    assert log_data["stderr"] == "stderr"
    assert log_data["exit_code"] == 0
    assert log_data["success"] is True
    assert moved_log_path.exists()
    assert moved_log_path.read_text() == "raw log data"
    assert not (source_log_dir / "test.log").exists()
    assert len(log_data["raw_log_files"]) == 1
    assert log_data["raw_log_files"][0] == str(moved_log_path)


@patch("subprocess.run")
def test_run_tests_command_construction(mock_subprocess_run):
    mock_subprocess_run.return_value = MagicMock(
        stdout="", stderr="", returncode=0
    )

    # Test with a specific file in a non-CI environment
    with patch.dict(os.environ, {"CI": "false", "GITHUB_ACTIONS": "false"}):
        run_tests("tests/test_specific.py")
        expected_command = [
            "pytest",
            "tests/test_specific.py",
            "-v",
            "-n",
            "auto",
            "--tb=short",
            "--timeout=60",
            "--cov=src/auto_coder",
            "--cov-report=term-missing",
        ]
        # Check if 'uv' and 'run' are in the command, then verify the rest
        args, _ = mock_subprocess_run.call_args
        command = args[0]
        if command[:2] == ["uv", "run"]:
            assert command[2:] == expected_command
        else:
            assert command == expected_command


    # Test with a specific file in a CI environment
    with patch.dict(os.environ, {"CI": "true"}):
        run_tests("tests/test_specific.py")
        expected_command = [
            "pytest",
            "tests/test_specific.py",
            "-vv",
            "-n",
            "auto",
            "--tb=short",
            "--timeout=60",
            "--cov=src/auto_coder",
            "--cov-report=term-missing",
        ]
        args, _ = mock_subprocess_run.call_args
        command = args[0]
        if command[:2] == ["uv", "run"]:
            assert command[2:] == expected_command
        else:
            assert command == expected_command

    # Test running all tests in a non-CI environment
    with patch.dict(os.environ, {"CI": "false", "GITHUB_ACTIONS": "false"}):
        run_tests()
        expected_command = [
            "pytest",
            "-q",
            "-n",
            "auto",
            "--tb=short",
            "--timeout=60",
            "--cov=src/auto_coder",
            "--cov-report=term-missing",
        ]
        args, _ = mock_subprocess_run.call_args
        command = args[0]
        if command[:2] == ["uv", "run"]:
            assert command[2:] == expected_command
        else:
            assert command == expected_command
