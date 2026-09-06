"""
Tests for utility CLI command helpers, specifically get_repo_or_detect.
"""

from unittest.mock import patch

import click
import pytest

from src.auto_coder.cli_commands_utils import get_repo_or_detect
from src.auto_coder.utils import CommandResult


class TestGetRepoOrDetect:
    """Test suite for get_repo_or_detect helper function."""

    def test_explicit_repo_returns_immediately(self):
        """When repo is explicitly provided, return it directly."""
        result = get_repo_or_detect("owner/custom-repo")
        assert result == "owner/custom-repo"

    def test_auto_detect_from_git_repository(self):
        """When repo is None, auto-detect from current git directory."""
        with patch("src.auto_coder.cli_commands_utils.get_current_repo_name", return_value="owner/detected-repo"):
            result = get_repo_or_detect(None)
            assert result == "owner/detected-repo"

    def test_fallback_url_used_when_auto_detect_fails(self):
        """When auto-detect returns None, use fallback_url to extract repository."""
        with patch("src.auto_coder.cli_commands_utils.get_current_repo_name", return_value=None):
            result = get_repo_or_detect(None, fallback_url="https://github.com/owner/url-repo/issues/123")
            assert result == "owner/url-repo"

    def test_fallback_url_pull_request_used_when_auto_detect_fails(self):
        """When auto-detect returns None, extract repo from PR URL."""
        with patch("src.auto_coder.cli_commands_utils.get_current_repo_name", return_value=None):
            result = get_repo_or_detect(None, fallback_url="https://github.com/owner/pr-repo/pull/456")
            assert result == "owner/pr-repo"

    def test_not_in_git_repo_raises_click_exception(self):
        """When not in a git repository and no fallback, raise ClickException."""
        with (
            patch("src.auto_coder.cli_commands_utils.get_current_repo_name", return_value=None),
            patch("src.auto_coder.cli_commands_utils.is_git_repository", return_value=False),
            patch(
                "src.auto_coder.utils.CommandExecutor.run_command",
                return_value=CommandResult(success=False, stdout="", stderr="Not a git repository", returncode=128),
            ),
        ):
            with pytest.raises(click.ClickException, match="Not in a Git repository"):
                get_repo_or_detect(None)

    def test_dubious_ownership_shows_safe_directory_message(self):
        """When git fails due to dubious ownership, show helpful safe.directory instruction."""
        with (
            patch("src.auto_coder.cli_commands_utils.get_current_repo_name", return_value=None),
            patch("src.auto_coder.cli_commands_utils.is_git_repository", return_value=False),
            patch(
                "src.auto_coder.utils.CommandExecutor.run_command",
                return_value=CommandResult(
                    success=False,
                    stdout="",
                    stderr="fatal: detected dubious ownership in repository at '/target/repo'",
                    returncode=128,
                ),
            ),
        ):
            with pytest.raises(click.ClickException, match="Git detected dubious ownership"):
                get_repo_or_detect(None)

    def test_in_git_repo_but_no_origin_raises_click_exception(self):
        """When in a git repo but remote origin cannot be detected, raise helpful error."""
        with patch("src.auto_coder.cli_commands_utils.get_current_repo_name", return_value=None), patch("src.auto_coder.cli_commands_utils.is_git_repository", return_value=True):
            with pytest.raises(click.ClickException, match="Could not auto-detect GitHub repository"):
                get_repo_or_detect(None)
