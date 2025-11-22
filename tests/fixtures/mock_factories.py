"""Mock factories for label-based prompt testing.

This module provides comprehensive mock infrastructure for testing label-based
prompt functionality. It includes pre-configured mock objects for GitHub API,
file systems, configuration objects, and more.

Mock Types:
- GitHub Client Mock: Pre-configured with various label scenarios
- File System Mock: Mock configuration and prompt files
- API Response Mock: GitHub API responses for different label operations
- Config Object Mock: Mock AutomationConfig instances
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from unittest.mock import MagicMock, Mock

try:
    import pytest
except ImportError:
    pytest = None


class GitHubClientMockFactory:
    """Factory for creating GitHub client mocks with various label scenarios."""

    @staticmethod
    def create_basic_mock():
        """Create a basic GitHub client mock."""
        mock_client = Mock()
        mock_client.disable_labels = False
        mock_client.has_label.return_value = False
        mock_client.try_add_labels.return_value = True
        mock_client.get_issue_details_by_number.return_value = {"labels": []}
        mock_client.get_pr_details_by_number.return_value = {"labels": []}
        mock_client.get_repository.return_value = Mock()
        mock_client.add_labels_to_issue.return_value = True
        mock_client.get_labels.return_value = []
        mock_client.token = "test_token"
        return mock_client

    @staticmethod
    def create_with_labels(labels: List[str]):
        """Create a mock client with specific labels."""
        mock_client = GitHubClientMockFactory.create_basic_mock()
        mock_client.get_issue_details_by_number.return_value = {"labels": [{"name": label} for label in labels]}
        mock_client.get_pr_details_by_number.return_value = {"labels": [{"name": label} for label in labels]}
        mock_client.has_label.side_effect = lambda issue_num, label: label in labels
        return mock_client

    @staticmethod
    def create_with_label_aliases(label_mappings: Dict[str, List[str]]):
        """Create a mock client that handles label aliases."""
        mock_client = GitHubClientMockFactory.create_basic_mock()

        def has_label_with_aliases(issue_num: int, label: str, all_labels: List[str] = None):
            all_labels = all_labels or []
            # Check direct match
            if label in all_labels:
                return True
            # Check alias matches
            for canonical, aliases in label_mappings.items():
                if label in aliases and any(alias in all_labels for alias in aliases):
                    return True
            return False

        mock_client.has_label.side_effect = has_label_with_aliases
        return mock_client

    @staticmethod
    def create_with_priority_labels(priorities: List[str], mappings: Dict[str, str], labels: List[str] = None):
        """Create a mock client that resolves label priorities."""
        mock_client = GitHubClientMockFactory.create_basic_mock()
        labels = labels or []

        def get_priority_label(issue_labels: List[str]):
            for priority_label in priorities:
                for label in issue_labels:
                    if label == priority_label or (label in mappings and mappings[label] == priority_label):
                        return priority_label
            return None

        mock_client.get_priority_label.side_effect = get_priority_label
        mock_client.get_issue_details_by_number.return_value = {"labels": [{"name": label} for label in labels] if labels else []}
        return mock_client

    @staticmethod
    def create_disabled_labels():
        """Create a mock client with labels disabled."""
        mock_client = GitHubClientMockFactory.create_basic_mock()
        mock_client.disable_labels = True
        return mock_client

    @staticmethod
    def create_with_error(error_type: str = "api_error"):
        """Create a mock client that raises errors."""
        mock_client = GitHubClientMockFactory.create_basic_mock()

        if error_type == "api_error":
            mock_client.get_issue_details_by_number.side_effect = Exception("GitHub API error")
        elif error_type == "rate_limit":
            mock_client.get_issue_details_by_number.side_effect = Exception("Rate limit exceeded")
        elif error_type == "auth_error":
            mock_client.get_issue_details_by_number.side_effect = Exception("Authentication failed")

        return mock_client


class FileSystemMockFactory:
    """Factory for creating file system mocks for testing."""

    @staticmethod
    def create_mock_path(tmp_path):
        """Create a mock Path object."""
        mock_path = Mock(spec=Path)
        mock_path.__truediv__ = lambda self, other: tmp_path / other
        mock_path.exists.return_value = True
        mock_path.is_file.return_value = True
        mock_path.read_text.return_value = "test content"
        return mock_path

    @staticmethod
    def create_config_file(tmp_path, config: Dict[str, Any]):
        """Create a mock configuration file."""
        import yaml

        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(config, f)
        return config_file

    @staticmethod
    def create_prompt_file(tmp_path, content: str = None):
        """Create a mock prompt file."""
        if content is None:
            content = """
