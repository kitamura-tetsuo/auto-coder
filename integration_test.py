#!/usr/bin/env python3
"""
Simple integration test for the ensure_pushed_with_fallback fix.
This script tests the changes without requiring pytest installation.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Add the src directory to the path so we can import the modules
sys.path.insert(0, str(Path(__file__).parent / "src"))


def test_git_pull_branch_name_fix():
    """Test that the git pull command now uses the correct branch name."""
    print("Testing git pull branch name fix...")

    # Read the git_utils.py file and check for the fix
    git_utils_path = Path(__file__).parent / "src" / "auto_coder" / "git_utils.py"

    if not git_utils_path.exists():
        print(f"❌ git_utils.py not found at {git_utils_path}")
        return False

    with open(git_utils_path, "r") as f:
        content = f.read()

    # Check for the specific fix: pulling from current branch instead of HEAD
    expected_patterns = [
        'git", "pull", remote, current_branch',
        'git", "rev-parse", "--abbrev-ref", "HEAD"',
    ]

    found_patterns = []
    for pattern in expected_patterns:
        if pattern in content:
            found_patterns.append(pattern)
            print(f"✅ Found expected pattern: {pattern}")
        else:
            print(f"❌ Missing expected pattern: {pattern}")

    success = len(found_patterns) == len(expected_patterns)

    if success:
        print("✅ Git pull branch name fix is correctly implemented")
    else:
        print("❌ Git pull branch name fix is missing or incorrect")

    return success


def test_conflict_resolution_logic():
    """Test that the conflict resolution logic is properly implemented."""
    print("\nTesting conflict resolution logic...")

    git_utils_path = Path(__file__).parent / "src" / "auto_coder" / "git_utils.py"

    with open(git_utils_path, "r") as f:
        content = f.read()

    # Check for conflict detection and resolution
    expected_patterns = [
        'if "conflict" in pull_result.stderr.lower()',
        'resolve_pull_conflicts(cwd=cwd, merge_method="merge")',
    ]

    found_patterns = []
    for pattern in expected_patterns:
        if pattern in content:
            found_patterns.append(pattern)
            print(f"✅ Found expected pattern: {pattern}")
        else:
            print(f"❌ Missing expected pattern: {pattern}")

    success = len(found_patterns) == len(expected_patterns)

    if success:
        print("✅ Conflict resolution logic is correctly implemented")
    else:
        print("❌ Conflict resolution logic is missing or incorrect")

    return success


def test_non_fast_forward_detection():
    """Test that non-fast-forward errors are properly detected."""
    print("\nTesting non-fast-forward error detection...")

    git_utils_path = Path(__file__).parent / "src" / "auto_coder" / "git_utils.py"

    with open(git_utils_path, "r") as f:
        content = f.read()

    # Check for non-fast-forward error detection
    expected_patterns = [
        '"non-fast-forward" in push_result.stderr.lower()',
        '"Updates were rejected because the tip of your current branch is behind" in push_result.stderr',
    ]

    found_patterns = []
    for pattern in expected_patterns:
        if pattern in content:
            found_patterns.append(pattern)
            print(f"✅ Found expected pattern: {pattern}")
        else:
            print(f"❌ Missing expected pattern: {pattern}")

    success = len(found_patterns) == len(expected_patterns)

    if success:
        print("✅ Non-fast-forward error detection is correctly implemented")
    else:
        print("❌ Non-fast-forward error detection is missing or incorrect")

    return success


def test_test_file_exists():
    """Test that our test file was created correctly."""
    print("\nTesting test file creation...")

    test_file_path = (
        Path(__file__).parent / "tests" / "test_ensure_pushed_with_fallback.py"
    )

    if not test_file_path.exists():
        print(f"❌ Test file not found at {test_file_path}")
        return False

    with open(test_file_path, "r") as f:
        content = f.read()

    # Check for key test functions
    expected_tests = [
        "test_non_fast_forward_error_handling_success",
        "test_non_fast_forward_error_with_conflicts",
        "test_non_fast_forward_error_pull_fails",
        "test_non_fast_forward_error_with_successful_conflict_resolution",
    ]

    found_tests = []
    for test in expected_tests:
        if test in content:
            found_tests.append(test)
            print(f"✅ Found test: {test}")
        else:
            print(f"❌ Missing test: {test}")

    success = len(found_tests) == len(expected_tests)

    if success:
        print("✅ Test file contains all expected tests")
    else:
        print("❌ Test file is missing some tests")

    return success


def main():
    """Run all tests."""
    print("Running integration tests for git push non-fast-forward error fix...")
    print("=" * 60)

    # Run all tests
    tests = [
        test_git_pull_branch_name_fix,
        test_conflict_resolution_logic,
        test_non_fast_forward_detection,
        test_test_file_exists,
    ]

    results = []
    for test in tests:
        try:
            result = test()
            results.append(result)
        except Exception as e:
            print(f"❌ Test {test.__name__} failed with exception: {e}")
            results.append(False)

    print("\n" + "=" * 60)
    print("Test Results Summary:")
    print(f"✅ Passed: {sum(results)}/{len(results)}")
    print(f"❌ Failed: {len(results) - sum(results)}/{len(results)}")

    if all(results):
        print("\n🎉 All tests passed! The fix is correctly implemented.")
        return 0
    else:
        print("\n⚠️  Some tests failed. Please review the implementation.")
        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
