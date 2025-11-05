"""Test cases for git commit history parsing and GitHub Actions identification."""

import json
from unittest.mock import Mock, patch

from src.auto_coder.util.github_action import (
    _check_commit_for_github_actions, parse_git_commit_history_for_actions)


def test_parse_git_commit_history_with_actions():
    """Test parsing commit history when commits have GitHub Actions."""

    # Mock git log output
    mock_git_log = """abc1234 Add new feature
def5678 Fix bug in utils
ghi9012 Update documentation
jkl3456 Refactor code"""

    # Mock gh run list output for commits with Actions
    mock_action_runs_commit1 = [
        {
            "databaseId": 12345,
            "url": "https://github.com/owner/repo/actions/runs/12345",
            "status": "completed",
            "conclusion": "success",
            "createdAt": "2025-11-01T10:00:00Z",
            "displayTitle": "CI Build",
            "headBranch": "main",
            "headSha": "abc1234567890abcdef",
            "event": "push",
        }
    ]

    mock_action_runs_commit2 = [
        {
            "databaseId": 12346,
            "url": "https://github.com/owner/repo/actions/runs/12346",
            "status": "completed",
            "conclusion": "failure",
            "createdAt": "2025-11-01T09:00:00Z",
            "displayTitle": "CI Build",
            "headBranch": "main",
            "headSha": "def56784567890abcdef",
            "event": "push",
        }
    ]

    mock_action_runs_commit3 = []  # No Actions for this commit

    with patch("src.auto_coder.util.github_action.cmd.run_command") as mock_run_command:
        # Setup mock for git log
        mock_git_result = Mock()
        mock_git_result.success = True
        mock_git_result.stdout = mock_git_log
        mock_git_result.stderr = ""

        # Setup mock for gh run list: 呼び出し順で各コミット相当の結果を返す
        list_call = {"i": 0}

        def run_command_side_effect(cmd, **kwargs):
            if "git" in cmd and "log" in cmd:
                return mock_git_result
            elif "gh" in cmd and "run" in cmd and "list" in cmd:
                list_call["i"] += 1
                mock_result = Mock()
                mock_result.returncode = 0
                if list_call["i"] == 1:
                    mock_result.stdout = json.dumps(mock_action_runs_commit1)
                elif list_call["i"] == 2:
                    mock_result.stdout = json.dumps(mock_action_runs_commit2)
                elif list_call["i"] == 3:
                    mock_result.stdout = "[]"
                else:
                    mock_result.stdout = "[]"
                return mock_result

        mock_run_command.side_effect = run_command_side_effect

        # Call the function
        result = parse_git_commit_history_for_actions(max_depth=4)

        # Verify results
        assert len(result) == 2, f"Expected 2 commits with Actions, got {len(result)}"

        # Check first commit (has Actions)
        assert result[0]["sha"] == "abc1234"
        assert result[0]["message"] == "Add new feature"
        assert result[0]["has_logs"] is True
        assert len(result[0]["action_runs"]) == 1
        assert result[0]["action_runs"][0]["run_id"] == 12345
        assert result[0]["action_runs"][0]["conclusion"] == "success"

        # Check second commit (has Actions)
        assert result[1]["sha"] == "def5678"
        assert result[1]["message"] == "Fix bug in utils"
        assert result[1]["has_logs"] is True
        assert len(result[1]["action_runs"]) == 1
        assert result[1]["action_runs"][0]["run_id"] == 12346
        assert result[1]["action_runs"][0]["conclusion"] == "failure"

        # Verify that commit 3 was skipped (no Actions)
        # (Only commits 0 and 1 should be in the result)

    print("✅ Test passed: parse_git_commit_history_with_actions")


