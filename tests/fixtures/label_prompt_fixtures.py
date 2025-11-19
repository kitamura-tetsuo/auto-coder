"""Test fixtures for label-based prompt tests.

This module provides comprehensive test data and fixtures for testing
label-based prompt handling functionality across the codebase.

Test Data Structure:
- TEST_ISSUE_DATA: Comprehensive mock data for issues with different label types
- Label configurations for testing various scenarios
- Mock configurations for performance and integration tests
"""

from typing import Any, Dict, List

import pytest

# Comprehensive test data for issues with different label configurations
TEST_ISSUE_DATA = {
    "breaking_change": {
        "number": 123,
        "title": "Breaking change: Remove deprecated API",
        "labels": ["breaking-change", "urgent"],
        "body": "This is a breaking change that removes deprecated functionality...",
    },
    "bug_fix": {
        "number": 124,
        "title": "Fix authentication bug",
        "labels": ["bug"],
        "body": "There's a bug in the authentication system...",
    },
    "enhancement": {
        "number": 125,
        "title": "Add new feature",
        "labels": ["enhancement", "documentation"],
        "body": "This enhancement adds a new feature...",
    },
    "urgent": {
        "number": 126,
        "title": "Critical security issue",
        "labels": ["urgent", "bug"],
        "body": "Security vulnerability needs immediate attention...",
    },
    "documentation": {
        "number": 127,
        "title": "Update documentation",
        "labels": ["documentation"],
        "body": "Documentation needs to be updated...",
    },
    "feature": {
        "number": 128,
        "title": "Implement new feature",
        "labels": ["feature"],
        "body": "This feature adds new functionality...",
    },
    "multiple_labels": {
        "number": 129,
        "title": "Complex issue with multiple labels",
        "labels": ["bug", "urgent", "enhancement"],
        "body": "This issue has multiple labels...",
    },
    "no_semantic_labels": {
        "number": 130,
        "title": "Issue with custom labels",
        "labels": ["custom-label", "another-label"],
        "body": "This issue has no semantic labels...",
    },
    "empty_labels": {
        "number": 131,
        "title": "Issue with no labels",
        "labels": [],
        "body": "This issue has no labels...",
    },
}


# Label prompt mappings for testing
TEST_LABEL_PROMPT_MAPPINGS = {
    "breaking-change": "issue.breaking_change",
    "breaking": "issue.breaking_change",
    "api-change": "issue.breaking_change",
    "deprecation": "issue.breaking_change",
    "version-major": "issue.breaking_change",
    "urgent": "issue.urgent",
    "bug": "issue.bugfix",
    "feature": "issue.feature",
    "enhancement": "issue.enhancement",
    "documentation": "issue.documentation",
}


# Label priorities (highest priority first)
TEST_LABEL_PRIORITIES = [
    "breaking-change",
    "breaking",
    "api-change",
    "deprecation",
    "version-major",
    "urgent",
    "bug",
    "enhancement",
    "documentation",
    "feature",
]


# PR label mappings (aliases)
TEST_PR_LABEL_MAPPINGS = {
    "breaking-change": [
        "breaking-change",
        "breaking",
        "api-change",
        "deprecation",
        "version-major",
        "major-change",
    ],
    "bug": [
        "bug",
        "bugfix",
        "defect",
        "error",
        "fix",
        "hotfix",
        "patch",
    ],
    "documentation": [
        "documentation",
        "docs",
        "doc",
        "readme",
        "guide",
    ],
    "enhancement": [
        "enhancement",
        "feature",
        "improvement",
        "new-feature",
        "refactor",
        "optimization",
        "optimisation",
    ],
    "urgent": [
        "urgent",
        "high-priority",
        "critical",
        "asap",
        "priority-high",
        "blocker",
    ],
}


# PR label priorities
TEST_PR_LABEL_PRIORITIES = [
    "breaking-change",
    "urgent",
    "bug",
    "enhancement",
    "documentation",
]


# Invalid configurations for error handling tests
INVALID_CONFIGS = {
    "negative_max_count": {
        "PR_LABEL_MAX_COUNT": -1,
        "expected_error": "PR_LABEL_MAX_COUNT must be between 0 and 10",
    },
    "too_large_max_count": {
        "PR_LABEL_MAX_COUNT": 15,
        "expected_error": "PR_LABEL_MAX_COUNT must be between 0 and 10",
    },
    "empty_priorities": {
        "PR_LABEL_PRIORITIES": [],
        "expected_error": None,  # May not raise error, just log warning
    },
    "priorities_without_mappings": {
        "PR_LABEL_PRIORITIES": ["custom-label"],
        "expected_error": None,  # May not raise error, just log warning
    },
}