issue:
  action: "Default issue action"
  bugfix: "Bug fix prompt"
  feature: "Feature prompt"
  enhancement: "Enhancement prompt"
  breaking_change: "Breaking change prompt"
  urgent: "Urgent prompt"
  documentation: "Documentation prompt"
"""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(content, encoding="utf-8")
        return prompt_file

    @staticmethod
    def create_non_existent_file(tmp_path, filename: str):
        """Create a path to a non-existent file."""
        return tmp_path / filename

    @staticmethod
    def create_invalid_yaml_file(tmp_path, filename: str = "invalid.yaml"):
        """Create an invalid YAML file for testing."""
        invalid_file = tmp_path / filename
        invalid_file.write_text("invalid: yaml: content:\n  - key: value: broken", encoding="utf-8")
        return invalid_file


class APIResponseMockFactory:
    """Factory for creating GitHub API response mocks."""

    @staticmethod
    def create_issue_response(number: int, title: str, labels: List[str], body: str = ""):
        """Create a mock issue response."""
        return {
            "number": number,
            "title": title,
            "labels": [{"name": label} for label in labels],
            "body": body,
            "state": "open",
        }

    @staticmethod
    def create_pr_response(number: int, title: str, labels: List[str], body: str = ""):
        """Create a mock PR response."""
        return {
            "number": number,
            "title": title,
            "labels": [{"name": label} for label in labels],
            "body": body,
            "state": "open",
            "mergeable": True,
        }

    @staticmethod
    def create_label_list_response(labels: List[str]):
        """Create a mock label list response."""
        return [{"name": label, "color": "000000"} for label in labels]

    @staticmethod
    def create_repository_response(owner: str, repo: str):
        """Create a mock repository response."""
        return {
            "owner": owner,
            "name": repo,
            "full_name": f"{owner}/{repo}",
            "default_branch": "main",
        }


class ConfigObjectMockFactory:
    """Factory for creating mock AutomationConfig instances."""

    @staticmethod
    def create_minimal_config():
        """Create a minimal mock config."""
        mock_config = Mock()
        mock_config.label_prompt_mappings = {"bug": "issue.bugfix"}
        mock_config.label_priorities = ["bug"]
        return mock_config

    @staticmethod
    def create_full_config(
        label_mappings: Dict[str, str] = None,
        label_priorities: List[str] = None,
        pr_label_mappings: Dict[str, List[str]] = None,
        pr_label_priorities: List[str] = None,
    ):
        """Create a full mock config with all options."""
        if label_mappings is None:
            label_mappings = {
                "bug": "issue.bugfix",
                "feature": "issue.feature",
                "enhancement": "issue.enhancement",
                "documentation": "issue.documentation",
            }

        if label_priorities is None:
            label_priorities = ["bug", "feature", "enhancement", "documentation"]

        if pr_label_mappings is None:
            pr_label_mappings = {
                "bug": ["bug", "bugfix"],
                "feature": ["feature", "enhancement"],
            }

        if pr_label_priorities is None:
            pr_label_priorities = ["bug", "feature"]

        mock_config = Mock()
        mock_config.label_prompt_mappings = label_mappings
        mock_config.label_priorities = label_priorities
        mock_config.PR_LABEL_MAPPINGS = pr_label_mappings
        mock_config.PR_LABEL_PRIORITIES = pr_label_priorities
        mock_config.PR_LABEL_MAX_COUNT = 5
        return mock_config

    @staticmethod
    def create_invalid_config():
        """Create an invalid config for error testing."""
        mock_config = Mock()
        mock_config.label_prompt_mappings = {"invalid_label": "invalid.prompt"}
        mock_config.label_priorities = []
        mock_config.PR_LABEL_MAPPINGS = {"invalid": []}
        mock_config.PR_LABEL_PRIORITIES = []
        # Simulate validation error
        mock_config.validate.side_effect = ValueError("Invalid configuration")
        return mock_config

    @staticmethod
    def create_config_with_custom_values(**kwargs):
        """Create a config with custom values."""
        mock_config = Mock()
        for key, value in kwargs.items():
            setattr(mock_config, key, value)
        return mock_config


class MockLabelManager:
    """Mock label manager for testing label operations."""

    def __init__(self, labels: List[str] = None):
        self.labels = labels or []
        self.label_history = []

    def add_label(self, label: str):
        """Add a label."""
        if label not in self.labels:
            self.labels.append(label)
        self.label_history.append(("add", label))

    def remove_label(self, label: str):
        """Remove a label."""
        if label in self.labels:
            self.labels.remove(label)
        self.label_history.append(("remove", label))

    def has_label(self, label: str) -> bool:
        """Check if label exists."""
        return label in self.labels

    def get_labels(self) -> List[str]:
        """Get all labels."""
        return self.labels.copy()


class MockPromptLoader:
    """Mock prompt loader for testing."""

    def __init__(self, prompts: Dict[str, str] = None):
        self.prompts = prompts or {
            "issue.action": "Default issue action",
            "issue.bugfix": "Bug fix prompt",
            "issue.feature": "Feature prompt",
        }

    def load_prompts(self, path: Path = None):
        """Load prompts from a file."""
        return self.prompts

    def get_prompt(self, key: str, default: str = None) -> str:
        """Get a prompt by key."""
        return self.prompts.get(key, default)


# Pytest fixtures for mock factories
def _create_fixture_decorator(func):
    """Create a pytest fixture decorator, or return identity if pytest is not available."""
    if pytest is not None:
        return pytest.fixture(func)
    # If pytest is not available, just return the function
    return func


def github_client_mock():
    """Create a basic GitHub client mock."""
    return GitHubClientMockFactory.create_basic_mock()


github_client_mock = _create_fixture_decorator(github_client_mock)


def github_client_with_labels():
    """Create a GitHub client mock with test labels."""
    labels = ["bug", "feature", "urgent"]
    return GitHubClientMockFactory.create_with_labels(labels)


github_client_with_labels = _create_fixture_decorator(github_client_with_labels)


def github_client_with_aliases():
    """Create a GitHub client mock with label aliases."""
    label_mappings = {"bug": ["bug", "bugfix"], "feature": ["feature", "enhancement"]}
    return GitHubClientMockFactory.create_with_label_aliases(label_mappings)


github_client_with_aliases = _create_fixture_decorator(github_client_with_aliases)


def github_client_disabled():
    """Create a GitHub client mock with disabled labels."""
    return GitHubClientMockFactory.create_disabled_labels()


github_client_disabled = _create_fixture_decorator(github_client_disabled)


def filesystem_mock():
    """Create a file system mock."""
    return FileSystemMockFactory


filesystem_mock = _create_fixture_decorator(filesystem_mock)


def api_response_mock():
    """Create an API response mock."""
    return APIResponseMockFactory


api_response_mock = _create_fixture_decorator(api_response_mock)


def config_mock():
    """Create a config object mock."""
    return ConfigObjectMockFactory.create_full_config()


config_mock = _create_fixture_decorator(config_mock)


def config_mock_minimal():
    """Create a minimal config object mock."""
    return ConfigObjectMockFactory.create_minimal_config()


config_mock_minimal = _create_fixture_decorator(config_mock_minimal)


def config_mock_invalid():
    """Create an invalid config object mock."""
    return ConfigObjectMockFactory.create_invalid_config()


config_mock_invalid = _create_fixture_decorator(config_mock_invalid)


def mock_label_manager():
    """Create a mock label manager."""
    return MockLabelManager()


mock_label_manager = _create_fixture_decorator(mock_label_manager)


def mock_prompt_loader():
    """Create a mock prompt loader."""
    return MockPromptLoader()


mock_prompt_loader = _create_fixture_decorator(mock_prompt_loader)
