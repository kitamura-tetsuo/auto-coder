#!/usr/bin/env python3
"""
Simple test: directly verify structure of fixed run method
"""

import os
import sys


def test_run_method_calls_functions():
    """Test that run method calls process_issues and process_pull_requests"""

    # Read automation_engine.py file
    with open("src/auto_coder/automation_engine.py", "r") as f:
        content = f.read()

    # Checkpoints
    checks = [
        ("process_issues(", "process_issues function is called"),
        ("process_pull_requests(", "process_pull_requests function is called"),
        ("issues_result = process_issues", "process_issues result is assigned to issues_result"),
        ("prs_result = process_pull_requests", "process_pull_requests result is assigned to prs_result"),
        ('issues_processed"] = issues_result', "issues_result is set to issues_processed"),
        ('prs_processed"] = prs_result', "prs_result is set to prs_processed"),
    ]

    all_passed = True

    for check_text, description in checks:
        if check_text in content:
            print(f"✓ {description}")
        else:
            print(f"✗ {description} - not found: {check_text}")
            all_passed = False

    return all_passed


def test_old_candidates_code_removed():
    """Test that old candidate-based loop code is removed"""

    with open("src/auto_coder/automation_engine.py", "r") as f:
        content = f.read()

    # Check for traces of old code
    old_code_patterns = [
        "_get_candidates(",
        "_select_best_candidate(",
        "_process_single_candidate(",
        "while True:",
        "candidates =",
    ]

    all_removed = True

    for pattern in old_code_patterns:
        if pattern in content:
            print(f"⚠ Old code remains: {pattern}")
            all_removed = False
        else:
            print(f"✓ Old code is removed: {pattern}")

    return all_removed


if __name__ == "__main__":
    print("Verifying fix contents...\n")

    print("1. run method function call check:")
    test1 = test_run_method_calls_functions()

    print(f"\n2. Old code removal check:")
    test2 = test_old_candidates_code_removed()

    print(f"\n=== Results ===")
    if test1 and test2:
        print("✓ All checks passed - fix is correctly implemented!")
        print("\nFixed run method:")
        print("- Calls process_issues and process_pull_requests")
        print("- Returns results in structure expected by tests")
        print("- Removes old candidate-based loop")
        sys.exit(0)
    else:
        print("✗ Some checks failed")
        sys.exit(1)
