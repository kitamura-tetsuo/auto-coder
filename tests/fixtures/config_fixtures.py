"""Configuration test fixtures for label system testing.

This module provides comprehensive test fixtures for configuration validation,
backward compatibility, and upgrade path testing. It includes:
- Sample configuration files (valid/invalid)
- Environment variable setups
- Default configuration values
- Migration test data
"""

import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

# Valid Configuration Templates
VALID_CONFIG_TEMPLATES = {
    "minimal": {
        "description": "Minimal valid configuration",
        "config": {
            "label_prompt_mappings": {"bug": "issue.bugfix"},
            "label_priorities": ["bug"],
        },
    },
    "standard": {
        "description": "Standard configuration with multiple labels",
        "config": {
            "label_prompt_mappings": {
                "bug": "issue.bugfix",
                "feature": "issue.feature",
                "enhancement": "issue.enhancement",
                "documentation": "issue.documentation",
                "urgent": "issue.urgent",
            },
            "label_priorities": [
                "urgent",
                "bug",
                "enhancement",
                "feature",
                "documentation",
            ],
        },
    },
    "with_pr_labels": {
        "description": "Configuration with PR label mappings",
        "config": {
            "label_prompt_mappings": {
                "bug": "issue.bugfix",
                "feature": "issue.feature",
            },
            "label_priorities": ["bug", "feature"],
            "PR_LABEL_MAPPINGS": {
                "bug": ["bug", "defect", "bugfix"],
                "feature": ["feature", "enhancement", "improvement"],
            },
            "PR_LABEL_PRIORITIES": ["bug", "feature"],
            "PR_LABEL_MAX_COUNT": 3,
        },
    },
    "comprehensive": {
        "description": "Comprehensive configuration with all options",
        "config": {
            "label_prompt_mappings": {
                "breaking-change": "issue.breaking_change",
                "urgent": "issue.urgent",
                "bug": "issue.bugfix",
                "feature": "issue.feature",
                "enhancement": "issue.enhancement",
                "documentation": "issue.documentation",
                "question": "issue.question",
                "duplicate": "issue.duplicate",
            },
            "label_priorities": [
                "breaking-change",
                "urgent",
                "bug",
                "feature",
                "enhancement",
                "documentation",
                "question",
                "duplicate",
            ],
            "PR_LABEL_MAPPINGS": {
                "breaking-change": [
                    "breaking-change",
                    "breaking",
                    "api-change",
                    "deprecation",
                    "version-major",
                ],
                "urgent": ["urgent", "high-priority", "critical", "asap"],
                "bug": ["bug", "bugfix", "defect", "error", "fix"],
                "feature": ["feature", "new-feature", "enhancement"],
            },
            "PR_LABEL_PRIORITIES": [
                "breaking-change",
                "urgent",
                "bug",
                "feature",
            ],
            "PR_LABEL_MAX_COUNT": 5,
            "DISABLE_LABELS": False,
        },
    },
    "with_unicode": {
        "description": "Configuration with Unicode characters",
        "config": {
            "label_prompt_mappings": {
                "🐛": "issue.bugfix",
                "✨": "issue.feature",
                "📚": "issue.documentation",
                "🚀": "issue.enhancement",
            },
            "label_priorities": ["🐛", "✨", "📚", "🚀"],
        },
    },
    "with_special_chars": {
        "description": "Configuration with special characters",
        "config": {
            "label_prompt_mappings": {
                "bug!": "issue.exclaim",
                "feature?": "issue.question",
                "enhancement#": "issue.hash",
                "urgent@home": "issue.at",
                "type-error": "issue.dash",
                "scope::module": "issue.double_colon",
            },
            "label_priorities": ["bug!", "feature?", "enhancement#", "urgent@home"],
        },
    },
}


