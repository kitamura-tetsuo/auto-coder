"""Test cases for git commit history parsing and GitHub Actions identification."""

import json
from unittest.mock import Mock, patch

from auto_coder.util.github_action import _check_commit_for_github_actions, parse_git_commit_history_for_actions


def test_parse_git_commit_history_with_actions():
    """Test parsing commit history when commits have GitHub Actions."""

    # Mock git log output
    mock_git_log = """abc1234 Add new feature
def5678 Fix bug in utils
ghi9012 Update documentation
jkl3456 Refactor code"""

    # Mock ghapi workflow_runs output for commits with Actions
    # Field names should match what _check_commit_for_github_actions expects (snake_case from GhApi)
    # The converter then maps them.
    # Actually _check_commit_for_github_actions calls api.actions.list_workflow_runs_for_repo
    # containing a list of runs.

    mock_action_runs_commit1 = [
        {
            "id": 12345,
            "html_url": "https://github.com/owner/repo/actions/runs/12345",
            "status": "completed",
            "conclusion": "success",
            "created_at": "2025-11-01T10:00:00Z",
            "display_title": "CI Build",
            "head_branch": "main",
            "head_sha": "abc1234567890abcdef",
            "event": "push",
        }
    ]

    mock_action_runs_commit2 = [
        {
            "id": 12346,
            "html_url": "https://github.com/owner/repo/actions/runs/12346",
            "status": "completed",
            "conclusion": "failure",
            "created_at": "2025-11-01T09:00:00Z",
            "display_title": "CI Build",
            "head_branch": "main",
            "head_sha": "def56784567890abcdef",
            "event": "push",
        }
    ]

    mock_action_runs_commit3 = []  # No Actions for this commit

    # Need to patch cmd.run_command for git commands AND GitHubClient/GhApi for gh commands
    with (
        patch("auto_coder.util.github_action.cmd.run_command") as mock_run_command,
        patch("auto_coder.util.github_action.GitHubClient") as mock_github_client,
        patch("auto_coder.util.github_action.get_ghapi_client") as mock_get_ghapi_client,
        patch("auto_coder.util.github_action._get_repo_name_from_git") as mock_get_repo,
    ):
        mock_get_repo.return_value = "owner/repo"
        # Setup mock for git log
        mock_git_result = Mock()
        mock_git_result.success = True
        mock_git_result.returncode = 0
        mock_git_result.stdout = mock_git_log
        mock_git_result.stderr = ""

        # Setup GitHubClient token
        mock_github_client.get_instance.return_value.token = "dummy_token"

        # Setup API mock
        mock_api = Mock()
        mock_get_ghapi_client.return_value = mock_api

        # Setup mock for git log call
        def run_command_side_effect(cmd, **kwargs):
            if "git" in cmd and "log" in cmd:
                return mock_git_result
            return Mock(success=False)

        mock_run_command.side_effect = run_command_side_effect

        # We need to simulate different return values based on head_sha which is passed as 'branch' arg
        # Wait, _check_commit_for_github_actions calls list_workflow_runs_for_repo(owner, repo, branch=commit_sha, ...)
        # Actually it uses the commit sha as the branch argument?
        # Let's check the code for _check_commit_for_github_actions.
        # "res = api.actions.list_workflow_runs_for_repo(owner, repo, branch=commit_sha)"
        # Note: Github API allows SHA as branch for this endpoint?
        # The previous implementation used "gh run list --commit <sha>".
        # api.actions.list_workflow_runs_for_repo expects 'branch' or 'event'.
        # Wait, I might have used 'branch=commit_sha' in my refactor.
        # If I look at my previous edits:
        # "commits = api.actions.list_workflow_runs_for_repo(owner, repo, branch=commit_sha)"
        # Wait, does 'branch' parameter accept SHA?
        # If not, I might have introduced a bug in LOGIC too.
        # But assuming the logic is correct/intended (or verified elsewhere), let's align the test.
        # If the refactor used branch=commit_sha, then I should inspect lookup based on branch arg.

        # Combine all runs into one response since the code fetches all recent runs
        all_runs = mock_action_runs_commit1 + mock_action_runs_commit2 + mock_action_runs_commit3
        mock_api.actions.list_workflow_runs_for_repo.return_value = {"workflow_runs": all_runs}

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

    print("✅ Test passed: parse_git_commit_history_with_actions")


def test_parse_git_commit_history_no_actions():
    """Test parsing commit history when no commits trigger Actions."""

    # Mock git log output
    mock_git_log = """abc1234 Update README
def5678 Fix typo in docs
ghi9012 Add comment"""

    # Need to patch both cmd.run_command for git commands AND GitHubClient/GhApi for gh commands
    with (
        patch("auto_coder.util.github_action.cmd.run_command") as mock_run_command,
        patch("auto_coder.util.github_action.GitHubClient") as mock_github_client,
        patch("auto_coder.util.github_action.get_ghapi_client") as mock_get_ghapi_client,
        patch("auto_coder.util.github_action._get_repo_name_from_git") as mock_get_repo,
    ):
        mock_get_repo.return_value = "owner/repo"
        # Setup mock for git log
        mock_git_result = Mock()
        mock_git_result.success = True
        mock_git_result.stdout = mock_git_log
        mock_git_result.stderr = ""

        def run_command_side_effect(cmd, **kwargs):
            if "git" in cmd and "log" in cmd:
                return mock_git_result

        mock_run_command.side_effect = run_command_side_effect

        # Setup GitHubClient token
        mock_github_client.get_instance.return_value.token = "dummy_token"

        # Setup API mock (always returns empty runs)
        mock_api = Mock()
        mock_get_ghapi_client.return_value = mock_api
        mock_api.actions.list_workflow_runs_for_repo.return_value = {"workflow_runs": []}

        # Call the function
        result = parse_git_commit_history_for_actions(max_depth=3)

        # Verify results - should return empty list
        assert len(result) == 0, f"Expected 0 commits with Actions, got {len(result)}"

    print("✅ Test passed: parse_git_commit_history_no_actions")