# Performance test scenarios
PERFORMANCE_TEST_SCENARIOS = {
    "small_scale": {
        "num_labels": 10,
        "num_issues": 100,
        "expected_max_time": 1.0,  # seconds
    },
    "medium_scale": {
        "num_labels": 50,
        "num_issues": 1000,
        "expected_max_time": 5.0,  # seconds
    },
    "large_scale": {
        "num_labels": 100,
        "num_issues": 10000,
        "expected_max_time": 30.0,  # seconds
    },
}


# Test configurations
@pytest.fixture
def test_issue_data():
    """Return comprehensive test issue data."""
    return TEST_ISSUE_DATA


@pytest.fixture
def label_prompt_mappings():
    """Return label-to-prompt mappings for testing."""
    return TEST_LABEL_PROMPT_MAPPINGS


@pytest.fixture
def label_priorities():
    """Return label priorities for testing."""
    return TEST_LABEL_PRIORITIES


@pytest.fixture
def pr_label_mappings():
    """Return PR label mappings (aliases) for testing."""
    return TEST_PR_LABEL_MAPPINGS


@pytest.fixture
def pr_label_priorities():
    """Return PR label priorities for testing."""
    return TEST_PR_LABEL_PRIORITIES


@pytest.fixture
def invalid_configs():
    """Return invalid configurations for error testing."""
    return INVALID_CONFIGS


@pytest.fixture
def performance_scenarios():
    """Return performance test scenarios."""
    return PERFORMANCE_TEST_SCENARIOS


@pytest.fixture
def mock_github_client():
    """Create a mock GitHub client for testing."""
    from unittest.mock import Mock

    client = Mock()
    client.disable_labels = False
    client.has_label.return_value = False
    client.try_add_labels_to_issue.return_value = True
    client.get_issue_details_by_number.return_value = {"labels": []}
    client.get_pr_details_by_number.return_value = {"labels": []}
    client.get_repository.return_value = Mock()
    return client


@pytest.fixture
def temp_prompt_file(tmp_path):
    """Create a temporary prompt file for testing."""
    path = tmp_path / "prompts.yaml"
    path.write_text(
        'issue:\n  action: "Default issue action"\n  bugfix: "Bug fix prompt"\n  feature: "Feature prompt"\n  enhancement: "Enhancement prompt"\n',
        encoding="utf-8",
    )
    return path


@pytest.fixture
def complex_prompt_file(tmp_path):
    """Create a complex prompt file with all label-specific prompts."""
    path = tmp_path / "prompts.yaml"
    path.write_text(
        'header: "Global header"\n'
        "issue:\n"
        '  action: "Default issue action"\n'
        '  bugfix: "Bug fix prompt with $issue_number"\n'
        '  feature: "Feature prompt with $issue_number"\n'
        '  enhancement: "Enhancement prompt with $issue_number"\n'
        '  breaking_change: "Breaking change prompt - DELETE TESTS with $issue_number"\n'
        '  urgent: "Urgent prompt with $issue_number"\n'
        '  documentation: "Documentation prompt with $issue_number"\n',
        encoding="utf-8",
    )
    return path


@pytest.fixture
def empty_prompt_file(tmp_path):
    """Create an empty prompt file for testing."""
    path = tmp_path / "empty.yaml"
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture
def invalid_yaml_file(tmp_path):
    """Create an invalid YAML file for testing."""
    path = tmp_path / "invalid.yaml"
    path.write_text(":-: not yaml\n", encoding="utf-8")
    return path


@pytest.fixture
def missing_prompt_file(tmp_path):
    """Path to a non-existent prompt file for testing."""
    return tmp_path / "nonexistent.yaml"


@pytest.fixture
def large_label_set():
    """Generate a large set of labels for performance testing."""
    labels = []
    for i in range(1000):
        labels.append(f"label-{i}")
    return labels


@pytest.fixture
def large_priority_list():
    """Generate a large priority list for performance testing."""
    priorities = [f"label-{i}" for i in range(1000)]
    return priorities


@pytest.fixture
def large_mappings_dict():
    """Generate a large mappings dictionary for performance testing."""
    mappings = {f"label-{i}": f"prompt.{i}" for i in range(1000)}
    return mappings