# Invalid Configuration Templates
INVALID_CONFIG_TEMPLATES = {
    "missing_mappings": {
        "description": "Missing label_prompt_mappings",
        "config": {"label_priorities": ["bug"]},
        "expected_issue": "missing_mappings",
    },
    "missing_priorities": {
        "description": "Missing label_priorities",
        "config": {"label_prompt_mappings": {"bug": "issue.bugfix"}},
        "expected_issue": "missing_priorities",
    },
    "empty_mappings": {
        "description": "Empty label_prompt_mappings",
        "config": {"label_prompt_mappings": {}, "label_priorities": ["bug"]},
        "expected_issue": "empty_mappings",
    },
    "empty_priorities": {
        "description": "Empty label_priorities",
        "config": {
            "label_prompt_mappings": {"bug": "issue.bugfix"},
            "label_priorities": [],
        },
        "expected_issue": "empty_priorities",
    },
    "invalid_type_mappings": {
        "description": "Invalid type for label_prompt_mappings",
        "config": {"label_prompt_mappings": "not a dict", "label_priorities": ["bug"]},
        "expected_issue": "invalid_type",
    },
    "invalid_type_priorities": {
        "description": "Invalid type for label_priorities",
        "config": {
            "label_prompt_mappings": {"bug": "issue.bugfix"},
            "label_priorities": "not a list",
        },
        "expected_issue": "invalid_type",
    },
    "non_string_mapping_values": {
        "description": "Non-string values in mappings",
        "config": {"label_prompt_mappings": {"bug": 123}, "label_priorities": ["bug"]},
        "expected_issue": "non_string_values",
    },
    "duplicate_keys": {
        "description": "Duplicate keys in configuration",
        "config": {
            "label_prompt_mappings": {"bug": "issue.bugfix"},
            "label_priorities": ["bug"],
        },
        "expected_issue": "duplicate_keys",
    },
}


# Environment Variable Test Cases
ENV_VAR_TEST_CASES = {
    "basic_json": {
        "description": "Basic JSON format in environment variables",
        "variables": {
            "AUTO_CODER_LABEL_PROMPT_MAPPINGS": '{"bug": "issue.bugfix", "feature": "issue.feature"}',
            "AUTO_CODER_LABEL_PRIORITIES": '["bug", "feature"]',
        },
        "expected": {
            "mappings": {"bug": "issue.bugfix", "feature": "issue.feature"},
            "priorities": ["bug", "feature"],
        },
    },
    "basic_yaml": {
        "description": "Basic YAML format in environment variables",
        "variables": {
            "AUTO_CODER_LABEL_PROMPT_MAPPINGS": "bug: issue.bugfix\nfeature: issue.feature",
            "AUTO_CODER_LABEL_PRIORITIES": "- bug\n- feature",
        },
        "expected": {
            "mappings": {"bug": "issue.bugfix", "feature": "issue.feature"},
            "priorities": ["bug", "feature"],
        },
    },
    "with_pr_labels": {
        "description": "PR label mappings in environment variables",
        "variables": {
            "AUTO_CODER_PR_LABEL_MAPPINGS": '{"bug": ["bug", "bugfix"], "feature": ["feature", "enhancement"]}',
            "AUTO_CODER_PR_LABEL_PRIORITIES": '["bug", "feature"]',
        },
        "expected": {
            "pr_mappings": {
                "bug": ["bug", "bugfix"],
                "feature": ["feature", "enhancement"],
            },
            "pr_priorities": ["bug", "feature"],
        },
    },
    "with_unicode": {
        "description": "Unicode characters in environment variables",
        "variables": {
            "AUTO_CODER_LABEL_PROMPT_MAPPINGS": '{"🐛": "issue.bugfix", "✨": "issue.feature"}',
        },
        "expected": {
            "mappings": {"🐛": "issue.bugfix", "✨": "issue.feature"},
        },
    },
}


