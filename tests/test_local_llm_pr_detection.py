"""Unit tests for local LLM PR identification and fix limitation."""

from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.conflict_resolver import _perform_base_branch_merge_and_conflict_resolution
from auto_coder.github_app_reviewer import ReviewPublicationResult
from auto_coder.issue_processor import _create_pr_for_issue
from auto_coder.pr_processor import _handle_pr_merge, _is_local_llm_pr, is_local_llm_pr
from auto_coder.util.github_action import DetailedChecksResult, GitHubActionsStatusResult


class TestIsLocalLLMPrDetection:
    """Tests for _is_local_llm_pr detection logic."""

    def test_local_marker_detection(self):
        """PR with explicit local LLM marker is identified as local LLM PR."""
        pr_data_1 = {
            "number": 1,
            "body": "<!-- auto-coder:local-llm -->\n\nCloses #10\nFix something",
            "head": {"ref": "some-feature-branch"},
        }
        assert _is_local_llm_pr(pr_data_1) is True
        assert is_local_llm_pr(pr_data_1) is True

        pr_data_2 = {
            "number": 2,
            "body": "Fix something\n\n<!-- auto-coder:local -->",
            "head": {"ref": "custom-branch"},
        }
        assert _is_local_llm_pr(pr_data_2) is True

    def test_branch_name_detection(self):
        """PR with work branch convention is identified as local LLM PR."""
        # issue-123
        pr_1 = {"number": 1, "body": "Fix bug", "head": {"ref": "issue-123"}}
        assert _is_local_llm_pr(pr_1) is True

        # issue-123_attempt-2
        pr_2 = {"number": 2, "body": "Fix bug", "head_branch": "issue-123_attempt-2"}
        assert _is_local_llm_pr(pr_2) is True

        # issue-123/attempt-1 (legacy format)
        pr_3 = {"number": 3, "body": "Fix bug", "head": {"ref": "issue-123/attempt-1"}}
        assert _is_local_llm_pr(pr_3) is True

    def test_body_signature_detection(self):
        """PR with standard Auto-Coder text signatures is identified as local LLM PR."""
        pr_1 = {
            "number": 1,
            "body": "Closes #42\n\nThis PR addresses issue #42.\n\nSummary of changes",
            "head": {"ref": "feature-xyz"},
        }
        assert _is_local_llm_pr(pr_1) is True

        pr_2 = {
            "number": 2,
            "body": "Auto-Coder: Address issue #55",
            "head": {"ref": "feature-abc"},
        }
        assert _is_local_llm_pr(pr_2) is True

    def test_cloud_llms_and_bots_excluded(self):
        """Jules, Codex Cloud, Claude Routine, and Dependabot PRs are excluded."""
        # Jules PR
        jules_pr = {
            "number": 1,
            "body": "Jules session active\nhttps://jules.google.com/session/123",
            "user": {"login": "google-labs-jules[bot]"},
            "head": {"ref": "issue-10"},  # even if branch matches
        }
        assert _is_local_llm_pr(jules_pr) is False

        # Codex Cloud PR
        codex_pr = {
            "number": 2,
            "body": "Closes #10\n\nhttps://chatgpt.com/codex/tasks/task_12345",
            "user": {"login": "octocat"},
            "head": {"ref": "issue-10"},
        }
        assert _is_local_llm_pr(codex_pr) is False

        # Claude Routine PR
        claude_pr = {
            "number": 3,
            "body": "Closes #11\n\nhttps://claude.ai/code/session_123",
            "user": {"login": "octocat"},
            "head": {"ref": "issue-11"},
        }
        assert _is_local_llm_pr(claude_pr) is False

        # Dependabot PR
        dependabot_pr = {
            "number": 4,
            "body": "Bump lodash from 4.17.15 to 4.17.21",
            "user": {"login": "dependabot[bot]"},
            "head": {"ref": "dependabot/npm_and_yarn/lodash-4.17.21"},
        }
        assert _is_local_llm_pr(dependabot_pr) is False

    def test_external_human_pr_excluded(self):
        """Normal external PRs without local Auto-Coder markers are excluded."""
        external_pr = {
            "number": 1,
            "body": "Refactor user authentication module",
            "user": {"login": "some-contributor"},
            "head": {"ref": "refactor-auth"},
        }
        assert _is_local_llm_pr(external_pr) is False

    def test_empty_pr_data(self):
        """Empty pr_data returns False."""
        assert _is_local_llm_pr({}) is False


