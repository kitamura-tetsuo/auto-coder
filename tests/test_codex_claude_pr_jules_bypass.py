"""Tests for Codex Cloud PR body URL enhancement and Jules fix bypass on Codex/Claude PRs."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.cloud_manager import CloudManager
from auto_coder.pr_processor import (
    _find_codex_cloud_task_for_issue,
    _is_claude_pr,
    _is_codex_or_claude_pr,
    _is_codex_pr,
    _is_jules_pr,
    _link_codex_cloud_pr_to_issue,
    _send_jules_error_feedback,
    _should_skip_waiting_for_jules,
    process_pull_request,
)


def test_is_codex_pr_detection():
    """Verify Codex PR detection via session/task URL in PR body and author login."""
    # Body containing ChatGPT Codex Cloud task URL
    pr_with_url = {
        "number": 1,
        "body": "Fix bug\n\nCloses #10\n\nhttps://chatgpt.com/codex/tasks/task_e_abc12345",
        "user": {"login": "octocat"},
    }
    assert _is_codex_pr(pr_with_url) is True

    # Body containing chat.openai.com Codex task URL
    pr_with_openai_url = {
        "number": 2,
        "body": "Implementation\n\nhttps://chat.openai.com/codex/tasks/task_xyz789",
        "user": {"login": "octocat"},
    }
    assert _is_codex_pr(pr_with_openai_url) is True

    # PR author login starting with codex
    pr_with_author = {
        "number": 3,
        "body": "Fixes #5",
        "user": {"login": "codex-bot"},
    }
    assert _is_codex_pr(pr_with_author) is True

    # Non-Codex PR
    normal_pr = {
        "number": 4,
        "body": "Just a normal PR\n\nCloses #20",
        "user": {"login": "developer"},
    }
    assert _is_codex_pr(normal_pr) is False


def test_is_claude_pr_detection():
    """Verify Claude PR detection via session URL in PR body and author login."""
    # Body containing Claude Code session URL
    pr_with_claude_url = {
        "number": 1,
        "body": "Add feature\n\nCloses #15\n\nhttps://claude.ai/code/session_01HJKLMNOPQR",
        "user": {"login": "octocat"},
    }
    assert _is_claude_pr(pr_with_claude_url) is True

    # Body containing Claude session text
    pr_with_claude_text = {
        "number": 2,
        "body": "Resolved issue. Claude session active.",
        "user": {"login": "octocat"},
    }
    assert _is_claude_pr(pr_with_claude_text) is True

    # PR author login starting with claude
    pr_with_author = {
        "number": 3,
        "body": "Fixes #3",
        "user": {"login": "claude[bot]"},
    }
    assert _is_claude_pr(pr_with_author) is True

    # Non-Claude PR
    normal_pr = {
        "number": 4,
        "body": "Regular PR\n\nFixes #1",
        "user": {"login": "developer"},
    }
    assert _is_claude_pr(normal_pr) is False


def test_is_codex_or_claude_pr():
    """Verify combined Codex/Claude PR check."""
    assert _is_codex_or_claude_pr({"body": "https://chatgpt.com/codex/tasks/task_1", "user": {}}) is True
    assert _is_codex_or_claude_pr({"body": "https://claude.ai/code/session_1", "user": {}}) is True
    assert _is_codex_or_claude_pr({"body": "Regular PR", "user": {"login": "user1"}}) is False


def test_is_jules_pr_excludes_codex_and_claude():
    """Verify that Codex and Claude PRs are never identified as Jules PRs."""
    codex_pr = {
        "number": 1,
        "body": "Closes #10\n\nhttps://chatgpt.com/codex/tasks/task_e_123",
        "user": {"login": "google-labs-jules[bot]"},  # Even if bot login spoofed
    }
    assert _is_jules_pr(codex_pr) is False

    claude_pr = {
        "number": 2,
        "body": "Closes #11\n\nhttps://claude.ai/code/session_abc",
        "user": {"login": "google-labs-jules[bot]"},
    }
    assert _is_jules_pr(claude_pr) is False

    # Genuine Jules PR
    jules_pr = {
        "number": 3,
        "body": "Fixed bug\n\nhttps://jules.google.com/session/987654321",
        "user": {"login": "google-labs-jules[bot]"},
    }
    assert _is_jules_pr(jules_pr) is True


def test_should_skip_waiting_for_jules_bypasses_codex_and_claude():
    """Verify _should_skip_waiting_for_jules immediately returns False for Codex/Claude PRs."""
    github_client = MagicMock()
    config = AutomationConfig()

    codex_pr = {
        "number": 10,
        "body": "Closes #5\n\nhttps://chatgpt.com/codex/tasks/task_e_999",
        "user": {"login": "octocat"},
    }
    assert _should_skip_waiting_for_jules(github_client, "owner/repo", codex_pr, config) is False
    github_client.get_pr_comments.assert_not_called()

    claude_pr = {
        "number": 11,
        "body": "Closes #6\n\nhttps://claude.ai/code/session_888",
        "user": {"login": "octocat"},
    }
    assert _should_skip_waiting_for_jules(github_client, "owner/repo", claude_pr, config) is False
    github_client.get_pr_comments.assert_not_called()


def test_send_jules_error_feedback_bypasses_codex_and_claude():
    """Verify _send_jules_error_feedback never messages Jules or comments on Codex/Claude PRs."""
    github_client = MagicMock()
    config = AutomationConfig()

    codex_pr = {
        "number": 10,
        "body": "Closes #5\n\nhttps://chatgpt.com/codex/tasks/task_e_999",
        "user": {"login": "octocat"},
    }

    with patch("auto_coder.jules_client.JulesClient") as mock_jules:
        actions = _send_jules_error_feedback("owner/repo", codex_pr, [{"name": "test"}], config, github_client)
        assert any("Skipped Jules error feedback" in action for action in actions)
        mock_jules.assert_not_called()
        github_client.add_comment_to_pr.assert_not_called()


def test_find_codex_cloud_task_for_issue_cloud_manager(tmp_path: Path):
    """Verify finding Codex Cloud task URL via CloudManager."""
    repo = "test-owner/test-repo"
    cloud_csv = tmp_path / "cloud.csv"
    cloud_manager = CloudManager(repo, cloud_file_path=cloud_csv)
    cloud_manager.add_session(42, "task_e_abcdef123456")

    with patch("auto_coder.pr_processor.CloudManager", return_value=cloud_manager):
        url = _find_codex_cloud_task_for_issue(repo, 42)
        assert url == "https://chatgpt.com/codex/tasks/task_e_abcdef123456"


def test_find_codex_cloud_task_for_issue_comments(tmp_path: Path):
    """Verify finding Codex Cloud task URL via GitHub issue comments."""
    repo = "test-owner/test-repo"
    cloud_csv = tmp_path / "empty.csv"
    cloud_manager = CloudManager(repo, cloud_file_path=cloud_csv)

    github_client = MagicMock()
    github_client.get_issue_comments.return_value = [{"body": "I started a Codex Cloud task to work on this issue. Task ID: task_e_comment123\n\nhttps://chatgpt.com/codex/tasks/task_e_comment123"}]

    with patch("auto_coder.pr_processor.CloudManager", return_value=cloud_manager):
        url = _find_codex_cloud_task_for_issue(repo, 99, github_client)
        assert url == "https://chatgpt.com/codex/tasks/task_e_comment123"


def test_link_codex_cloud_pr_to_issue_appends_url(tmp_path: Path):
    """Verify _link_codex_cloud_pr_to_issue appends the Codex Cloud URL to PR body."""
    repo = "test-owner/test-repo"
    cloud_csv = tmp_path / "cloud.csv"
    cloud_manager = CloudManager(repo, cloud_file_path=cloud_csv)
    cloud_manager.add_session(55, "task_e_55555")

    pr_data = {
        "number": 105,
        "body": "Implementation details.\n\nCloses #55",
        "user": {"login": "octocat"},
    }

    github_client = MagicMock()
    github_client.token = "fake-token"
    mock_ghapi = MagicMock()

    with patch("auto_coder.pr_processor.CloudManager", return_value=cloud_manager), patch("auto_coder.util.gh_cache.get_ghapi_client", return_value=mock_ghapi), patch("auto_coder.pr_processor.validate_issue_references"):
        result = _link_codex_cloud_pr_to_issue(repo, pr_data, github_client)
        assert result is True
        expected_url = "https://chatgpt.com/codex/tasks/task_e_55555"
        assert expected_url in pr_data["body"]
        mock_ghapi.pulls.update.assert_called_once_with("test-owner", "test-repo", 105, body=pr_data["body"])


def test_link_codex_cloud_pr_to_issue_skips_when_already_present(tmp_path: Path):
    """Verify _link_codex_cloud_pr_to_issue does not duplicate if URL is already in PR body."""
    repo = "test-owner/test-repo"
    cloud_csv = tmp_path / "cloud.csv"
    cloud_manager = CloudManager(repo, cloud_file_path=cloud_csv)
    cloud_manager.add_session(55, "task_e_55555")

    existing_url = "https://chatgpt.com/codex/tasks/task_e_55555"
    pr_data = {
        "number": 105,
        "body": f"Implementation details.\n\nCloses #55\n\n{existing_url}",
        "user": {"login": "octocat"},
    }

    github_client = MagicMock()
    github_client.token = "fake-token"
    mock_ghapi = MagicMock()

    with patch("auto_coder.pr_processor.CloudManager", return_value=cloud_manager), patch("auto_coder.util.gh_cache.get_ghapi_client", return_value=mock_ghapi):
        result = _link_codex_cloud_pr_to_issue(repo, pr_data, github_client)
        assert result is True
        mock_ghapi.pulls.update.assert_not_called()
