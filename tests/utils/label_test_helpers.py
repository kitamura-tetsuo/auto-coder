"""Test helper utilities for label-based prompt testing.

This module provides helper functions and utilities to reduce code duplication
in label-based tests. It includes functions for:
- Creating test issues with labels
- Asserting label priorities
- Validating prompt selections
- Measuring performance
- Common test assertions
"""

import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

try:
    import pytest
except ImportError:
    pytest = None


def create_test_issue(
    number: int = 1,
    title: str = "Test Issue",
    labels: List[str] = None,
    body: str = "",
    state: str = "open",
    assignees: List[str] = None,
) -> Dict[str, Any]:
    """Create a test issue dictionary with specified labels.

    Args:
        number: Issue number
        title: Issue title
        labels: List of labels for the issue
        body: Issue body text
        state: Issue state (open, closed)
        assignees: List of assignees

    Returns:
        Dictionary representing a GitHub issue

    Example:
        >>> issue = create_test_issue(number=123, labels=["bug", "urgent"])
        >>> assert issue["number"] == 123
        >>> assert "bug" in [l["name"] for l in issue["labels"]]
    """
    if labels is None:
        labels = []

    if assignees is None:
        assignees = []

    return {
        "number": number,
        "title": title,
        "labels": [{"name": label} for label in labels],
        "body": body,
        "state": state,
        "assignees": [{"login": assignee} for assignee in assignees],
    }


def create_test_pr(
    number: int = 1,
    title: str = "Test PR",
    labels: List[str] = None,
    body: str = "",
    state: str = "open",
    mergeable: bool = True,
) -> Dict[str, Any]:
    """Create a test PR dictionary with specified labels.

    Args:
        number: PR number
        title: PR title
        labels: List of labels for the PR
        body: PR body text
        state: PR state (open, closed, merged)
        mergeable: Whether the PR is mergeable

    Returns:
        Dictionary representing a GitHub PR

    Example:
        >>> pr = create_test_pr(number=456, labels=["feature"])
        >>> assert pr["number"] == 456
        >>> assert pr["mergeable"] is True
    """
    if labels is None:
        labels = []

    return {
        "number": number,
        "title": title,
        "labels": [{"name": label} for label in labels],
        "body": body,
        "state": state,
        "mergeable": mergeable,
    }


def assert_label_priority(result: Optional[str], expected: str):
    """Assert that the label priority resolution produces the expected result.

    Args:
        result: The resolved label from the priority system
        expected: The expected label

    Example:
        >>> labels = ["urgent", "bug", "feature"]
        >>> priorities = ["urgent", "bug", "feature"]
        >>> resolved = resolve_priority(labels, priorities)
        >>> assert_label_priority(resolved, "urgent")
    """
    assert result == expected, f"Expected label '{expected}' but got '{result}'"


def assert_no_label_selected(result: Optional[str]):
    """Assert that no label was selected from the priority system.

    Args:
        result: The resolved label (should be None)

    Example:
        >>> labels = ["custom", "unofficial"]
        >>> priorities = ["bug", "feature"]
        >>> resolved = resolve_priority(labels, priorities)
        >>> assert_no_label_selected(resolved)
    """
    assert result is None, f"Expected no label but got '{result}'"


def assert_prompt_selection(prompt: str, expected_content: str):
    """Assert that the selected prompt contains the expected content.

    Args:
        prompt: The prompt string to validate
        expected_content: Content that should be in the prompt

    Example:
        >>> prompt = render_prompt(labels=["bug"], ...)
        >>> assert_prompt_selection(prompt, "Bug fix prompt")
    """
    assert expected_content in prompt, f"Expected '{expected_content}' in prompt but got: {prompt}"


def assert_prompt_not_selected(prompt: str, unexpected_content: str):
    """Assert that the selected prompt does not contain unexpected content.

    Args:
        prompt: The prompt string to validate
        unexpected_content: Content that should NOT be in the prompt

    Example:
        >>> prompt = render_prompt(labels=["bug"], ...)
        >>> assert_prompt_not_selected(prompt, "Breaking change")
    """
    assert unexpected_content not in prompt, f"Did not expect '{unexpected_content}' in prompt"


