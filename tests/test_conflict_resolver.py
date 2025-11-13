from unittest.mock import MagicMock, patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.conflict_resolver import _perform_base_branch_merge_and_conflict_resolution
from src.auto_coder.utils import CommandResult


def test_perform_base_merge_uses_fq_remote_ref():
    config = AutomationConfig()

    with (
        patch("src.auto_coder.conflict_resolver.cmd") as mock_cmd,
        patch("src.auto_coder.conflict_resolver.get_gh_logger") as mock_get_gh_logger,
        patch("src.auto_coder.conflict_resolver.git_push") as mock_git_push,
    ):
        # gh pr checkout succeeds
        mock_gh = MagicMock()
        mock_gh.execute_with_logging.return_value = CommandResult(True, stdout="", stderr="", returncode=0)
        mock_get_gh_logger.return_value = mock_gh

        # Sequence: reset, clean, merge --abort, fetch, rev-parse fq ref OK, merge, push
        mock_cmd.run_command.side_effect = [
            CommandResult(True, stdout="", stderr="", returncode=0),  # reset
            CommandResult(True, stdout="", stderr="", returncode=0),  # clean
            CommandResult(False, stdout="", stderr="", returncode=1),  # merge --abort (no ongoing merge)
            CommandResult(True, stdout="", stderr="", returncode=0),  # fetch origin base
            CommandResult(True, stdout="abc123\n", stderr="", returncode=0),  # rev-parse refs/remotes/origin/main
            CommandResult(True, stdout="", stderr="", returncode=0),  # merge fq ref
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
        assert ["git", "fetch", "origin", "main"] in calls
        assert ["git", "rev-parse", "--verify", "refs/remotes/origin/main"] in calls
        assert ["git", "merge", "refs/remotes/origin/main"] in calls