# Default Configuration Values
DEFAULT_CONFIGS = {
    "empty": {
        "label_prompt_mappings": {},
        "label_priorities": [],
        "PR_LABEL_MAPPINGS": {},
        "PR_LABEL_PRIORITIES": [],
        "PR_LABEL_MAX_COUNT": 10,
        "DISABLE_LABELS": False,
    },
    "minimal_defaults": {
        "label_prompt_mappings": {"bug": "issue.bugfix"},
        "label_priorities": ["bug"],
        "PR_LABEL_MAPPINGS": {"bug": ["bug", "bugfix"]},
        "PR_LABEL_PRIORITIES": ["bug"],
        "PR_LABEL_MAX_COUNT": 10,
        "DISABLE_LABELS": False,
    },
    "standard_defaults": {
        "label_prompt_mappings": {
            "bug": "issue.bugfix",
            "feature": "issue.feature",
            "enhancement": "issue.enhancement",
            "documentation": "issue.documentation",
            "urgent": "issue.urgent",
        },
        "label_priorities": [
            "urgent",
            "bug",
            "enhancement",
            "feature",
            "documentation",
        ],
        "PR_LABEL_MAPPINGS": {
            "bug": ["bug", "bugfix", "defect", "error", "fix"],
            "feature": ["feature", "new-feature", "enhancement", "improvement"],
            "documentation": ["documentation", "docs", "doc", "readme"],
        },
        "PR_LABEL_PRIORITIES": ["bug", "feature", "documentation"],
        "PR_LABEL_MAX_COUNT": 5,
        "DISABLE_LABELS": False,
    },
}


# Migration Test Data
MIGRATION_TEST_CASES = {
    "v1_to_v2": {
        "description": "Migration from version 1.0 to 2.0",
        "from_version": "1.0",
        "to_version": "2.0",
        "old_config": {
            "bug": "issue.bugfix",
            "feature": "issue.feature",
        },
        "new_config": {
            "version": "2.0",
            "label_prompt_mappings": {
                "bug": "issue.bugfix",
                "feature": "issue.feature",
            },
            "label_priorities": ["bug", "feature"],
        },
        "migration_steps": [
            "Detect old format",
            "Create structured config",
            "Add version marker",
            "Validate result",
        ],
    },
    "v2_to_v3": {
        "description": "Migration from version 2.0 to 3.0 with PR labels",
        "from_version": "2.0",
        "to_version": "3.0",
        "old_config": {
            "version": "2.0",
            "label_prompt_mappings": {"bug": "issue.bugfix"},
            "label_priorities": ["bug"],
        },
        "new_config": {
            "version": "3.0",
            "label_prompt_mappings": {"bug": "issue.bugfix"},
            "label_priorities": ["bug"],
            "PR_LABEL_MAPPINGS": {"bug": ["bug", "bugfix"]},
            "PR_LABEL_PRIORITIES": ["bug"],
            "PR_LABEL_MAX_COUNT": 5,
        },
        "migration_steps": [
            "Detect v2 format",
            "Add PR label support",
            "Set default max count",
            "Validate result",
        ],
    },
    "flat_to_structured": {
        "description": "Migration from flat to structured format",
        "old_format": "flat",
        "new_format": "structured",
        "old_config": {
            "bug": "issue.bugfix",
            "feature": "issue.feature",
            "urgent": "issue.urgent",
        },
        "new_config": {
            "label_prompt_mappings": {
                "bug": "issue.bugfix",
                "feature": "issue.feature",
                "urgent": "issue.urgent",
            },
            "label_priorities": ["urgent", "bug", "feature"],
        },
    },
}


# Backward Compatibility Test Cases
BACKWARD_COMPAT_TEST_CASES = {
    "old_render_prompt_calls": {
        "description": "Old render_prompt calls without labels",
        "test_cases": [
            {
                "call": {"key": "issue.action", "path": None, "data": None},
                "expected": "should_work",
            },
            {
                "call": {
                    "key": "issue.action",
                    "path": None,
                    "data": None,
                    "labels": None,
                },
                "expected": "should_work",
            },
            {
                "call": {
                    "key": "issue.action",
                    "path": None,
                    "data": None,
                    "labels": [],
                },
                "expected": "should_work",
            },
        ],
    },
    "deprecated_label_names": {
        "description": "Deprecated label names that should still work",
        "labels": ["defect", "hotfix", "improvement"],
        "mappings": {
            "defect": "issue.bugfix",
            "hotfix": "issue.bugfix",
            "improvement": "issue.feature",
        },
        "priorities": ["defect", "hotfix", "improvement"],
        "expected": "all_resolve",
    },
    "legacy_prompt_keys": {
        "description": "Legacy prompt template key formats",
        "keys": ["issue.action", "issue_bugfix", "issueAction", "1", "issue@action"],
        "expected": "all_accessible",
    },
    "removed_parameters": {
        "description": "Removed parameters that should be ignored",
        "config": {
            "label_prompt_mappings": {"bug": "issue.bugfix"},
            "label_priorities": ["bug"],
            "obsolete_key": "should_be_ignored",
        },
        "expected": "obsolete_ignored",
    },
}