def assert_config_validation(config: Dict, is_valid: bool, error_message: str = None):
    """Assert that a configuration is valid or invalid.

    Args:
        config: Configuration dictionary to validate
        is_valid: Whether the config should be valid
        error_message: Expected error message if invalid (optional)

    Example:
        >>> config = {"label_priorities": ["bug"], "label_prompt_mappings": {"bug": "issue.bug"}}
        >>> assert_config_validation(config, is_valid=True)
    """
    if is_valid:
        assert _is_valid_config(config), f"Expected valid config but got invalid: {config}"
    else:
        assert not _is_valid_config(config), f"Expected invalid config but got valid: {config}"
        if error_message:
            # In a real test, you'd capture and check the actual error message
            pass


def measure_label_performance(func: Callable, *args, **kwargs) -> Dict[str, float]:
    """Measure the performance of a label-related function.

    Args:
        func: Function to measure
        *args: Positional arguments for the function
        **kwargs: Keyword arguments for the function

    Returns:
        Dictionary with timing information

    Example:
        >>> timings = measure_label_performance(resolve_labels, labels, priorities)
        >>> assert timings["total_time"] < 1.0  # Should complete in under 1 second
        >>> print(f"Function took {timings['total_time']:.3f}s")
    """
    # Warm up
    for _ in range(2):
        func(*args, **kwargs)

    # Measure multiple times for accuracy
    num_runs = 10
    times = []

    for _ in range(num_runs):
        start = time.perf_counter()
        result = func(*args, **kwargs)
        end = time.perf_counter()
        times.append(end - start)

    total_time = sum(times)
    avg_time = total_time / num_runs
    min_time = min(times)
    max_time = max(times)

    return {
        "total_time": total_time,
        "avg_time": avg_time,
        "min_time": min_time,
        "max_time": max_time,
        "num_runs": num_runs,
        "result": result,
    }


def create_large_label_set(size: int) -> List[str]:
    """Generate a large set of labels for performance testing.

    Args:
        size: Number of labels to generate

    Returns:
        List of label names

    Example:
        >>> labels = create_large_label_set(1000)
        >>> assert len(labels) == 1000
    """
    return [f"label-{i:04d}" for i in range(size)]


def create_large_priority_list(size: int) -> List[str]:
    """Generate a large priority list for performance testing.

    Args:
        size: Number of priorities to generate

    Returns:
        List of priority labels

    Example:
        >>> priorities = create_large_priority_list(1000)
        >>> assert len(priorities) == 1000
    """
    return [f"priority-{i:04d}" for i in range(size)]


def create_large_mappings_dict(size: int) -> Dict[str, str]:
    """Generate a large mappings dictionary for performance testing.

    Args:
        size: Number of mappings to generate

    Returns:
        Dictionary mapping labels to prompts

    Example:
        >>> mappings = create_large_mappings_dict(1000)
        >>> assert len(mappings) == 1000
    """
    return {f"label-{i:04d}": f"prompt.path.{i:04d}" for i in range(size)}


def validate_label_data(labels: List[Dict[str, Any]]):
    """Validate that a list of label dictionaries has the correct structure.

    Args:
        labels: List of label dictionaries

    Example:
        >>> labels = [{"name": "bug"}, {"name": "feature"}]
        >>> validate_label_data(labels)
    """
    for label in labels:
        assert "name" in label, f"Label missing 'name' field: {label}"
        assert isinstance(label["name"], str), f"Label name must be string: {label}"
    return True


def compare_label_sets(set1: List[str], set2: List[str]) -> bool:
    """Compare two label sets for equality.

    Args:
        set1: First set of labels
        set2: Second set of labels

    Returns:
        True if sets are equal, False otherwise

    Example:
        >>> labels1 = ["bug", "feature"]
        >>> labels2 = ["feature", "bug"]
        >>> assert compare_label_sets(labels1, labels2)
    """
    return set(set1) == set(set2)


def extract_labels_from_issue(issue: Dict[str, Any]) -> List[str]:
    """Extract label names from an issue dictionary.

    Args:
        issue: Issue dictionary from GitHub API

    Returns:
        List of label names

    Example:
        >>> issue = create_test_issue(labels=["bug", "urgent"])
        >>> labels = extract_labels_from_issue(issue)
        >>> assert "bug" in labels
    """
    return [label["name"] for label in issue.get("labels", [])]