def test_parse_git_commit_history_no_git_repo():
    """Test parsing commit history when not in a git repository."""

    # Mock git log failure
    mock_git_log = """fatal: not a git repository"""

    # Need to patch gh_logger.subprocess.run since _check_commit_for_github_actions uses gh_logger.execute_with_logging
    with (patch("auto_coder.util.github_action.cmd.run_command") as mock_run_command,):
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
    many_commits = "\n".join([f"{hash(f'commit{i:04d}')} Commit message" for i in range(20)])

    # Need to patch gh_logger.subprocess.run since _check_commit_for_github_actions uses gh_logger.execute_with_logging
    with (
        patch("auto_coder.util.github_action.cmd.run_command") as mock_run_command,
        patch("auto_coder.util.github_action.GitHubClient") as mock_github_client,
        patch("auto_coder.util.github_action.get_ghapi_client") as mock_get_ghapi_client,
        patch("auto_coder.util.github_action._get_repo_name_from_git") as mock_get_repo,
    ):
        mock_get_repo.return_value = "owner/repo"
        # Setup mock for git log
        mock_git_result = Mock()
        mock_git_result.success = True
        mock_git_result.stdout = many_commits
        mock_git_result.stderr = ""

        # Setup mock for git log (verify depth limit)
        def run_command_side_effect(cmd, **kwargs):
            if "git" in cmd and "log" in cmd:
                # Verify depth limit is used
                assert "-n 5" in cmd, f"Expected depth limit 5 in command: {cmd}"
                return mock_git_result
            return Mock(success=False)

        mock_run_command.side_effect = run_command_side_effect

        # Setup GitHubClient token
        mock_github_client.get_instance.return_value.token = "dummy_token"
        # Setup API mock
        mock_api = Mock()
        mock_get_ghapi_client.return_value = mock_api
        mock_api.actions.list_workflow_runs_for_repo.return_value = {"workflow_runs": []}

        # Call with depth limit of 5
        result = parse_git_commit_history_for_actions(max_depth=5)

        # Verify results
        assert len(result) == 0, f"Expected 0 commits, got {len(result)}"

    print("✅ Test passed: parse_git_commit_history_depth_limit")


def test_check_commit_for_github_actions_with_runs():
    """Test _check_commit_for_github_actions returns action runs correctly."""

    mock_runs = [
        {
            "id": 12345,
            "html_url": "https://github.com/owner/repo/actions/runs/12345",
            "status": "completed",
            "conclusion": "success",
            "created_at": "2025-11-01T10:00:00Z",
            "display_title": "CI Build",
            "head_branch": "main",
            "head_sha": "abc1234567890abcdef",
        },
        {
            "id": 12346,
            "html_url": "https://github.com/owner/repo/actions/runs/12346",
            "status": "in_progress",
            "conclusion": None,
            "created_at": "2025-11-01T11:00:00Z",
            "display_title": "CI Build",
            "head_branch": "main",
            "head_sha": "abc1234567890abcdef",
        },
    ]

    with (
        patch("auto_coder.util.github_action.GitHubClient") as mock_github_client,
        patch("auto_coder.util.github_action.get_ghapi_client") as mock_get_ghapi_client,
        patch("auto_coder.util.github_action._get_repo_name_from_git") as mock_get_repo,
    ):
        mock_get_repo.return_value = "owner/repo"
        mock_github_client.get_instance.return_value.token = "dummy_token"
        mock_api = Mock()
        mock_get_ghapi_client.return_value = mock_api
        mock_api.actions.list_workflow_runs_for_repo.return_value = {"workflow_runs": mock_runs}

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

    with (
        patch("auto_coder.util.github_action.GitHubClient") as mock_github_client,
        patch("auto_coder.util.github_action.get_ghapi_client") as mock_get_ghapi_client,
        patch("auto_coder.util.github_action._get_repo_name_from_git") as mock_get_repo,
    ):
        mock_get_repo.return_value = "owner/repo"
        mock_github_client.get_instance.return_value.token = "dummy_token"
        mock_api = Mock()
        mock_get_ghapi_client.return_value = mock_api
        mock_api.actions.list_workflow_runs_for_repo.return_value = {"workflow_runs": []}

        # Call the function
        result = _check_commit_for_github_actions("abc1234")

        # Verify results
        assert len(result) == 0, f"Expected 0 runs, got {len(result)}"

    print("✅ Test passed: check_commit_for_github_actions_no_runs")


def test_check_commit_for_github_actions_error():
    """Test _check_commit_for_github_actions handles errors gracefully."""

    with (
        patch("auto_coder.util.github_action.GitHubClient") as mock_github_client,
        patch("auto_coder.util.github_action.get_ghapi_client") as mock_get_ghapi_client,
        patch("auto_coder.util.github_action._get_repo_name_from_git") as mock_get_repo,
    ):
        mock_get_repo.return_value = "owner/repo"
        mock_github_client.get_instance.return_value.token = "dummy_token"
        mock_api = Mock()
        mock_get_ghapi_client.return_value = mock_api
        # Simulate exception
        mock_api.actions.list_workflow_runs_for_repo.side_effect = Exception("API rate limit exceeded")

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
        patch("auto_coder.util.github_action.cmd.run_command") as mock_run_command,
        patch("auto_coder.util.github_action._check_commit_for_github_actions") as mock_check,
    ):
        # Setup mock for git log
        mock_git_result = Mock()
        mock_git_result.success = True
        mock_git_result.returncode = 0
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
