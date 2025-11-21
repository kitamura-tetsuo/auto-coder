"""Tests for PR processor label-based prompt selection."""

from unittest.mock import MagicMock, Mock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.pr_processor import _create_pr_analysis_prompt


def test_create_pr_analysis_prompt_with_bug_label():
    """Test that PR analysis prompt uses bug-specific template when bug label is present.

    Note: If both "bug" and "high-priority" labels are present, "high-priority" maps to
    "urgent" which has higher priority than "bug", so urgent template will be used.
    """
    repo_name = "owner/repo"
    pr_data = {
        "number": 123,
        "title": "Fix authentication bug",
        "body": "This PR fixes a critical authentication bug",
        "user": {"login": "testuser"},
        "state": "open",
        "draft": False,
        "mergeable": True,
        "labels": ["bug"],  # Only bug label, no high-priority
    }
    pr_diff = "diff --git a/test.py"
    config = AutomationConfig()

    with patch("auto_coder.pr_processor.get_commit_log") as mock_commit_log:
        mock_commit_log.return_value = "commit log"
        prompt = _create_pr_analysis_prompt(repo_name, pr_data, pr_diff, config)

    # Should use bug-specific prompt
    assert "BUG FIX PR REQUIREMENTS" in prompt
    assert "Root cause analysis" in prompt or "ROOT CAUSE ANALYSIS" in prompt
    assert "Fix authentication bug" in prompt


def test_create_pr_analysis_prompt_with_enhancement_label():
    """Test that PR analysis prompt uses enhancement-specific template."""
    repo_name = "owner/repo"
    pr_data = {
        "number": 456,
        "title": "Add new feature",
        "body": "This PR adds a new feature to improve user experience",
        "user": {"login": "testuser"},
        "state": "open",
        "draft": False,
        "mergeable": True,
        "labels": ["enhancement", "feature"],
    }
    pr_diff = "diff --git a/feature.py"
    config = AutomationConfig()

    with patch("auto_coder.pr_processor.get_commit_log") as mock_commit_log:
        mock_commit_log.return_value = "commit log"
        prompt = _create_pr_analysis_prompt(repo_name, pr_data, pr_diff, config)

    # Should use enhancement-specific prompt
    assert "ENHANCEMENT PR REQUIREMENTS" in prompt
    assert "architecture and design patterns" in prompt or "DESIGN" in prompt
    assert "Add new feature" in prompt


def test_create_pr_analysis_prompt_with_documentation_label():
    """Test that PR analysis prompt uses documentation-specific template."""
    repo_name = "owner/repo"
    pr_data = {
        "number": 789,
        "title": "Update README",
        "body": "This PR updates the README with better examples",
        "user": {"login": "testuser"},
        "state": "open",
        "draft": False,
        "mergeable": True,
        "labels": ["documentation", "docs"],
    }
    pr_diff = "diff --git a/README.md"
    config = AutomationConfig()

    with patch("auto_coder.pr_processor.get_commit_log") as mock_commit_log:
        mock_commit_log.return_value = "commit log"
        prompt = _create_pr_analysis_prompt(repo_name, pr_data, pr_diff, config)

    # Should use documentation-specific prompt
    assert "DOCUMENTATION PR REQUIREMENTS" in prompt
    assert "clarity, accuracy, and completeness" in prompt or "CLARITY" in prompt
    assert "Update README" in prompt


def test_create_pr_analysis_prompt_with_urgent_label():
    """Test that PR analysis prompt uses urgent-specific template."""
    repo_name = "owner/repo"
    pr_data = {
        "number": 999,
        "title": "URGENT: Fix critical security issue",
        "body": "This PR fixes a critical security vulnerability",
        "user": {"login": "testuser"},
        "state": "open",
        "draft": False,
        "mergeable": True,
        "labels": ["urgent", "security"],
    }
    pr_diff = "diff --git a/security.py"
    config = AutomationConfig()

    with patch("auto_coder.pr_processor.get_commit_log") as mock_commit_log:
        mock_commit_log.return_value = "commit log"
        prompt = _create_pr_analysis_prompt(repo_name, pr_data, pr_diff, config)

    # Should use urgent-specific prompt
    assert "URGENT PR REQUIREMENTS" in prompt
    assert "immediate attention" in prompt or "IMMEDIATE ACTION" in prompt
    assert "URGENT: Fix critical security issue" in prompt


def test_create_pr_analysis_prompt_with_breaking_change_label():
    """Test that PR analysis prompt uses breaking-change-specific template."""
    repo_name = "owner/repo"
    pr_data = {
        "number": 111,
        "title": "Breaking: API version 2.0",
        "body": "This PR introduces breaking changes to the API",
        "user": {"login": "testuser"},
        "state": "open",
        "draft": False,
        "mergeable": True,
        "labels": ["breaking-change", "api-change"],
    }
    pr_diff = "diff --git a/api.py"
    config = AutomationConfig()

    with patch("auto_coder.pr_processor.get_commit_log") as mock_commit_log:
        mock_commit_log.return_value = "commit log"
        prompt = _create_pr_analysis_prompt(repo_name, pr_data, pr_diff, config)

    # Should use breaking-change-specific prompt
    assert "BREAKING CHANGE REQUIREMENTS" in prompt
    assert "major version bump" in prompt or "VERSION BUMP" in prompt
    assert "Breaking: API version 2.0" in prompt