def create_temp_prompt_file(content: str = None, suffix: str = ".yaml") -> Path:
    """Create a temporary prompt file for testing.

    Args:
        content: Content to write to the file
        suffix: File suffix (default: .yaml)

    Returns:
        Path to the temporary file

    Example:
        >>> file_path = create_temp_prompt_file("issue:\\n  action: 'test'")
        >>> assert file_path.exists()
    """
    if content is None:
        content = """
issue:
  action: "Default issue action"
  bugfix: "Bug fix prompt"
  feature: "Feature prompt"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False, encoding="utf-8") as f:
        f.write(content)
        return Path(f.name)


def create_temp_config_file(config: Dict[str, Any]) -> Path:
    """Create a temporary configuration file for testing.

    Args:
        config: Configuration dictionary

    Returns:
        Path to the temporary config file

    Example:
        >>> config = {"label_priorities": ["bug"], "label_prompt_mappings": {"bug": "issue.bug"}}
        >>> file_path = create_temp_config_file(config)
        >>> assert file_path.exists()
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        yaml.dump(config, f)
        return Path(f.name)


def assert_timing_requirement(timings: Dict[str, float], max_time: float):
    """Assert that execution time meets a requirement.

    Args:
        timings: Timing dictionary from measure_label_performance
        max_time: Maximum allowed time in seconds

    Example:
        >>> timings = measure_label_performance(func, arg1, arg2)
        >>> assert_timing_requirement(timings, 1.0)  # Must complete in under 1 second
    """
    assert timings["avg_time"] < max_time, f"Average execution time {timings['avg_time']:.3f}s " f"exceeds maximum {max_time:.3f}s"


def validate_prompt_template(template: str, variables: List[str]) -> bool:
    """Validate that a prompt template contains expected variables.

    Args:
        template: Prompt template string
        variables: List of expected variable names (without $)

    Returns:
        True if all variables are present

    Example:
        >>> template = "Fix issue $issue_number with label $label"
        >>> validate_prompt_template(template, ["issue_number", "label"])
        True
    """
    for var in variables:
        if f"${var}" not in template:
            return False
    return True


def create_test_scenario(
    name: str,
    labels: List[str],
    expected_prompt: str,
    priority_list: List[str],
    mappings: Dict[str, str],
) -> Dict[str, Any]:
    """Create a complete test scenario for label-based prompt testing.

    Args:
        name: Scenario name
        labels: Labels for the scenario
        expected_prompt: Expected prompt key or content
        priority_list: Label priority list
        mappings: Label to prompt mappings

    Returns:
        Dictionary representing a test scenario

    Example:
        >>> scenario = create_test_scenario(
        ...     name="bug_priority",
        ...     labels=["bug", "feature"],
        ...     expected_prompt="issue.bugfix",
        ...     priority_list=["bug", "feature"],
        ...     mappings={"bug": "issue.bugfix", "feature": "issue.feature"}
        ... )
    """
    return {
        "name": name,
        "labels": labels,
        "expected_prompt": expected_prompt,
        "priority_list": priority_list,
        "mappings": mappings,
    }


# Helper function for config validation
def _is_valid_config(config: Dict[str, Any]) -> bool:
    """Check if a configuration is valid.

    This is a simplified validation - in practice you'd use the actual config validation logic.
    """
    if not isinstance(config, dict):
        return False

    if "label_prompt_mappings" in config:
        if not isinstance(config["label_prompt_mappings"], dict):
            return False

    if "label_priorities" in config:
        if not isinstance(config["label_priorities"], list):
            return False

    return True


# Common test patterns
def assert_label_handling(test_func, labels, expected_result, **kwargs):
    """Helper to test label handling functions with various inputs.

    Args:
        test_func: Function to test
        labels: Labels to test with
        expected_result: Expected result
        **kwargs: Additional arguments for the test function

    Example:
        >>> assert_label_handling(
        ...     resolve_priority,
        ...     ["bug", "feature"],
        ...     "bug",
        ...     priorities=["bug", "feature"]
        ... )
    """
    result = test_func(labels, **kwargs)
    assert result == expected_result, f"Test failed for labels {labels}. " f"Expected {expected_result} but got {result}"


# Performance test decorators
def performance_test(threshold_seconds: float):
    """Decorator to mark performance tests.

    Args:
        threshold_seconds: Maximum acceptable execution time

    Example:
        >>> @performance_test(1.0)
        ... def test_label_resolution_performance():
        ...     resolve_labels(labels, priorities)
    """
    if pytest is not None:
        return pytest.mark.performance(threshold_seconds=threshold_seconds)

    # Return identity decorator if pytest not available
    def decorator(func):
        return func

    return decorator