def test_parse_git_commit_history_no_actions():
    """Test parsing commit history when no commits trigger Actions."""

    # Mock git log output
    mock_git_log = """abc1234 Update README
def5678 Fix typo in docs
ghi9012 Add comment"""

    with patch("src.auto_coder.util.github_action.cmd.run_command") as mock_run_command:
        # Setup mock for git log
        mock_git_result = Mock()
        mock_git_result.success = True
        mock_git_result.stdout = mock_git_log
        mock_git_result.stderr = ""

        # Setup mock for gh run list (no Actions for any commit)
        def run_command_side_effect(cmd, **kwargs):
            if "git" in cmd and "log" in cmd:
                return mock_git_result
            elif "gh" in cmd and "run" in cmd and "list" in cmd:
                mock_result = Mock()
                mock_result.returncode = 0
                mock_result.stdout = "[]"
                return mock_result

        mock_run_command.side_effect = run_command_side_effect

        # Call the function
        result = parse_git_commit_history_for_actions(max_depth=3)

        # Verify results - should return empty list
        assert len(result) == 0, f"Expected 0 commits with Actions, got {len(result)}"

    print("✅ Test passed: parse_git_commit_history_no_actions")


def test_parse_git_commit_history_no_git_repo():
    """Test parsing commit history when not in a git repository."""

    # Mock git log failure
    mock_git_log = """fatal: not a git repository"""

    with patch("src.auto_coder.util.github_action.cmd.run_command") as mock_run_command:
        # Setup mock for git log (fails)
        mock_git_result = Mock()
        mock_git_result.success = False
        mock_git_result.stdout = ""
        mock_git_result.stderr = mock_git_log

        def run_command_side_effect(cmd, **kwargs):
            if "git" in cmd and "log" in cmd:
                return mock_git_result
            # Should not reach gh commands

        mock_run_command.side_effect = run_command_side_effect

        # Call the function
        result = parse_git_commit_history_for_actions(max_depth=10)

        # Verify results - should return empty list on error
        assert len(result) == 0, f"Expected 0 commits, got {len(result)}"

    print("✅ Test passed: parse_git_commit_history_no_git_repo")


def test_parse_git_commit_history_depth_limit():
    """Test that search depth limit is respected."""

    # Mock git log with many commits
    many_commits = "\n".join(
        [f"{hash(f'commit{i:04d}')} Commit message" for i in range(20)]
    )

    with patch("src.auto_coder.util.github_action.cmd.run_command") as mock_run_command:
        # Setup mock for git log
        mock_git_result = Mock()
        mock_git_result.success = True
        mock_git_result.stdout = many_commits
        mock_git_result.stderr = ""

        # Setup mock for gh run list (no Actions)
        def run_command_side_effect(cmd, **kwargs):
            if "git" in cmd and "log" in cmd:
                # Verify depth limit is used
                assert "-n 5" in cmd, f"Expected depth limit 5 in command: {cmd}"
                return mock_git_result
            elif "gh" in cmd and "run" in cmd and "list" in cmd:
                mock_result = Mock()
                mock_result.returncode = 0
                mock_result.stdout = "[]"
                return mock_result

        mock_run_command.side_effect = run_command_side_effect

        # Call with depth limit of 5
        result = parse_git_commit_history_for_actions(max_depth=5)

        # Verify results
        assert len(result) == 0, f"Expected 0 commits, got {len(result)}"

    print("✅ Test passed: parse_git_commit_history_depth_limit")


def test_check_commit_for_github_actions_with_runs():
    """Test _check_commit_for_github_actions returns action runs correctly."""

    mock_runs = [
        {
            "databaseId": 12345,
            "url": "https://github.com/owner/repo/actions/runs/12345",
            "status": "completed",
            "conclusion": "success",
            "createdAt": "2025-11-01T10:00:00Z",
            "displayTitle": "CI Build",
            "headBranch": "main",
            "headSha": "abc1234567890abcdef",
        },
        {
            "databaseId": 12346,
            "url": "https://github.com/owner/repo/actions/runs/12346",
            "status": "in_progress",
            "conclusion": None,
            "createdAt": "2025-11-01T11:00:00Z",
            "displayTitle": "CI Build",
            "headBranch": "main",
            "headSha": "abc1234567890abcdef",
        },
    ]

    with patch("src.auto_coder.util.github_action.cmd.run_command") as mock_run_command:
        # Setup mock for gh run list
        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_runs)
        mock_run_command.return_value = mock_result

        # Call the function
        result = _check_commit_for_github_actions("abc1234")

        # Verify results
        assert len(result) == 2, f"Expected 2 runs, got {len(result)}"
        assert result[0]["run_id"] == 12345
        assert result[0]["status"] == "completed"
        assert result[0]["conclusion"] == "success"
        assert result[1]["run_id"] == 12346
        assert result[1]["status"] == "in_progress"
        assert result[1]["conclusion"] is None

    print("✅ Test passed: check_commit_for_github_actions_with_runs")