def test_create_pr_analysis_prompt_priority_breaking_over_urgent():
    """Test that breaking-change has higher priority than urgent."""
    repo_name = "owner/repo"
    pr_data = {
        "number": 222,
        "title": "Critical API change",
        "body": "This PR introduces critical breaking changes",
        "user": {"login": "testuser"},
        "state": "open",
        "draft": False,
        "mergeable": True,
        "labels": ["urgent", "breaking-change"],
    }
    pr_diff = "diff --git a/api.py"
    config = AutomationConfig()

    with patch("auto_coder.pr_processor.get_commit_log") as mock_commit_log:
        mock_commit_log.return_value = "commit log"
        prompt = _create_pr_analysis_prompt(repo_name, pr_data, pr_diff, config)

    # Should use breaking-change prompt (higher priority)
    assert "BREAKING CHANGE REQUIREMENTS" in prompt
    assert "major version bump" in prompt or "VERSION BUMP" in prompt


def test_create_pr_analysis_prompt_priority_urgent_over_bug():
    """Test that urgent has higher priority than bug."""
    repo_name = "owner/repo"
    pr_data = {
        "number": 333,
        "title": "Urgent bug fix",
        "body": "This PR fixes an urgent bug",
        "user": {"login": "testuser"},
        "state": "open",
        "draft": False,
        "mergeable": True,
        "labels": ["urgent", "bug"],
    }
    pr_diff = "diff --git a/bug.py"
    config = AutomationConfig()

    with patch("auto_coder.pr_processor.get_commit_log") as mock_commit_log:
        mock_commit_log.return_value = "commit log"
        prompt = _create_pr_analysis_prompt(repo_name, pr_data, pr_diff, config)

    # Should use urgent prompt (higher priority)
    assert "URGENT PR REQUIREMENTS" in prompt
    assert "immediate attention" in prompt or "IMMEDIATE ACTION" in prompt


def test_create_pr_analysis_prompt_fallback_no_specific_label():
    """Test that PR prompt falls back to default when no specific label matches."""
    repo_name = "owner/repo"
    pr_data = {
        "number": 444,
        "title": "Miscellaneous changes",
        "body": "This PR has generic changes",
        "user": {"login": "testuser"},
        "state": "open",
        "draft": False,
        "mergeable": True,
        "labels": ["question", "help wanted"],
    }
    pr_diff = "diff --git a/misc.py"
    config = AutomationConfig()

    with patch("auto_coder.pr_processor.get_commit_log") as mock_commit_log:
        mock_commit_log.return_value = "commit log"
        prompt = _create_pr_analysis_prompt(repo_name, pr_data, pr_diff, config)

    # Should fall back to default PR action prompt
    assert "Repository: owner/repo" in prompt
    assert "PR #444: Miscellaneous changes" in prompt


def test_create_pr_analysis_prompt_includes_all_required_data():
    """Test that PR prompt includes all required PR data."""
    repo_name = "owner/repo"
    pr_data = {
        "number": 555,
        "title": "Test PR",
        "body": "Test PR body",
        "user": {"login": "testuser"},
        "state": "open",
        "draft": False,
        "mergeable": True,
        "labels": ["bug"],
    }
    pr_diff = "diff --git a/test.py"
    config = AutomationConfig()

    with patch("auto_coder.pr_processor.get_commit_log") as mock_commit_log:
        mock_commit_log.return_value = "Test commit log"
        prompt = _create_pr_analysis_prompt(repo_name, pr_data, pr_diff, config)

    # Verify all required fields are present
    assert "Repository: owner/repo" in prompt
    assert "PR #555: Test PR" in prompt
    assert "Test PR body" in prompt
    assert "Test commit log" in prompt
    assert "diff --git a/test.py" in prompt


def test_create_pr_analysis_prompt_empty_labels():
    """Test that PR prompt works with empty labels list."""
    repo_name = "owner/repo"
    pr_data = {
        "number": 666,
        "title": "No labels PR",
        "body": "This PR has no labels",
        "user": {"login": "testuser"},
        "state": "open",
        "draft": False,
        "mergeable": True,
        "labels": [],
    }
    pr_diff = "diff --git a/test.py"
    config = AutomationConfig()

    with patch("auto_coder.pr_processor.get_commit_log") as mock_commit_log:
        mock_commit_log.return_value = "commit log"
        prompt = _create_pr_analysis_prompt(repo_name, pr_data, pr_diff, config)

    # Should use default PR prompt
    assert "Repository: owner/repo" in prompt
    assert "PR #666: No labels PR" in prompt


def test_create_pr_analysis_prompt_with_multiple_labels_uses_highest_priority():
    """Test that PR prompt uses the highest priority label when multiple are present."""
    repo_name = "owner/repo"
    pr_data = {
        "number": 777,
        "title": "Multi-label PR",
        "body": "This PR has multiple labels",
        "user": {"login": "testuser"},
        "state": "open",
        "draft": False,
        "mergeable": True,
        "labels": ["enhancement", "documentation", "bug"],
    }
    pr_diff = "diff --git a/test.py"
    config = AutomationConfig()

    with patch("auto_coder.pr_processor.get_commit_log") as mock_commit_log:
        mock_commit_log.return_value = "commit log"
        prompt = _create_pr_analysis_prompt(repo_name, pr_data, pr_diff, config)

    # According to label_priorities, priority order is:
    # urgent > breaking-change > bug > enhancement > documentation
    # So bug should be selected
    assert "BUG FIX PR REQUIREMENTS" in prompt
    assert "Root cause analysis" in prompt or "ROOT CAUSE ANALYSIS" in prompt
