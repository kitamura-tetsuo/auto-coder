"""
Tests for unmergeable PR priority enhancement.

This test module validates the enhanced priority system that gives unmergeable PRs
higher priority for conflict resolution while maintaining urgent label precedence.
"""

from unittest.mock import Mock, patch

import pytest

from auto_coder.automation_config import AutomationConfig, Candidate
from auto_coder.automation_engine import AutomationEngine
from auto_coder.util.github_action import GitHubActionsStatusResult


class TestUnmergeablePRPriority:
    """Test cases for unmergeable PR priority elevation."""

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.pr_processor._extract_linked_issues_from_pr_body")
    def test_unmergeable_pr_priority_elevation(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        test_repo_name,
    ):
        """Test that unmergeable PRs get higher priority than regular fix-required PRs."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        # Create two PRs: one unmergeable with passing checks, one mergeable with failing checks
        mock_github_client.get_open_pull_requests.return_value = [
            Mock(number=1, created_at="2024-01-01T00:00:00Z"),  # Unmergeable PR
            Mock(number=2, created_at="2024-01-02T00:00:00Z"),  # Regular fix PR
        ]
        mock_github_client.get_open_issues.return_value = []

        pr_data = {
            1: {
                "number": 1,
                "title": "Unmergeable PR",
                "body": "",
                "head": {"ref": "pr-1"},
                "labels": [],
                "mergeable": False,  # Unmergeable (merge conflicts)
                "created_at": "2024-01-01T00:00:00Z",
            },
            2: {
                "number": 2,
                "title": "Regular fix PR",
                "body": "",
                "head": {"ref": "pr-2"},
                "labels": [],
                "mergeable": True,  # Mergeable but has failing checks
                "created_at": "2024-01-02T00:00:00Z",
            },
        }

        def get_pr_details_side_effect(pr):
            return pr_data[pr.number]

        mock_github_client.get_pr_details.side_effect = get_pr_details_side_effect

        # Mock GitHub Actions checks
        def check_actions_side_effect(repo_name, pr_details, config):
            if pr_details["number"] == 1:
                # Unmergeable PR has passing checks
                return GitHubActionsStatusResult(success=True, ids=[], in_progress=False)
            elif pr_details["number"] == 2:
                # Regular fix PR has failing checks
                return GitHubActionsStatusResult(success=False, ids=[], in_progress=False)
            return GitHubActionsStatusResult(success=True, ids=[], in_progress=False)

        mock_check_actions.side_effect = check_actions_side_effect
        mock_extract_issues.return_value = []
        mock_github_client.check_should_process_with_label_manager.return_value = True

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert - Unmergeable PR should get priority 2, regular fix PR should get priority 1
        assert len(candidates) == 2

        # Find candidates by PR number
        unmergeable_candidate = next(c for c in candidates if c.data["number"] == 1)
        regular_fix_candidate = next(c for c in candidates if c.data["number"] == 2)

        # Verify unmergeable PR gets priority 2 (higher than regular fix priority 1)
        assert unmergeable_candidate.priority == 2
        assert regular_fix_candidate.priority == 1

        # Verify unmergeable PR is processed first (sorted by priority descending)
        assert candidates[0].data["number"] == 1  # Unmergeable PR first
        assert candidates[1].data["number"] == 2  # Regular fix PR second

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.pr_processor._extract_linked_issues_from_pr_body")
    def test_urgent_unmergeable_pr_highest_priority(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        test_repo_name,
    ):
        """Test that urgent unmergeable PRs get the highest priority."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        mock_github_client.get_open_pull_requests.return_value = [
            Mock(number=1, created_at="2024-01-01T00:00:00Z"),  # Urgent unmergeable PR
            Mock(number=2, created_at="2024-01-02T00:00:00Z"),  # Urgent mergeable PR with failing checks
            Mock(number=3, created_at="2024-01-03T00:00:00Z"),  # Regular unmergeable PR
        ]
        mock_github_client.get_open_issues.return_value = []

        pr_data = {
            1: {
                "number": 1,
                "title": "Urgent unmergeable PR",
                "body": "",
                "head": {"ref": "pr-1"},
                "labels": ["urgent"],
                "mergeable": False,
                "created_at": "2024-01-01T00:00:00Z",
            },
            2: {
                "number": 2,
                "title": "Urgent fix PR",
                "body": "",
                "head": {"ref": "pr-2"},
                "labels": ["urgent"],
                "mergeable": True,
                "created_at": "2024-01-02T00:00:00Z",
            },
            3: {
                "number": 3,
                "title": "Regular unmergeable PR",
                "body": "",
                "head": {"ref": "pr-3"},
                "labels": [],
                "mergeable": False,
                "created_at": "2024-01-03T00:00:00Z",
            },
        }

        def get_pr_details_side_effect(pr):
            return pr_data[pr.number]

        mock_github_client.get_pr_details.side_effect = get_pr_details_side_effect

        def check_actions_side_effect(repo_name, pr_details, config):
            if pr_details["number"] == 2:
                return GitHubActionsStatusResult(success=False, ids=[], in_progress=False)
            return GitHubActionsStatusResult(success=True, ids=[], in_progress=False)

        mock_check_actions.side_effect = check_actions_side_effect
        mock_extract_issues.return_value = []
        mock_github_client.check_should_process_with_label_manager.return_value = True

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert - Verify priority hierarchy
        assert len(candidates) == 3

        # Find candidates by PR number
        urgent_unmergeable = next(c for c in candidates if c.data["number"] == 1)
        urgent_fix = next(c for c in candidates if c.data["number"] == 2)
        regular_unmergeable = next(c for c in candidates if c.data["number"] == 3)

        # Verify urgent unmergeable PR gets priority 4
        assert urgent_unmergeable.priority == 4

        # Verify urgent fix PR gets priority 3 (urgent + mergeable, failing checks)
        assert urgent_fix.priority == 3

        # Verify regular unmergeable PR gets priority 2
        assert regular_unmergeable.priority == 2

        # Verify sorting order: highest priority first
        assert candidates[0].data["number"] == 1  # Urgent unmergeable (priority 4)
        assert candidates[1].data["number"] == 2  # Urgent fix (priority 3)
        assert candidates[2].data["number"] == 3  # Regular unmergeable (priority 2)

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.pr_processor._extract_linked_issues_from_pr_body")
    def test_priority_hierarchy_ordering(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        test_repo_name,
    ):
        """Test complete priority hierarchy ordering."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        # Create all combinations of PR states
        # Note: Issues are only collected if PR candidates < 5, so we use fewer PRs
        mock_github_client.get_open_pull_requests.return_value = [
            Mock(number=1, created_at="2024-01-01T00:00:00Z"),  # Regular fix-required PR
            Mock(number=2, created_at="2024-01-02T00:00:00Z"),  # Unmergeable PR
            Mock(number=3, created_at="2024-01-03T00:00:00Z"),  # Ready to merge PR
        ]

        mock_github_client.get_open_issues.return_value = [
            Mock(number=10, created_at="2024-01-07T00:00:00Z"),  # Regular issue
            Mock(number=11, created_at="2024-01-08T00:00:00Z"),  # Urgent issue
        ]

        pr_data = {
            1: {
                "number": 1,
                "title": "Fix-required PR",
                "body": "",
                "head": {"ref": "pr-1"},
                "labels": [],
                "mergeable": True,
                "created_at": "2024-01-01T00:00:00Z",
            },
            2: {
                "number": 2,
                "title": "Unmergeable PR",
                "body": "",
                "head": {"ref": "pr-2"},
                "labels": [],
                "mergeable": False,
                "created_at": "2024-01-02T00:00:00Z",
            },
            3: {
                "number": 3,
                "title": "Ready to merge PR",
                "body": "",
                "head": {"ref": "pr-3"},
                "labels": [],
                "mergeable": True,
                "created_at": "2024-01-03T00:00:00Z",
            },
        }

        def get_pr_details_side_effect(pr):
            return pr_data[pr.number]

        def get_issue_details_side_effect(issue):
            return {
                "number": issue.number,
                "title": "Issue",
                "body": "",
                "labels": ["urgent"] if issue.number == 11 else [],
                "state": "open",
                "created_at": issue.created_at,
            }

        mock_github_client.get_pr_details.side_effect = get_pr_details_side_effect
        mock_github_client.get_issue_details.side_effect = get_issue_details_side_effect

        def check_actions_side_effect(repo_name, pr_details, config):
            # PR 1 has failing checks; PR 2 is unmergeable but checks pass; PR 3 is ready
            if pr_details["number"] == 1:
                return GitHubActionsStatusResult(success=False, ids=[], in_progress=False)
            return GitHubActionsStatusResult(success=True, ids=[], in_progress=False)

        mock_check_actions.side_effect = check_actions_side_effect
        mock_extract_issues.return_value = []
        mock_github_client.get_open_sub_issues.return_value = []
        mock_github_client.has_linked_pr.return_value = False
        mock_github_client.check_should_process_with_label_manager.return_value = True

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert - Verify sorting order by priority (highest first)
        assert len(candidates) == 5

        # Expected priority order (highest to lowest):
        # 1. Urgent issue #11 (priority 3)
        # 2. Ready to merge PR #3 (priority 2)
        # 3. Unmergeable PR #2 (priority 2, older than PR #3)
        # 4. Fix-required PR #1 (priority 1)
        # 5. Regular issue #10 (priority 0)
        # Note: Same priority items are sorted by creation time (oldest first)

        expected_order = [11, 2, 3, 1, 10]
        actual_order = [c.data["number"] for c in candidates]
        assert actual_order == expected_order

        # Verify priorities
        priorities = {c.data["number"]: c.priority for c in candidates}
        assert priorities[3] == 2  # Ready to merge (mergeable with passing checks)
        assert priorities[2] == 2  # Unmergeable
        assert priorities[1] == 1  # Fix-required
        assert priorities[11] == 3  # Urgent issue
        assert priorities[10] == 0  # Regular issue


class TestPriorityBackwardCompatibility:
    """Test cases to ensure backward compatibility of existing priority behaviors."""

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.pr_processor._extract_linked_issues_from_pr_body")
    def test_urgent_label_maintains_highest_precedence(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        test_repo_name,
    ):
        """Test that urgent labels maintain highest priority regardless of mergeable status."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        mock_github_client.get_open_pull_requests.return_value = [
            Mock(number=1, created_at="2024-01-01T00:00:00Z"),  # Urgent PR
            Mock(number=2, created_at="2024-01-02T00:00:00Z"),  # Ready to merge PR (no urgent)
        ]
        mock_github_client.get_open_issues.return_value = []

        pr_data = {
            1: {
                "number": 1,
                "title": "Urgent PR",
                "body": "",
                "head": {"ref": "pr-1"},
                "labels": ["urgent"],
                "mergeable": True,
                "created_at": "2024-01-01T00:00:00Z",
            },
            2: {
                "number": 2,
                "title": "Ready PR",
                "body": "",
                "head": {"ref": "pr-2"},
                "labels": [],
                "mergeable": True,
                "created_at": "2024-01-02T00:00:00Z",
            },
        }

        def get_pr_details_side_effect(pr):
            return pr_data[pr.number]

        mock_github_client.get_pr_details.side_effect = get_pr_details_side_effect

        def check_actions_side_effect(repo_name, pr_details, config):
            if pr_details["number"] == 1:
                return GitHubActionsStatusResult(success=False, ids=[], in_progress=False)
            return GitHubActionsStatusResult(success=True, ids=[], in_progress=False)

        mock_check_actions.side_effect = check_actions_side_effect
        mock_extract_issues.return_value = []
        mock_github_client.check_should_process_with_label_manager.return_value = True

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert - Urgent PR should be first despite having failing checks
        assert len(candidates) == 2
        assert candidates[0].data["number"] == 1  # Urgent PR first
        assert candidates[0].priority == 3  # Urgent + mergeable
        assert candidates[1].data["number"] == 2  # Ready PR second
        assert candidates[1].priority == 2  # Ready to merge (mergeable with passing checks)

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.pr_processor._extract_linked_issues_from_pr_body")
    def test_breaking_change_label_highest_priority(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        test_repo_name,
    ):
        """Test that breaking-change labels get the highest priority (7)."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        mock_github_client.get_open_pull_requests.return_value = [
            Mock(number=1, created_at="2024-01-01T00:00:00Z"),  # Breaking-change PR
            Mock(number=2, created_at="2024-01-02T00:00:00Z"),  # Urgent unmergeable PR
        ]
        mock_github_client.get_open_issues.return_value = []

        pr_data = {
            1: {
                "number": 1,
                "title": "Breaking-change PR",
                "body": "",
                "head": {"ref": "pr-1"},
                "labels": ["breaking-change"],
                "mergeable": True,
                "created_at": "2024-01-01T00:00:00Z",
            },
            2: {
                "number": 2,
                "title": "Urgent unmergeable PR",
                "body": "",
                "head": {"ref": "pr-2"},
                "labels": ["urgent"],
                "mergeable": False,
                "created_at": "2024-01-02T00:00:00Z",
            },
        }

        def get_pr_details_side_effect(pr):
            return pr_data[pr.number]

        mock_github_client.get_pr_details.side_effect = get_pr_details_side_effect

        def check_actions_side_effect(repo_name, pr_details, config):
            return GitHubActionsStatusResult(success=False, ids=[], in_progress=False)

        mock_check_actions.side_effect = check_actions_side_effect
        mock_extract_issues.return_value = []
        mock_github_client.check_should_process_with_label_manager.return_value = True

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert - Breaking-change PR should be first with priority 7
        assert len(candidates) == 2
        assert candidates[0].data["number"] == 1  # Breaking-change PR first
        assert candidates[0].priority == 7  # Breaking-change priority
        assert candidates[1].data["number"] == 2  # Urgent unmergeable PR second
        assert candidates[1].priority == 4  # Urgent + unmergeable


class TestPriorityEdgeCases:
    """Test edge cases in priority calculation."""

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.pr_processor._extract_linked_issues_from_pr_body")
    def test_unmergeable_pr_with_passing_checks(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        test_repo_name,
    ):
        """Test unmergeable PR with passing checks gets priority 2."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        mock_github_client.get_open_pull_requests.return_value = [
            Mock(number=1, created_at="2024-01-01T00:00:00Z"),
        ]
        mock_github_client.get_open_issues.return_value = []

        pr_data = {
            1: {
                "number": 1,
                "title": "Unmergeable PR with passing checks",
                "body": "",
                "head": {"ref": "pr-1"},
                "labels": [],
                "mergeable": False,  # Unmergeable (merge conflicts)
                "created_at": "2024-01-01T00:00:00Z",
            },
        }

        mock_github_client.get_pr_details.return_value = pr_data[1]

        # Mock passing checks
        mock_check_actions.return_value = GitHubActionsStatusResult(success=True, ids=[], in_progress=False)
        mock_extract_issues.return_value = []
        mock_github_client.check_should_process_with_label_manager.return_value = True

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert - Unmergeable PR should get priority 2 even with passing checks
        assert len(candidates) == 1
        assert candidates[0].priority == 2
        assert candidates[0].data["mergeable"] is False

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.pr_processor._extract_linked_issues_from_pr_body")
    def test_unmergeable_pr_with_failing_checks(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        test_repo_name,
    ):
        """Test unmergeable PR with failing checks still gets priority 2."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        mock_github_client.get_open_pull_requests.return_value = [
            Mock(number=1, created_at="2024-01-01T00:00:00Z"),
        ]
        mock_github_client.get_open_issues.return_value = []

        pr_data = {
            1: {
                "number": 1,
                "title": "Unmergeable PR with failing checks",
                "body": "",
                "head": {"ref": "pr-1"},
                "labels": [],
                "mergeable": False,  # Unmergeable (merge conflicts)
                "created_at": "2024-01-01T00:00:00Z",
            },
        }

        mock_github_client.get_pr_details.return_value = pr_data[1]

        # Mock failing checks
        mock_check_actions.return_value = GitHubActionsStatusResult(success=False, ids=[], in_progress=False)
        mock_extract_issues.return_value = []
        mock_github_client.check_should_process_with_label_manager.return_value = True

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert - Unmergeable PR should still get priority 2 (unmergeable takes precedence)
        assert len(candidates) == 1
        assert candidates[0].priority == 2
        assert candidates[0].data["mergeable"] is False

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.pr_processor._extract_linked_issues_from_pr_body")
    def test_mergeable_pr_with_failing_checks(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        test_repo_name,
    ):
        """Test mergeable PR with failing checks gets priority 1."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        mock_github_client.get_open_pull_requests.return_value = [
            Mock(number=1, created_at="2024-01-01T00:00:00Z"),
        ]
        mock_github_client.get_open_issues.return_value = []

        pr_data = {
            1: {
                "number": 1,
                "title": "Mergeable PR with failing checks",
                "body": "",
                "head": {"ref": "pr-1"},
                "labels": [],
                "mergeable": True,  # Mergeable but has failing checks
                "created_at": "2024-01-01T00:00:00Z",
            },
        }

        mock_github_client.get_pr_details.return_value = pr_data[1]

        # Mock failing checks
        mock_check_actions.return_value = GitHubActionsStatusResult(success=False, ids=[], in_progress=False)
        mock_extract_issues.return_value = []
        mock_github_client.check_should_process_with_label_manager.return_value = True

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert - Mergeable PR with failing checks should get priority 1
        assert len(candidates) == 1
        assert candidates[0].priority == 1
        assert candidates[0].data["mergeable"] is True

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.pr_processor._extract_linked_issues_from_pr_body")
    def test_mergeable_pr_with_passing_checks(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        test_repo_name,
    ):
        """Test mergeable PR with passing checks gets priority 2."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        mock_github_client.get_open_pull_requests.return_value = [
            Mock(number=1, created_at="2024-01-01T00:00:00Z"),
        ]
        mock_github_client.get_open_issues.return_value = []

        pr_data = {
            1: {
                "number": 1,
                "title": "Ready to merge PR",
                "body": "",
                "head": {"ref": "pr-1"},
                "labels": [],
                "mergeable": True,
                "created_at": "2024-01-01T00:00:00Z",
            },
        }

        mock_github_client.get_pr_details.return_value = pr_data[1]

        # Mock passing checks
        mock_check_actions.return_value = GitHubActionsStatusResult(success=True, ids=[], in_progress=False)
        mock_extract_issues.return_value = []
        mock_github_client.check_should_process_with_label_manager.return_value = True

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert - Ready to merge PR should get priority 2
        assert len(candidates) == 1
        assert candidates[0].priority == 2
        assert candidates[0].data["mergeable"] is True


class TestPriorityIntegration:
    """Integration tests for priority-based candidate selection."""

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.pr_processor._extract_linked_issues_from_pr_body")
    def test_unmergeable_pr_processed_before_regular_fixes(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        test_repo_name,
    ):
        """Test that unmergeable PRs are processed before regular fix-required PRs."""
        # Setup
        engine = AutomationEngine(mock_github_client)

        # Create test scenario with mixed PR types
        mock_github_client.get_open_pull_requests.return_value = [
            Mock(number=1, created_at="2024-01-01T00:00:00Z"),  # Regular fix-required PR
            Mock(number=2, created_at="2024-01-02T00:00:00Z"),  # Unmergeable PR
            Mock(number=3, created_at="2024-01-03T00:00:00Z"),  # Another regular fix-required PR
        ]
        mock_github_client.get_open_issues.return_value = []

        pr_data = {
            1: {
                "number": 1,
                "title": "Regular fix-required PR",
                "body": "",
                "head": {"ref": "pr-1"},
                "labels": [],
                "mergeable": True,
                "created_at": "2024-01-01T00:00:00Z",
            },
            2: {
                "number": 2,
                "title": "Unmergeable PR",
                "body": "",
                "head": {"ref": "pr-2"},
                "labels": [],
                "mergeable": False,
                "created_at": "2024-01-02T00:00:00Z",
            },
            3: {
                "number": 3,
                "title": "Another regular fix-required PR",
                "body": "",
                "head": {"ref": "pr-3"},
                "labels": [],
                "mergeable": True,
                "created_at": "2024-01-03T00:00:00Z",
            },
        }

        def get_pr_details_side_effect(pr):
            return pr_data[pr.number]

        mock_github_client.get_pr_details.side_effect = get_pr_details_side_effect

        def check_actions_side_effect(repo_name, pr_details, config):
            # All PRs have failing checks
            return GitHubActionsStatusResult(success=False, ids=[], in_progress=False)

        mock_check_actions.side_effect = check_actions_side_effect
        mock_extract_issues.return_value = []
        mock_github_client.check_should_process_with_label_manager.return_value = True

        # Execute
        candidates = engine._get_candidates(test_repo_name, max_items=10)

        # Assert - Unmergeable PR should be processed first
        assert len(candidates) == 3
        assert candidates[0].data["number"] == 2  # Unmergeable PR first (priority 2)
        assert candidates[0].priority == 2
        # Regular fix-required PRs should follow (priority 1)
        assert candidates[1].priority == 1
        assert candidates[2].priority == 1

    @patch("auto_coder.util.github_action._check_github_actions_status")
    @patch("auto_coder.pr_processor._extract_linked_issues_from_pr_body")
    def test_performance_impact_minimal(
        self,
        mock_extract_issues,
        mock_check_actions,
        mock_github_client,
        test_repo_name,
    ):
        """Test that enhanced priority calculation has minimal performance impact."""
        import time

        # Setup
        engine = AutomationEngine(mock_github_client)

        # Create many PRs
        pr_count = 50
        mock_github_client.get_open_pull_requests.return_value = [Mock(number=i, created_at=f"2024-01-01T00:00:{i:02d}Z") for i in range(1, pr_count + 1)]
        mock_github_client.get_open_issues.return_value = []

        pr_data = {
            i: {
                "number": i,
                "title": f"PR #{i}",
                "body": "",
                "head": {"ref": f"pr-{i}"},
                "labels": [],
                "mergeable": i % 3 != 0,  # Every 3rd PR is unmergeable
                "created_at": f"2024-01-01T00:00:{i:02d}Z",
            }
            for i in range(1, pr_count + 1)
        }

        def get_pr_details_side_effect(pr):
            return pr_data[pr.number]

        mock_github_client.get_pr_details.side_effect = get_pr_details_side_effect

        def check_actions_side_effect(repo_name, pr_details, config):
            # Every 2nd PR fails checks
            if pr_details["number"] % 2 == 1:
                return GitHubActionsStatusResult(success=False, ids=[], in_progress=False)
            return GitHubActionsStatusResult(success=True, ids=[], in_progress=False)

        mock_check_actions.side_effect = check_actions_side_effect
        mock_extract_issues.return_value = []
        mock_github_client.check_should_process_with_label_manager.return_value = True

        # Execute and measure time
        start_time = time.time()
        candidates = engine._get_candidates(test_repo_name, max_items=pr_count)
        elapsed_time = time.time() - start_time

        # Assert - Priority calculation should be fast (< 100ms for 50 PRs)
        assert elapsed_time < 0.1
        assert len(candidates) == pr_count

        # Verify correct prioritization
        # - Every 3rd PR is unmergeable -> priority 2
        # - Even PRs with passing checks -> priority 2 (mergeable with passing checks)
        # - Odd PRs (not divisible by 3) with failing checks -> priority 1
        priorities = [c.priority for c in candidates]
        assert max(priorities) == 2  # Unmergeable or ready to merge
        assert min(priorities) == 1  # Mergeable with failing checks