class TestCreatePrForIssueMarker:
    """Tests that _create_pr_for_issue injects the local LLM marker."""

    def test_create_pr_contains_local_marker(self):
        mock_gh = MagicMock()
        mock_gh.token = "test-token"
        mock_gh.find_pr_by_head_branch.return_value = None
        mock_gh.get_pr_closing_issues.return_value = [10]

        mock_api = MagicMock()
        mock_api.pulls.create.return_value = {"number": 101, "html_url": "https://github.com/owner/repo/pull/101"}

        config = AutomationConfig()
        config.PR_LABEL_COPYING_ENABLED = False

        with (
            patch("auto_coder.issue_processor.get_commit_log", return_value=""),
            patch("auto_coder.issue_processor.run_llm_noedit_prompt", side_effect=RuntimeError("No test backend configured")),
            patch("auto_coder.issue_processor.get_ghapi_client", return_value=mock_api),
            patch("auto_coder.issue_processor.validate_issue_references"),
        ):
            res = _create_pr_for_issue(
                repo_name="owner/repo",
                issue_data={"number": 10, "title": "Test Issue", "body": "Issue details"},
                work_branch="issue-10",
                base_branch="main",
                llm_response="Fixed the issue",
                github_client=mock_gh,
                config=config,
            )
            assert "Successfully created PR for issue #10" in res

            # Verify body passed to api.pulls.create contains local LLM marker
            call_kwargs = mock_api.pulls.create.call_args[1]
            pr_body = call_kwargs.get("body", "")
            assert "<!-- auto-coder:local-llm -->" in pr_body
            assert "Closes #10" in pr_body