# Upgrade Path Test Scenarios
UPGRADE_SCENARIOS = {
    "fresh_install": {
        "description": "Fresh installation with no existing config",
        "has_existing_config": False,
        "expected_config": "default",
        "migration_needed": False,
    },
    "upgrade_no_labels": {
        "description": "Upgrade from version without label support",
        "has_existing_config": True,
        "existing_config_type": "v1_no_labels",
        "expected_config": "v2_with_labels",
        "migration_needed": True,
    },
    "upgrade_with_custom": {
        "description": "Upgrade with existing custom configurations",
        "has_existing_config": True,
        "existing_config_type": "custom_labels",
        "expected_config": "upgraded_custom",
        "migration_needed": True,
        "preserve_custom": True,
    },
    "upgrade_with_aliases": {
        "description": "Upgrade with existing label aliases",
        "has_existing_config": True,
        "existing_config_type": "with_aliases",
        "expected_config": "integrated_aliases",
        "migration_needed": True,
        "preserve_aliases": True,
    },
}


# Sample Prompt Files
SAMPLE_PROMPT_FILES = {
    "minimal": {
        "path": "prompts_minimal.yaml",
        "content": 'issue:\n  action: "Default issue action"\n',
    },
    "with_labels": {
        "path": "prompts_with_labels.yaml",
        "content": """
issue:
  action: "Default issue action"
  bugfix: "Bug fix prompt"
  feature: "Feature prompt"
  enhancement: "Enhancement prompt"
  urgent: "Urgent prompt"
  documentation: "Documentation prompt"
  breaking_change: "Breaking change prompt"
        """.strip(),
    },
    "complex": {
        "path": "prompts_complex.yaml",
        "content": """
header: "Global header"
issue:
  action: "Default: $issue_number"
  bugfix: "Fix bug #$issue_number"
  feature: "Implement feature: $issue_number"
  enhancement: "Enhance: $issue_number"
  urgent: "URGENT: $issue_number"
  documentation: "Document: $issue_number"
  breaking_change: "BREAKING: $issue_number - DELETE TESTS"
pr:
  action: "Default PR action"
  merge: "Merge PR #$pr_number"
        """.strip(),
    },
    "with_unicode": {
        "path": "prompts_unicode.yaml",
        "content": """
issue:
  action: "Default 🌍"
  bugfix: "Fix 🐛 #$issue_number"
  feature: "Add ✨ #$issue_number"
  documentation: "Document 📚 #$issue_number"
        """.strip(),
    },
}


