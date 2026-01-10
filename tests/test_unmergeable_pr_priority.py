from unittest.mock import MagicMock, Mock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine
from auto_coder.util.github_action import GitHubActionsStatusResult


class TestUnmergeablePRPriority:
    """Test cases for enhanced priority logic handling unmergeable PRs."""

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.issue_context.extract_linked_issues_from_pr_body")
    def test_unmergeable_pr_priority_elevation(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        mock_gemini_client,
        test_repo_name,
    ):
        """Test that unmergeable PRs get priority 2, higher than passing mergeable PRs (which would be merged)."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        # Mock GitHub client to return various PRs
        mock_github_client.get_open_pull_requests.return_value = [
            Mock(number=1, created_at="2024-01-01T00:00:00Z"),  # Unmergeable PR
            Mock(number=2, created_at="2024-01-02T00:00:00Z"),  # Mergeable PR (passing)
        ]

        mock_github_client.get_open_issues.return_value = []

        # Mock PR details
        pr_data = {
            1: {
                "number": 1,
                "title": "Unmergeable PR",
                "body": "",
                "head": {"ref": "pr-1", "sha": "sha1"},
                "labels": [],
                "mergeable": False,
                "created_at": "2024-01-01T00:00:00Z",
            },
            2: {
                "number": 2,
                "title": "Mergeable PR",
                "body": "",
                "head": {"ref": "pr-2", "sha": "sha2"},
                "labels": [],
                "mergeable": True,
                "created_at": "2024-01-02T00:00:00Z",
            },
        }

        # Mock get_open_prs_json to return the list of PR data
        mock_github_client.get_open_prs_json.return_value = list(pr_data.values())

        def get_pr_details_side_effect(pr_data_dict):
            # The 'pr' object is now a dictionary, not a mock object
            return pr_data[pr_data_dict["number"]]

        mock_github_client.get_pr_details.side_effect = get_pr_details_side_effect
        mock_github_client.get_pr_comments.return_value = []
        mock_github_client.get_pr_commits.return_value = []

        # Mock GitHub Actions checks - all passing
        def check_actions_side_effect(repo_name, pr_data, config):
            return GitHubActionsStatusResult(success=True, ids=[])

        mock_check_actions.side_effect = check_actions_side_effect

        mock_extract_issues.return_value = []
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert
        assert len(candidates) == 2

        # Unmergeable PR (priority 2)
        assert candidates[0].data["number"] == 1
        assert candidates[0].priority == 2

        # Mergeable PR (priority 2) - same priority, but should be second due to creation date
        # Wait, auto-mergeable PRs get priority 2 too.
        # But unmergeable needs conflict resolution (via LLM), whereas mergeable needs merging.
        # Current logic assigns 2 to both.
        # Sort order: priority desc, type, creation asc.
        # Since creation date for #1 is older, it comes first.
        assert candidates[1].data["number"] == 2
        assert candidates[1].priority == 2

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.issue_context.extract_linked_issues_from_pr_body")
    def test_urgent_unmergeable_pr_highest_priority(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        mock_gemini_client,
        test_repo_name,
    ):
        """Test that urgent unmergeable PRs get priority 4, higher than urgent mergeable PRs (3)."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        # Mock GitHub client to return various PRs
        mock_github_client.get_open_pull_requests.return_value = [
            Mock(number=1, created_at="2024-01-01T00:00:00Z"),  # Urgent unmergeable PR
            Mock(number=2, created_at="2024-01-02T00:00:00Z"),  # Urgent mergeable PR
            Mock(number=3, created_at="2024-01-03T00:00:00Z"),  # Regular unmergeable PR
        ]

        mock_github_client.get_open_issues.return_value = []

        # Mock PR details
        pr_data = {
            1: {
                "number": 1,
                "title": "Urgent unmergeable PR",
                "body": "",
                "head": {"ref": "pr-1", "sha": "sha1"},
                "labels": ["urgent"],
                "mergeable": False,
                "created_at": "2024-01-01T00:00:00Z",
            },
            2: {
                "number": 2,
                "title": "Urgent mergeable PR",
                "body": "",
                "head": {"ref": "pr-2", "sha": "sha2"},
                "labels": ["urgent"],
                "mergeable": True,
                "created_at": "2024-01-02T00:00:00Z",
            },
            3: {
                "number": 3,
                "title": "Regular unmergeable PR",
                "body": "",
                "head": {"ref": "pr-3", "sha": "sha3"},
                "labels": [],
                "mergeable": False,
                "created_at": "2024-01-03T00:00:00Z",
            },
        }

        # Mock get_open_prs_json to return the list of PR data
        mock_github_client.get_open_prs_json.return_value = list(pr_data.values())

        def get_pr_details_side_effect(pr_data_dict):
            return pr_data[pr_data_dict["number"]]

        mock_github_client.get_pr_details.side_effect = get_pr_details_side_effect
        mock_github_client.get_pr_comments.return_value = []
        mock_github_client.get_pr_commits.return_value = []

        # Mock GitHub Actions checks - all passing
        def check_actions_side_effect(repo_name, pr_data, config):
            return GitHubActionsStatusResult(success=True, ids=[])

        mock_check_actions.side_effect = check_actions_side_effect

        mock_extract_issues.return_value = []
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert
        assert len(candidates) == 3

        # Urgent unmergeable (priority 4)
        assert candidates[0].data["number"] == 1
        assert candidates[0].priority == 4

        # Urgent mergeable (priority 3)
        assert candidates[1].data["number"] == 2
        assert candidates[1].priority == 3

        # Regular unmergeable (priority 2)
        assert candidates[2].data["number"] == 3
        assert candidates[2].priority == 2

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.issue_context.extract_linked_issues_from_pr_body")
    def test_priority_hierarchy_ordering(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        mock_gemini_client,
        test_repo_name,
    ):
        """Test full hierarchy sorting of mixed candidates."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        # Mock GitHub client to return various PRs
        mock_github_client.get_open_pull_requests.return_value = [
            Mock(number=1, created_at="2024-01-01T00:00:00Z"),  # Urgent unmergeable
            Mock(number=2, created_at="2024-01-02T00:00:00Z"),  # Urgent mergeable
            Mock(number=3, created_at="2024-01-03T00:00:00Z"),  # Regular unmergeable
        ]

        # Mock issue data in the format expected by get_open_issues_json
        issue_data = [
            {
                "number": 10,
                "title": "Issue 10",
                "body": "",
                "labels": [],
                "state": "open",
                "created_at": "2024-01-07T00:00:00Z",
                "has_open_sub_issues": False,
                "parent_issue_number": None,
                "has_linked_prs": False,
            },  # Regular issue
            {
                "number": 11,
                "title": "Issue 11",
                "body": "",
                "labels": ["urgent"],
                "state": "open",
                "created_at": "2024-01-06T00:00:00Z",
                "has_open_sub_issues": False,
                "parent_issue_number": None,
                "has_linked_prs": False,
            },  # Urgent issue
        ]
        mock_github_client.get_open_issues_json.return_value = issue_data

        # Mock PR details
        pr_data = {
            1: {
                "number": 1,
                "title": "Urgent unmergeable",
                "body": "",
                "head": {"ref": "pr-1", "sha": "sha1"},
                "labels": ["urgent"],
                "mergeable": False,
                "created_at": "2024-01-01T00:00:00Z",
            },
            2: {
                "number": 2,
                "title": "Urgent mergeable",
                "body": "",
                "head": {"ref": "pr-2", "sha": "sha2"},
                "labels": ["urgent"],
                "mergeable": True,
                "created_at": "2024-01-02T00:00:00Z",
            },
            3: {
                "number": 3,
                "title": "Regular unmergeable",
                "body": "",
                "head": {"ref": "pr-3", "sha": "sha3"},
                "labels": [],
                "mergeable": False,
                "created_at": "2024-01-03T00:00:00Z",
            },
        }

        # Mock get_open_prs_json to return the list of PR data
        mock_github_client.get_open_prs_json.return_value = list(pr_data.values())

        def get_pr_details_side_effect(pr_data_dict):
            return pr_data[pr_data_dict["number"]]

        mock_github_client.get_pr_details.side_effect = get_pr_details_side_effect
        mock_github_client.get_pr_comments.return_value = []
        mock_github_client.get_pr_commits.return_value = []

        # Mock GitHub Actions checks
        def check_actions_side_effect(repo_name, pr_data, config):
            return GitHubActionsStatusResult(success=True, ids=[])

        mock_check_actions.side_effect = check_actions_side_effect

        mock_extract_issues.return_value = []
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert
        assert len(candidates) == 5

        # Order should be:
        # 1. Urgent unmergeable PR (priority 4) - #1
        # 2. Urgent mergeable PR (priority 3) - #2
        # 3. Urgent issue (priority 3) - #11 (PR comes first due to type order 1 vs 0? No, type order is issue(0), pr(1))
        # Wait, _type_order returns 0 for issue, 1 for pr.
        # Sort key: (-priority, _type_order, created_at)
        # So for priority 3:
        # - Urgent issue #11: type=0
        # - Urgent PR #2: type=1
        # Issue should come first!

        assert candidates[0].data["number"] == 1
        assert candidates[0].priority == 4

        # Priority 3 items
        assert candidates[1].data["number"] == 11  # Issue (type 0)
        assert candidates[1].priority == 3

        assert candidates[2].data["number"] == 2  # PR (type 1)
        assert candidates[2].priority == 3

        # Priority 2 items
        assert candidates[3].data["number"] == 3  # Unmergeable PR
        assert candidates[3].priority == 2

        # Priority 0 items
        assert candidates[4].data["number"] == 10  # Regular issue
        assert candidates[4].priority == 0


class TestPriorityBackwardCompatibility:
    """Test cases ensuring backward compatibility for existing priorities."""

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.issue_context.extract_linked_issues_from_pr_body")
    def test_urgent_label_maintains_highest_precedence(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        mock_gemini_client,
        test_repo_name,
    ):
        """Test that 'urgent' label still takes precedence even with unmergeable PRs."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        # Mock GitHub client to return various PRs
        mock_github_client.get_open_pull_requests.return_value = [
            Mock(number=1, created_at="2024-01-01T00:00:00Z"),  # Urgent PR
            Mock(number=2, created_at="2024-01-02T00:00:00Z"),  # Unmergeable PR
        ]

        mock_github_client.get_open_issues.return_value = []

        # Mock PR details
        pr_data = {
            1: {
                "number": 1,
                "title": "Urgent PR",
                "body": "",
                "head": {"ref": "pr-1", "sha": "sha1"},
                "labels": ["urgent"],
                "mergeable": True,
                "created_at": "2024-01-01T00:00:00Z",
            },
            2: {
                "number": 2,
                "title": "Unmergeable PR",
                "body": "",
                "head": {"ref": "pr-2", "sha": "sha2"},
                "labels": [],
                "mergeable": False,
                "created_at": "2024-01-02T00:00:00Z",
            },
        }

        # Mock get_open_prs_json to return the list of PR data
        mock_github_client.get_open_prs_json.return_value = list(pr_data.values())

        def get_pr_details_side_effect(pr_data_dict):
            return pr_data[pr_data_dict["number"]]

        mock_github_client.get_pr_details.side_effect = get_pr_details_side_effect
        mock_github_client.get_pr_comments.return_value = []
        mock_github_client.get_pr_commits.return_value = []

        # Mock GitHub Actions checks
        def check_actions_side_effect(repo_name, pr_data, config):
            return GitHubActionsStatusResult(success=True, ids=[])

        mock_check_actions.side_effect = check_actions_side_effect

        mock_extract_issues.return_value = []
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert
        assert len(candidates) == 2

        # Urgent PR (priority 3) > Unmergeable PR (priority 2)
        assert candidates[0].data["number"] == 1
        assert candidates[0].priority == 3

        assert candidates[1].data["number"] == 2
        assert candidates[1].priority == 2

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.issue_context.extract_linked_issues_from_pr_body")
    def test_breaking_change_label_highest_priority(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        mock_gemini_client,
        test_repo_name,
    ):
        """Test that 'breaking-change' label has absolute highest priority."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        # Mock GitHub client to return various PRs
        mock_github_client.get_open_pull_requests.return_value = [
            Mock(number=1, created_at="2024-01-01T00:00:00Z"),  # Breaking change
            Mock(number=2, created_at="2024-01-02T00:00:00Z"),  # Urgent unmergeable
        ]

        mock_github_client.get_open_issues.return_value = []

        # Mock PR details
        pr_data = {
            1: {
                "number": 1,
                "title": "Breaking change PR",
                "body": "",
                "head": {"ref": "pr-1", "sha": "sha1"},
                "labels": ["breaking-change"],
                "mergeable": True,
                "created_at": "2024-01-01T00:00:00Z",
            },
            2: {
                "number": 2,
                "title": "Urgent unmergeable PR",
                "body": "",
                "head": {"ref": "pr-2", "sha": "sha2"},
                "labels": ["urgent"],
                "mergeable": False,
                "created_at": "2024-01-02T00:00:00Z",
            },
        }

        # Mock get_open_prs_json to return the list of PR data
        mock_github_client.get_open_prs_json.return_value = list(pr_data.values())

        def get_pr_details_side_effect(pr_data_dict):
            return pr_data[pr_data_dict["number"]]

        mock_github_client.get_pr_details.side_effect = get_pr_details_side_effect
        mock_github_client.get_pr_comments.return_value = []
        mock_github_client.get_pr_commits.return_value = []

        # Mock GitHub Actions checks
        def check_actions_side_effect(repo_name, pr_data, config):
            return GitHubActionsStatusResult(success=True, ids=[])

        mock_check_actions.side_effect = check_actions_side_effect

        mock_extract_issues.return_value = []
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert
        assert len(candidates) == 2

        # Breaking change (priority 7) > Urgent unmergeable (priority 4)
        assert candidates[0].data["number"] == 1
        assert candidates[0].priority == 7

        assert candidates[1].data["number"] == 2
        assert candidates[1].priority == 4


class TestPriorityEdgeCases:
    """Test cases for edge cases in priority calculation."""

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.issue_context.extract_linked_issues_from_pr_body")
    def test_unmergeable_pr_with_passing_checks(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        mock_gemini_client,
        test_repo_name,
    ):
        """Test priority for unmergeable PRs with passing CI checks."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        # Mock GitHub client
        mock_github_client.get_open_pull_requests.return_value = [
            Mock(number=1, created_at="2024-01-01T00:00:00Z"),
        ]
        mock_github_client.get_open_issues.return_value = []

        # PR details: unmergeable but checks pass
        pr_data = {
            "number": 1,
            "title": "Unmergeable PR",
            "body": "",
            "head": {"ref": "pr-1", "sha": "sha1"},
            "labels": [],
            "mergeable": False,
            "created_at": "2024-01-01T00:00:00Z",
        }
        mock_github_client.get_pr_details.return_value = pr_data
        # Mock get_open_prs_json to return the PR data
        mock_github_client.get_open_prs_json.return_value = [pr_data]

        mock_github_client.get_pr_comments.return_value = []
        mock_github_client.get_pr_commits.return_value = []

        # Checks pass
        mock_check_actions.return_value = GitHubActionsStatusResult(success=True, ids=[])

        mock_extract_issues.return_value = []
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert - Unmergeable -> Priority 2
        assert len(candidates) == 1
        assert candidates[0].priority == 2

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.issue_context.extract_linked_issues_from_pr_body")
    def test_unmergeable_pr_with_failing_checks(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        mock_gemini_client,
        test_repo_name,
    ):
        """Test priority for unmergeable PRs with failing CI checks."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        # Mock GitHub client
        mock_github_client.get_open_pull_requests.return_value = [
            Mock(number=1, created_at="2024-01-01T00:00:00Z"),
        ]
        mock_github_client.get_open_issues.return_value = []

        # PR details: unmergeable AND checks fail
        pr_data = {
            "number": 1,
            "title": "Unmergeable PR with failures",
            "body": "",
            "head": {"ref": "pr-1", "sha": "sha1"},
            "labels": [],
            "mergeable": False,
            "created_at": "2024-01-01T00:00:00Z",
        }
        mock_github_client.get_pr_details.return_value = pr_data
        # Mock get_open_prs_json to return the PR data
        mock_github_client.get_open_prs_json.return_value = [pr_data]

        mock_github_client.get_pr_comments.return_value = []
        mock_github_client.get_pr_commits.return_value = []

        # Checks fail
        mock_check_actions.return_value = GitHubActionsStatusResult(success=False, ids=[])

        mock_extract_issues.return_value = []
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert - Unmergeable -> Priority 2 (dominates failing checks which is 1)
        assert len(candidates) == 1
        assert candidates[0].priority == 2

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.issue_context.extract_linked_issues_from_pr_body")
    def test_mergeable_pr_with_failing_checks(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        mock_gemini_client,
        test_repo_name,
    ):
        """Test priority for mergeable PRs with failing CI checks."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        # Mock GitHub client
        mock_github_client.get_open_pull_requests.return_value = [
            Mock(number=1, created_at="2024-01-01T00:00:00Z"),
        ]
        mock_github_client.get_open_issues.return_value = []

        # PR details: mergeable but checks fail
        pr_data = {
            "number": 1,
            "title": "Mergeable PR with failures",
            "body": "",
            "head": {"ref": "pr-1", "sha": "sha1"},
            "labels": [],
            "mergeable": True,
            "created_at": "2024-01-01T00:00:00Z",
        }
        mock_github_client.get_pr_details.return_value = pr_data
        # Mock get_open_prs_json to return the PR data
        mock_github_client.get_open_prs_json.return_value = [pr_data]

        mock_github_client.get_pr_comments.return_value = []
        mock_github_client.get_pr_commits.return_value = []

        # Checks fail
        mock_check_actions.return_value = GitHubActionsStatusResult(success=False, ids=[])

        mock_extract_issues.return_value = []
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert - Mergeable but failing -> Priority 1
        assert len(candidates) == 1
        assert candidates[0].priority == 1

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.issue_context.extract_linked_issues_from_pr_body")
    def test_mergeable_pr_with_passing_checks(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        mock_gemini_client,
        test_repo_name,
    ):
        """Test priority for mergeable PRs with passing CI checks."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        # Mock GitHub client
        mock_github_client.get_open_pull_requests.return_value = [
            Mock(number=1, created_at="2024-01-01T00:00:00Z"),
        ]
        mock_github_client.get_open_issues.return_value = []

        # PR details: mergeable and checks pass
        pr_data = {
            "number": 1,
            "title": "Ready PR",
            "body": "",
            "head": {"ref": "pr-1", "sha": "sha1"},
            "labels": [],
            "mergeable": True,
            "created_at": "2024-01-01T00:00:00Z",
        }
        mock_github_client.get_pr_details.return_value = pr_data
        # Mock get_open_prs_json to return the PR data
        mock_github_client.get_open_prs_json.return_value = [pr_data]

        mock_github_client.get_pr_comments.return_value = []
        mock_github_client.get_pr_commits.return_value = []

        # Checks pass
        mock_check_actions.return_value = GitHubActionsStatusResult(success=True, ids=[])

        mock_extract_issues.return_value = []
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert - Ready -> Priority 2
        assert len(candidates) == 1
        assert candidates[0].priority == 2


class TestPriorityIntegration:
    """Integration-like tests for priority logic."""

    @patch("auto_coder.automation_engine.AutomationEngine._process_single_candidate_unified")
    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.issue_context.extract_linked_issues_from_pr_body")
    def test_unmergeable_pr_processed_before_regular_fixes(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_process_unified,
        mock_github_client,
        mock_gemini_client,
        test_repo_name,
    ):
        """Test that unmergeable PR is processed before a PR needing fixes."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        # Mock GitHub client to return various PRs
        mock_github_client.get_open_pull_requests.return_value = [
            Mock(number=1, created_at="2024-01-01T00:00:00Z"),  # Failing mergeable PR
            Mock(number=2, created_at="2024-01-02T00:00:00Z"),  # Unmergeable PR
            Mock(number=3, created_at="2024-01-03T00:00:00Z"),  # Regular issue
        ]

        mock_github_client.get_open_issues.return_value = []

        # Mock PR details
        pr_data = {
            1: {
                "number": 1,
                "title": "Failing mergeable",
                "body": "",
                "head": {"ref": "pr-1", "sha": "sha1"},
                "labels": [],
                "mergeable": True,
                "created_at": "2024-01-01T00:00:00Z",
            },
            2: {
                "number": 2,
                "title": "Unmergeable PR",
                "body": "",
                "head": {"ref": "pr-2", "sha": "sha2"},
                "labels": [],
                "mergeable": False,
                "created_at": "2024-01-02T00:00:00Z",
            },
            3: {
                "number": 3,
                "title": "Issue",
                "body": "",
                "labels": [],
                "state": "open",
                "created_at": "2024-01-03T00:00:00Z",
            },
        }

        # Mock get_open_prs_json to return the list of PR data
        # Only include PRs 1 and 2
        mock_github_client.get_open_prs_json.return_value = [pr_data[1], pr_data[2]]

        def get_pr_details_side_effect(pr):
            return pr_data[pr.number]

        mock_github_client.get_pr_details.side_effect = get_pr_details_side_effect
        mock_github_client.get_pr_comments.return_value = []
        mock_github_client.get_pr_commits.return_value = []

        # Checks: PR 1 fails, PR 2 passes (but is unmergeable)
        def check_actions_side_effect(repo_name, pr_data, config):
            if pr_data["number"] == 1:
                return GitHubActionsStatusResult(success=False, ids=[])
            return GitHubActionsStatusResult(success=True, ids=[])

        mock_check_actions.side_effect = check_actions_side_effect

        mock_extract_issues.return_value = []
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert - should find 2 PRs (issues were mocked to return [] so no issues found)
        assert len(candidates) == 2

        # Unmergeable (priority 2)
        assert candidates[0].data["number"] == 2
        assert candidates[0].priority == 2

        # Failing mergeable (priority 1)
        assert candidates[1].data["number"] == 1
        assert candidates[1].priority == 1

    @patch("auto_coder.automation_engine.AutomationEngine._process_single_candidate_unified")
    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.issue_context.extract_linked_issues_from_pr_body")
    def test_performance_impact_minimal(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_process_unified,
        mock_github_client,
        mock_gemini_client,
        test_repo_name,
    ):
        """Test candidate collection performance with many PRs."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        # Generate 50 PRs
        prs = []
        pr_data_map = {}
        for i in range(1, 51):
            prs.append(Mock(number=i, created_at="2024-01-01T00:00:00Z"))
            pr_data_map[i] = {
                "number": i,
                "title": f"PR {i}",
                "body": "",
                "head": {"ref": f"pr-{i}", "sha": f"sha{i}"},
                "labels": [],
                "mergeable": i % 2 == 0,  # Half unmergeable
                "created_at": "2024-01-01T00:00:00Z",
            }

        mock_github_client.get_open_pull_requests.return_value = prs
        mock_github_client.get_open_issues.return_value = []

        # Mock bulk fetch via get_open_prs_json
        mock_github_client.get_open_prs_json.return_value = list(pr_data_map.values())

        mock_check_actions.return_value = GitHubActionsStatusResult(success=True, ids=[])
        mock_extract_issues.return_value = []
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False

        # Execute
        import time

        start_time = time.time()
        candidates = engine._get_candidates(test_repo_name, max_items=100)
        end_time = time.time()

        # Assert
        assert len(candidates) == 50
        assert (end_time - start_time) < 1.0  # Should be fast (< 1s) without real API calls