# Parametrized test cases
BREAKING_CHANGE_TEST_CASES = [
    (["breaking-change"], True),
    (["breaking"], True),
    (["api-change"], True),
    (["deprecation"], True),
    (["version-major"], True),
    (["breaking", "urgent"], True),
    (["bug", "feature"], False),
    (["enhancement", "documentation"], False),
    ([], False),
]


LABEL_PRIORITY_TEST_CASES = [
    # (issue_labels, mappings, priorities, expected_result)
    (["bug"], {"bug": "issue.bug"}, ["bug"], "bug"),
    (["bug", "feature"], {"bug": "issue.bug", "feature": "issue.feature"}, ["feature", "bug"], "feature"),
    (["bug", "feature"], {"bug": "issue.bug", "feature": "issue.feature"}, ["bug"], "bug"),
    (["documentation"], {"bug": "issue.bug"}, ["bug"], None),
    ([], {"bug": "issue.bug"}, ["bug"], None),
    (["bug"], {}, ["bug"], None),
    (["bug"], {"bug": "issue.bug"}, [], "bug"),  # Fallback
]


RENDER_PROMPT_TEST_CASES = [
    # Test label-based prompt selection
    {
        "labels": ["bug"],
        "mappings": {"bug": "issue.bugfix"},
        "priorities": ["bug"],
        "default_key": "issue.action",
        "expected_contains": "Bug fix prompt",
    },
    {
        "labels": ["feature"],
        "mappings": {"feature": "issue.feature"},
        "priorities": ["feature"],
        "default_key": "issue.action",
        "expected_contains": "Feature prompt",
    },
    {
        "labels": ["bug", "feature"],
        "mappings": {"bug": "issue.bugfix", "feature": "issue.feature"},
        "priorities": ["feature", "bug"],
        "default_key": "issue.action",
        "expected_contains": "Feature prompt",  # Higher priority
    },
    {
        "labels": ["random"],
        "mappings": {"bug": "issue.bugfix"},
        "priorities": ["bug"],
        "default_key": "issue.action",
        "expected_contains": "Default issue action",  # Fallback
    },
]


# Backward compatibility test cases
BACKWARD_COMPAT_TEST_CASES = [
    # (render_prompt call, expected_result)
    (
        {"key": "issue.action", "path": None, "data": None, "labels": None},
        "Default issue action",
    ),
    (
        {"key": "issue.action", "path": None, "data": {"issue_number": "123"}, "labels": None},
        "Default issue action",
    ),
    (
        {"key": "issue.action", "path": None, "data": None, "labels": [], "label_prompt_mappings": None, "label_priorities": None},
        "Default issue action",
    ),
]


@pytest.fixture
def breaking_change_test_cases():
    """Parametrized test cases for breaking-change detection."""
    return BREAKING_CHANGE_TEST_CASES


@pytest.fixture
def label_priority_test_cases():
    """Parametrized test cases for label priority resolution."""
    return LABEL_PRIORITY_TEST_CASES


@pytest.fixture
def render_prompt_test_cases():
    """Parametrized test cases for render_prompt with labels."""
    return RENDER_PROMPT_TEST_CASES


@pytest.fixture
def backward_compat_test_cases():
    """Parametrized test cases for backward compatibility."""
    return BACKWARD_COMPAT_TEST_CASES


# Environment variable test cases
ENV_VAR_TEST_CASES = [
    {
        "var_name": "AUTO_CODER_LABEL_PROMPT_MAPPINGS",
        "var_value": '{"bug": "issue.bugfix", "feature": "issue.feature"}',
        "expected_mappings": {"bug": "issue.bugfix", "feature": "issue.feature"},
    },
    {
        "var_name": "AUTO_CODER_LABEL_PRIORITIES",
        "var_value": '["bug", "feature", "enhancement"]',
        "expected_priorities": ["bug", "feature", "enhancement"],
    },
    {
        "var_name": "AUTO_CODER_PR_LABEL_MAPPINGS",
        "var_value": '{"bug": ["bug", "bugfix"]}',
        "expected_mappings": {"bug": ["bug", "bugfix"]},
    },
    {
        "var_name": "AUTO_CODER_PR_LABEL_PRIORITIES",
        "var_value": '["bug", "feature"]',
        "expected_priorities": ["bug", "feature"],
    },
]


@pytest.fixture
def env_var_test_cases():
    """Environment variable test cases for configuration testing."""
    return ENV_VAR_TEST_CASES