# Configuration File Templates (as strings for file creation)
CONFIG_FILE_TEMPLATES = {
    "yaml_minimal": """
label_prompt_mappings:
  bug: "issue.bugfix"
label_priorities:
  - bug
    """.strip(),
    "yaml_standard": """
label_prompt_mappings:
  bug: "issue.bugfix"
  feature: "issue.feature"
  enhancement: "issue.enhancement"
  documentation: "issue.documentation"
  urgent: "issue.urgent"
label_priorities:
  - urgent
  - bug
  - enhancement
  - feature
  - documentation
    """.strip(),
    "yaml_comprehensive": """
version: "3.0"
label_prompt_mappings:
  breaking-change: "issue.breaking_change"
  urgent: "issue.urgent"
  bug: "issue.bugfix"
  feature: "issue.feature"
  enhancement: "issue.enhancement"
  documentation: "issue.documentation"
label_priorities:
  - breaking-change
  - urgent
  - bug
  - feature
  - enhancement
  - documentation
PR_LABEL_MAPPINGS:
  breaking-change:
    - breaking-change
    - breaking
    - api-change
    - deprecation
  urgent:
    - urgent
    - high-priority
    - critical
  bug:
    - bug
    - bugfix
    - defect
    - error
  feature:
    - feature
    - new-feature
    - enhancement
PR_LABEL_PRIORITIES:
  - breaking-change
  - urgent
  - bug
  - feature
PR_LABEL_MAX_COUNT: 5
DISABLE_LABELS: false
    """.strip(),
    "json_minimal": """
{
  "label_prompt_mappings": {
    "bug": "issue.bugfix"
  },
  "label_priorities": ["bug"]
}
    """.strip(),
    "old_format_flat": """
bug: "issue.bugfix"
feature: "issue.feature"
enhancement: "issue.enhancement"
    """.strip(),
}


# Performance Test Configurations
PERFORMANCE_CONFIGS = {
    "small": {
        "num_labels": 10,
        "num_mappings": 10,
        "expected_max_time": 0.01,
    },
    "medium": {
        "num_labels": 100,
        "num_mappings": 100,
        "expected_max_time": 0.1,
    },
    "large": {
        "num_labels": 1000,
        "num_mappings": 1000,
        "expected_max_time": 1.0,
    },
}


# Error Message Test Cases
ERROR_MESSAGE_TEST_CASES = {
    "missing_file": {
        "description": "Missing configuration file",
        "file_path": "/nonexistent/config.yaml",
        "expected_error_type": "FileNotFoundError",
    },
    "invalid_yaml": {
        "description": "Invalid YAML syntax",
        "content": "[: invalid yaml",
        "expected_error_type": "YAMLError",
    },
    "root_not_dict": {
        "description": "YAML root is not a dictionary",
        "content": "- item1\n- item2",
        "expected_error_type": "SystemExit",
    },
    "invalid_mapping_type": {
        "description": "Invalid type for mappings",
        "mappings": "not a dict",
        "priorities": ["bug"],
        "expected_error_type": None,  # Should handle gracefully
    },
}


# Test Fixtures
@pytest.fixture
def valid_config_templates():
    """Return valid configuration templates."""
    return VALID_CONFIG_TEMPLATES


@pytest.fixture
def invalid_config_templates():
    """Return invalid configuration templates."""
    return INVALID_CONFIG_TEMPLATES


@pytest.fixture
def env_var_test_cases():
    """Return environment variable test cases."""
    return ENV_VAR_TEST_CASES


@pytest.fixture
def default_configs():
    """Return default configuration values."""
    return DEFAULT_CONFIGS


@pytest.fixture
def migration_test_cases():
    """Return migration test cases."""
    return MIGRATION_TEST_CASES


@pytest.fixture
def backward_compat_test_cases():
    """Return backward compatibility test cases."""
    return BACKWARD_COMPAT_TEST_CASES


@pytest.fixture
def upgrade_scenarios():
    """Return upgrade path scenarios."""
    return UPGRADE_SCENARIOS


@pytest.fixture
def sample_prompt_files():
    """Return sample prompt file templates."""
    return SAMPLE_PROMPT_FILES


@pytest.fixture
def config_file_templates():
    """Return configuration file templates."""
    return CONFIG_FILE_TEMPLATES


@pytest.fixture
def performance_configs():
    """Return performance test configurations."""
    return PERFORMANCE_CONFIGS


@pytest.fixture
def error_message_test_cases():
    """Return error message test cases."""
    return ERROR_MESSAGE_TEST_CASES


# Helper Functions
def create_config_file(tmp_path: Path, config_name: str, config_type: str = "yaml_standard") -> Path:
    """Create a configuration file from templates.

    Args:
        tmp_path: Temporary directory path
        config_name: Name for the config file
        config_type: Type of configuration template

    Returns:
        Path to created configuration file
    """
    config_file = tmp_path / config_name
    template = CONFIG_FILE_TEMPLATES.get(config_type, CONFIG_FILE_TEMPLATES["yaml_standard"])
    config_file.write_text(template, encoding="utf-8")
    return config_file


