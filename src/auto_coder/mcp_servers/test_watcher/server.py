"""
Test Watcher MCP Server - Provides continuous test monitoring via MCP.
"""

import os
from mcp.server.fastmcp import FastMCP
from test_watcher_tool import TestWatcherTool

# Create an MCP server
mcp = FastMCP(
    "Test Watcher",
    dependencies=["loguru"]
)

# Initialize the test watcher tool
# Get project root from environment variable or use current directory
project_root = os.getenv("TEST_WATCHER_PROJECT_ROOT")
test_watcher = TestWatcherTool(project_root=project_root)


@mcp.tool()
def start_watching() -> dict:
    """
    Start file watching and automatic test execution.

    This tool starts monitoring file changes in the project (respecting .gitignore)
    and automatically runs Playwright tests when files are modified.

    Returns:
        Status of the watcher startup

    Example:
        start_watching()
    """
    return test_watcher.start_watching()


@mcp.tool()
def stop_watching() -> dict:
    """
    Stop file watching.

    Returns:
        Status of the watcher shutdown

    Example:
        stop_watching()
    """
    return test_watcher.stop_watching()


@mcp.tool()
def query_test_results(test_type: str = "all") -> dict:
    """
    Query test results.

    This tool returns test results for the specified test type. If tests are
    currently running, it will return a status indicating to wait.

    Args:
        test_type: Type of tests to query. Must be one of:
                   - "unit": Unit tests
                   - "integration": Integration tests
                   - "e2e": End-to-end tests (Playwright)
                   - "all": All test types (default)

    Returns:
        Test results including:
        - status: "running", "completed", "idle", or "error"
        - summary: Counts of passed, failed, flaky, skipped tests
        - failed_tests: List of all failed tests with file paths and error messages
        - first_failed_test: First failed test (for quick debugging)
        - flaky_tests: List of all flaky tests
        - first_flaky_test: First flaky test

    Example:
        query_test_results()
        query_test_results(test_type="e2e")
        query_test_results(test_type="unit")
    """
    return test_watcher.query_test_results(test_type=test_type)


@mcp.tool()
def get_status() -> dict:
    """
    Get overall status of the test watcher.

    Returns:
        Status information including:
        - file_watcher_running: Whether file watcher is active
        - playwright_running: Whether Playwright tests are currently running
        - project_root: Project root directory
        - test_results: Summary of test results for each test type

    Example:
        get_status()
    """
    return test_watcher.get_status()


@mcp.resource("test-watcher://status")
def get_overall_status() -> str:
    """
    Get overall status of the test watcher.

    This resource provides a comprehensive view of the file watcher status
    and current test results in a formatted string.

    Returns:
        Formatted string with status and test results
    """
    status = test_watcher.get_status()

    output = ["# Test Watcher Status\n"]

    output.append(f"Project Root: {status['project_root']}")
    output.append(f"File Watcher: {'Running' if status['file_watcher_running'] else 'Stopped'}")
    output.append(f"Playwright: {'Running' if status['playwright_running'] else 'Idle'}")

    output.append("\n## Test Results\n")

    for test_type in ["unit", "integration", "e2e"]:
        result = status["test_results"][test_type]
        output.append(f"\n### {test_type.upper()}")
        output.append(f"Status: {result['status']}")
        output.append(f"Passed: {result['passed']}")
        output.append(f"Failed: {result['failed']}")
        if test_type == "e2e":
            output.append(f"Flaky: {result['flaky']}")

    return "\n".join(output)


@mcp.resource("test-watcher://help")
def get_help() -> str:
    """
    Get help information about the Test Watcher MCP server.

    Returns:
        Help text with usage instructions
    """
    return """# Test Watcher MCP Server

A Model Context Protocol server for continuous test monitoring with automatic file watching.

## Overview

This MCP server monitors file changes (respecting .gitignore) and automatically runs
Playwright tests when files are modified. It provides test results to coding agents
without requiring manual test execution.

## Features

- **File Watching**: Monitors git-tracked files for changes
- **Automatic Test Execution**: Runs Playwright tests on file changes
- **Smart Test Running**: Uses --last-failed to run only failed tests first
- **Full Test Suite**: Runs all tests after all failed tests pass
- **Process Management**: Terminates running tests before starting new ones
- **JSON Reporting**: Parses Playwright JSON reports for detailed results
- **Flaky Test Detection**: Identifies and reports flaky tests

## Available Tools

1. **start_watching** - Start file watching and automatic test execution
2. **stop_watching** - Stop file watching
3. **query_test_results** - Query test results (unit/integration/e2e/all)
4. **get_status** - Get overall status of the test watcher

## Available Resources

1. **test-watcher://status** - Overall status and test results
2. **test-watcher://help** - This help text

## Usage Example

```python
# Start file watching
start_watching()

# Query e2e test results
results = query_test_results(test_type="e2e")

# Check if tests are running
if results["status"] == "running":
    print("Tests are running, please wait")
else:
    print(f"Passed: {results['summary']['passed']}")
    print(f"Failed: {results['summary']['failed']}")

    # Get first failed test for debugging
    if results.get("first_failed_test"):
        test = results["first_failed_test"]
        print(f"First failure: {test['file']}")
        print(f"Error: {test['error']}")

# Get overall status
status = get_status()

# Stop watching when done
stop_watching()
```

## Environment Variables

- `TEST_WATCHER_PROJECT_ROOT`: Root directory of the project to watch (default: current directory)

## Requirements

- Node.js and npm (for Playwright)
- Playwright installed: `npm install -D @playwright/test`
- Python packages: watchdog, pathspec
"""

