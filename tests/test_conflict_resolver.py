import json
from unittest.mock import MagicMock, patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.conflict_resolver import _perform_base_branch_merge_and_conflict_resolution
from src.auto_coder.utils import CommandResult


def test_perform_base_merge_uses_fq_remote_ref():
    config = AutomationConfig()

    with (
        patch("src.auto_coder.conflict_resolver.cmd") as mock_cmd,
        patch("src.auto_coder.conflict_resolver.GitHubClient") as mock_gh_client_class,
        patch("src.auto_coder.conflict_resolver.git_push") as mock_git_push,
    ):
        # Setup mocks
        mock_client = MagicMock()
        mock_gh_client_class.get_instance.return_value = mock_client
        mock_repo = MagicMock()
        mock_client.get_repository.return_value = mock_repo
        mock_pr = MagicMock()
        mock_repo.get_pull.return_value = mock_pr
        mock_client.get_pr_details.return_value = {"number": 123, "head_branch": "pr-branch", "author": {"login": "someone"}}

        # Sequence: reset, clean, abort, fetch pr (Step 1), checkout (Step 1), fetch origin base (Step 2), rev-parse (Step 3), merge (Step 3)
        mock_cmd.run_command.side_effect = [
            CommandResult(True, stdout="", stderr="", returncode=0),  # reset
            CommandResult(True, stdout="", stderr="", returncode=0),  # clean
            CommandResult(False, stdout="", stderr="", returncode=1),  # merge --abort
            CommandResult(True, stdout="", stderr="", returncode=0),  # fetch pr
            CommandResult(True, stdout="", stderr="", returncode=0),  # checkout pr
            CommandResult(True, stdout="", stderr="", returncode=0),  # fetch origin main
            CommandResult(True, stdout="abc123\n", stderr="", returncode=0),  # rev-parse
            CommandResult(True, stdout="", stderr="", returncode=0),  # merge
        ]

        mock_git_push.return_value = CommandResult(True, stdout="", stderr="", returncode=0)

        ok = _perform_base_branch_merge_and_conflict_resolution(
            pr_number=123,
            base_branch="main",
            config=config,
            repo_name="test/repo",
            pr_data={"number": 123},
        )

        assert ok is True
        # Validate that merge was called with a fully qualified remote ref
        calls = [c[0][0] for c in mock_cmd.run_command.call_args_list]
        assert any("refs/remotes/origin/main" in str(arg) for arg in calls)


def test_perform_base_merge_closes_jules_pr_on_degrade_with_linked_issues():
    """Test that Jules PR is closed on degradation even with linked issues."""
    with (
        patch("src.auto_coder.conflict_resolver.cmd") as mock_cmd,
        patch("src.auto_coder.conflict_resolver.GitHubClient") as mock_gh_client_class,
        patch("src.auto_coder.conflict_resolver.scan_conflict_markers") as mock_scan,
        patch("src.auto_coder.conflict_resolver.run_llm_noedit_prompt") as mock_llm,
        patch("src.auto_coder.conflict_resolver.create_high_score_backend_manager") as mock_create_backend,
        patch("src.auto_coder.conflict_resolver._archive_jules_session") as mock_archive,
    ):
        # Setup mocks
        mock_create_backend.return_value = None
        mock_client = MagicMock()
        mock_gh_client_class.get_instance.return_value = mock_client
        mock_repo = MagicMock()
        mock_client.get_repository.return_value = mock_repo
        mock_pr = MagicMock()
        mock_repo.get_pull.return_value = mock_pr
        mock_client.get_pr_details.return_value = {"number": 1253, "title": "Fix something", "body": "Fixes #123. Session ID: xyz", "author": {"login": "google-labs-jules"}, "head_branch": "jules-branch"}

        # Sequence: reset, clean, abort, fetch pr, checkout, fetch base, rev-parse, merge (fails)
        mock_cmd.run_command.side_effect = [
            CommandResult(True, "", "", 0),  # reset
            CommandResult(True, "", "", 0),  # clean
            CommandResult(True, "", "", 0),  # merge --abort
            CommandResult(True, "", "", 0),  # fetch pr
            CommandResult(True, "", "", 0),  # checkout pr
            CommandResult(True, "", "", 0),  # fetch origin main
            CommandResult(True, "abc123\n", "", 0),  # rev-parse
            CommandResult(False, "CONFLICT", "", 1),  # git merge fails
        ]

        mock_scan.return_value = ["file1.py"]
        mock_llm.return_value = "DEGRADING_MERGE"

        pr_data = {"number": 1253, "title": "Fix something", "body": "Fixes #123. Session ID: xyz", "author": {"login": "google-labs-jules"}, "baseRefName": "main"}

        ok = _perform_base_branch_merge_and_conflict_resolution(
            pr_number=1253,
            base_branch="main",
            config=AutomationConfig(),
            repo_name="test/repo",
            pr_data=pr_data,
        )

        assert ok is False
        mock_client.close_pr.assert_called_once()
        mock_archive.assert_called_once()


def test_perform_base_merge_enriches_pr_data_when_missing_fields():
    """Test that pr_data is enriched if author or body is missing."""
    config = AutomationConfig()

    with (
        patch("src.auto_coder.conflict_resolver.cmd") as mock_cmd,
        patch("src.auto_coder.conflict_resolver.GitHubClient") as mock_gh_client_class,
        patch("src.auto_coder.conflict_resolver.git_push") as mock_git_push,
    ):
        # Setup mocks
        mock_client = MagicMock()
        mock_gh_client_class.get_instance.return_value = mock_client
        mock_repo = MagicMock()
        mock_client.get_repository.return_value = mock_repo
        mock_pr = MagicMock()
        mock_repo.get_pull.return_value = mock_pr

        mock_client.get_pr_details.return_value = {"number": 1253, "author": {"login": "google-labs-jules"}, "body": "Fixes #123", "baseRefName": "main", "head_branch": "jules-fix"}

        mock_cmd.run_command.side_effect = [
            CommandResult(True, "", "", 0),  # reset
            CommandResult(True, "", "", 0),  # clean
            CommandResult(False, "", "", 1),  # merge --abort
            CommandResult(True, "", "", 0),  # fetch pr
            CommandResult(True, "", "", 0),  # checkout pr
            CommandResult(True, "", "", 0),  # fetch base
            CommandResult(True, "abc123\n", "", 0),  # rev-parse
            CommandResult(True, "", "", 0),  # merge
        ]

        mock_git_push.return_value = CommandResult(True, "", "", 0)

        pr_data = {"number": 1253}

        ok = _perform_base_branch_merge_and_conflict_resolution(
            pr_number=1253,
            base_branch="main",
            config=config,
            repo_name="test/repo",
            pr_data=pr_data,
        )

        assert ok is True
        mock_client.get_pr_details.assert_called()