def create_prompt_file(tmp_path: Path, prompt_name: str, prompt_type: str = "with_labels") -> Path:
    """Create a prompt file from templates.

    Args:
        tmp_path: Temporary directory path
        prompt_name: Name for the prompt file
        prompt_type: Type of prompt template

    Returns:
        Path to created prompt file
    """
    prompt_file = tmp_path / prompt_name
    template = SAMPLE_PROMPT_FILES.get(prompt_type, SAMPLE_PROMPT_FILES["with_labels"])
    prompt_file.write_text(template["content"], encoding="utf-8")
    return prompt_file


def create_test_config(
    tmp_path: Path,
    config_name: str,
    mappings: Dict[str, str],
    priorities: List[str],
) -> Path:
    """Create a test configuration file with custom mappings and priorities.

    Args:
        tmp_path: Temporary directory path
        config_name: Name for the config file
        mappings: Label to prompt mappings
        priorities: Label priorities list

    Returns:
        Path to created configuration file
    """
    config_file = tmp_path / config_name
    config_data = {
        "label_prompt_mappings": mappings,
        "label_priorities": priorities,
    }
    config_file.write_text(yaml.dump(config_data), encoding="utf-8")
    return config_file


def load_config_from_dict(config_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Load configuration from dictionary with validation.

    Args:
        config_dict: Configuration dictionary

    Returns:
        Validated configuration dictionary
    """
    # Ensure required keys exist
    validated = {
        "label_prompt_mappings": config_dict.get("label_prompt_mappings", {}),
        "label_priorities": config_dict.get("label_priorities", []),
        "PR_LABEL_MAPPINGS": config_dict.get("PR_LABEL_MAPPINGS", {}),
        "PR_LABEL_PRIORITIES": config_dict.get("PR_LABEL_PRIORITIES", []),
        "PR_LABEL_MAX_COUNT": config_dict.get("PR_LABEL_MAX_COUNT", 10),
        "DISABLE_LABELS": config_dict.get("DISABLE_LABELS", False),
    }
    return validated


# Parametrized Test Data
@pytest.fixture(
    params=[
        "minimal",
        "standard",
        "with_pr_labels",
        "comprehensive",
        "with_unicode",
        "with_special_chars",
    ]
)
def valid_config_param(request):
    """Parametrized fixture for valid configurations."""
    return VALID_CONFIG_TEMPLATES[request.param]


@pytest.fixture(
    params=[
        "missing_mappings",
        "missing_priorities",
        "empty_mappings",
        "empty_priorities",
        "invalid_type_mappings",
        "invalid_type_priorities",
    ]
)
def invalid_config_param(request):
    """Parametrized fixture for invalid configurations."""
    return INVALID_CONFIG_TEMPLATES[request.param]


@pytest.fixture(
    params=[
        ("bug", "issue.bugfix", ["bug"], "bug"),
        ("feature", "issue.feature", ["feature"], "feature"),
        ("bug", "issue.bugfix", ["feature", "bug"], "bug"),
        ("urgent", "issue.urgent", ["urgent", "bug"], "urgent"),
    ]
)
def label_resolution_test_case(request):
    """Parametrized fixture for label resolution test cases."""
    return request.param


# Export all for easy access
__all__ = [
    "VALID_CONFIG_TEMPLATES",
    "INVALID_CONFIG_TEMPLATES",
    "ENV_VAR_TEST_CASES",
    "DEFAULT_CONFIGS",
    "MIGRATION_TEST_CASES",
    "BACKWARD_COMPAT_TEST_CASES",
    "UPGRADE_SCENARIOS",
    "SAMPLE_PROMPT_FILES",
    "CONFIG_FILE_TEMPLATES",
    "PERFORMANCE_CONFIGS",
    "ERROR_MESSAGE_TEST_CASES",
    "create_config_file",
    "create_prompt_file",
    "create_test_config",
    "load_config_from_dict",
]