def test_check_commit_for_github_actions_no_runs():
    """Test _check_commit_for_github_actions when commit has no runs."""

    with patch("src.auto_coder.util.github_action.cmd.run_command") as mock_run_command:
        # Setup mock for gh run list (no runs)
        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = "[]"
        mock_run_command.return_value = mock_result

        # Call the function
        result = _check_commit_for_github_actions("abc1234")

        # Verify results
        assert len(result) == 0, f"Expected 0 runs, got {len(result)}"

    print("✅ Test passed: check_commit_for_github_actions_no_runs")


def test_check_commit_for_github_actions_error():
    """Test _check_commit_for_github_actions handles errors gracefully."""

    with patch("src.auto_coder.util.github_action.cmd.run_command") as mock_run_command:
        # Setup mock for gh run list (API error)
        mock_result = Mock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "API rate limit exceeded"
        mock_run_command.return_value = mock_result

        # Call the function
        result = _check_commit_for_github_actions("abc1234")

        # Verify results - should return empty list on error
        assert len(result) == 0, f"Expected 0 runs, got {len(result)}"

    print("✅ Test passed: check_commit_for_github_actions_error")


def test_parse_git_commit_history_with_malformed_lines():
    """Test parsing commit history with malformed git log lines."""

    # Mock git log with some malformed lines
    mock_git_log = """abc1234 Valid commit
invalid-line-without-space
def5678 Another valid commit
ghi9012 Third commit"""

    with (
        patch("src.auto_coder.util.github_action.cmd.run_command") as mock_run_command,
        patch(
            "src.auto_coder.util.github_action._check_commit_for_github_actions"
        ) as mock_check,
    ):
        # Setup mock for git log
        mock_git_result = Mock()
        mock_git_result.success = True
        mock_git_result.stdout = mock_git_log
        mock_git_result.stderr = ""

        # Track which commits were checked via the helper function
        checked_commits = []

        def run_command_side_effect(cmd, **kwargs):
            if "git" in cmd and "log" in cmd:
                return mock_git_result
            # Should not be called for gh run list because we stub _check_commit_for_github_actions
            mock_result = Mock()
            mock_result.returncode = 0
            mock_result.stdout = "[]"
            return mock_result

        mock_run_command.side_effect = run_command_side_effect

        def check_side_effect(commit_sha, cwd=None, timeout=60):
            checked_commits.append(commit_sha[:7])
            return []

        mock_check.side_effect = check_side_effect

        # Call the function
        result = parse_git_commit_history_for_actions(max_depth=4)

        # Verify results - should skip malformed lines
        assert len(result) == 0, f"Expected 0 commits, got {len(result)}"

        # Verify only valid commit SHAs were checked (malformed line should be skipped)
        assert "abc1234" in checked_commits
        assert "def5678" in checked_commits

    print("✅ Test passed: parse_git_commit_history_with_malformed_lines")


if __name__ == "__main__":
    print("Running tests for git commit history parsing...\n")

    # Run all tests
    test_parse_git_commit_history_with_actions()
    test_parse_git_commit_history_no_actions()
    test_parse_git_commit_history_no_git_repo()
    test_parse_git_commit_history_depth_limit()
    test_check_commit_for_github_actions_with_runs()
    test_check_commit_for_github_actions_no_runs()
    test_check_commit_for_github_actions_error()
    test_parse_git_commit_history_with_malformed_lines()

    print("\n✅ All tests passed!")