class TestHandlePrMergeFixLimitation:
    """Tests that _handle_pr_merge restricts local LLM fixes to local LLM PRs."""

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True})
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.publish_adversarial_review", return_value=ReviewPublicationResult(True, "APPROVE", ""))
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor._merge_pr", return_value=True)
    def test_clean_pr_merges_regardless_of_pr_type(self, mock_merge, mock_worktree, mock_publish, mock_val, mock_mergeable, mock_exit_check):
        from auto_coder.adversarial_validator import AdversarialValidationResult

        mock_val.return_value = AdversarialValidationResult(result="PASS", summary="Pass", findings=[])
        config = AutomationConfig()
        config.AUTO_MERGE = True
        github_client = MagicMock()

        # 1. Local PR with passing checks
        local_pr = {
            "number": 101,
            "body": "<!-- auto-coder:local-llm -->\nCloses #10",
            "head": {"ref": "issue-10", "sha": "sha_101"},
        }
        github_client.get_pull_request.return_value = {"head": {"sha": "sha_101"}}
        with patch("auto_coder.pr_processor._check_github_actions_status", return_value=GitHubActionsStatusResult(success=True, ids=[1])):
            actions = _handle_pr_merge(github_client, "owner/repo", local_pr, config, {})
            assert any("Successfully merged PR #101" in a for a in actions)
            mock_merge.assert_called_once()

        mock_merge.reset_mock()

        # 2. Codex Cloud PR with passing checks
        codex_pr = {
            "number": 102,
            "body": "https://chatgpt.com/codex/tasks/task_123",
            "head": {"ref": "codex-branch", "sha": "sha_102"},
        }
        github_client.get_pull_request.return_value = {"head": {"sha": "sha_102"}}
        with patch("auto_coder.pr_processor._check_github_actions_status", return_value=GitHubActionsStatusResult(success=True, ids=[2])):
            actions = _handle_pr_merge(github_client, "owner/repo", codex_pr, config, {})
            assert any("Successfully merged PR #102" in a for a in actions)
            mock_merge.assert_called_once()

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True})
    @patch("auto_coder.pr_processor.cmd.run_command")
    @patch("auto_coder.pr_processor._checkout_pr_branch", return_value=True)
    @patch("auto_coder.pr_processor.BranchManager")
    @patch("auto_coder.pr_processor._fix_pr_issues_with_testing", return_value=["Fixed PR locally"])
    def test_ci_failure_runs_local_fix_for_local_pr(self, mock_fix, mock_bm, mock_checkout, mock_cmd, mock_mergeable, mock_exit_check):
        """Local PR with CI failure triggers local LLM fix loop."""
        config = AutomationConfig()
        config.SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL = True
        github_client = MagicMock()

        local_pr = {
            "number": 101,
            "body": "<!-- auto-coder:local-llm -->\nCloses #10",
            "head": {"ref": "issue-10"},
        }

        mock_cmd.return_value = MagicMock(success=True, stdout="main\n")
        mock_bm.return_value.__enter__.return_value = MagicMock()

        with (
            patch("auto_coder.pr_processor._check_github_actions_status", return_value=GitHubActionsStatusResult(success=False, ids=[1])),
            patch("auto_coder.pr_processor.get_detailed_checks_from_history", return_value=DetailedChecksResult(success=False, failed_checks=["test_failed"])),
            patch("auto_coder.pr_processor._create_github_action_log_summary", return_value=("Log summary", ["test_file.py"])),
        ):
            actions = _handle_pr_merge(github_client, "owner/repo", local_pr, config, {})
            assert any("Fixed PR locally" in a for a in actions)
            mock_fix.assert_called_once()

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True})
    @patch("auto_coder.pr_processor.cmd.run_command")
    @patch("auto_coder.pr_processor._fix_pr_issues_with_testing")
    def test_ci_failure_skips_local_fix_for_non_local_pr(self, mock_fix, mock_cmd, mock_mergeable, mock_exit_check):
        """Non-local PR with CI failure skips local LLM fix loop."""
        config = AutomationConfig()
        github_client = MagicMock()

        non_local_pr = {
            "number": 102,
            "body": "Implementation by contributor",
            "user": {"login": "external-dev"},
            "head": {"ref": "feature-external"},
        }

        mock_cmd.return_value = MagicMock(success=True, stdout="main\n")

        with patch("auto_coder.pr_processor._check_github_actions_status", return_value=GitHubActionsStatusResult(success=False, ids=[1])), patch("auto_coder.pr_processor.get_detailed_checks_from_history", return_value=DetailedChecksResult(success=False, failed_checks=["test_failed"])):
            actions = _handle_pr_merge(github_client, "owner/repo", non_local_pr, config, {})
            assert any("was not created by local LLM, skipping local LLM fixes" in a for a in actions)
            mock_fix.assert_not_called()


class TestConflictResolutionFixLimitation:
    """Tests that conflict resolution skips LLM resolution for non-local PRs."""

    @patch("auto_coder.conflict_resolver.cmd.run_command")
    @patch("auto_coder.conflict_resolver.scan_conflict_markers", return_value=["file1.py"])
    @patch("auto_coder.conflict_resolver.check_mergeability_with_llm")
    def test_conflict_skips_llm_for_non_local_pr(self, mock_mergeability, mock_scan, mock_cmd):
        """Non-local PR with conflict skips LLM mergeability check and resolution."""
        config = AutomationConfig()
        pr_data = {
            "number": 201,
            "body": "https://chatgpt.com/codex/tasks/task_999",
            "head": {"ref": "codex-branch"},
        }

        # Mock git commands: reset/clean/abort ok, merge fails (conflict)
        mock_cmd.side_effect = lambda args: MagicMock(
            success=(args[0] == "git" and args[1] != "merge"),
            returncode=(0 if args[0] == "git" and args[1] != "merge" else 1),
            stdout="",
            stderr="",
        )

        resolved = _perform_base_branch_merge_and_conflict_resolution(
            pr_number=201,
            base_branch="main",
            config=config,
            pr_data=pr_data,
            repo_name="owner/repo",
        )

        assert resolved is False
        mock_mergeability.assert_not_called()
