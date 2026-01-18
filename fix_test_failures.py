#!/usr/bin/env python3
"""
Script to fix test failures systematically after refactoring.
"""

import re

def fix_exclusive_processing_label() -> None:
    """Fix test_exclusive_processing_label.py test failures."""

    # Read the current file
    with open('tests/test_exclusive_processing_label.py', 'r') as f:
        content = f.read()

    # Fix function signature issues
    # Replace old function calls with correct parameter counts

    # Fix _process_issues_normal calls (remove extra parameters)
    content = re.sub(
        r'_process_issues_normal\([^,]+,[^,]+,[^,]+,\s*None\s*\)',
        r'_process_issues_normal(\1, \2, \3)',
        content
    )

    # Fix process_pull_requests calls (remove extra parameters)
    content = re.sub(
        r'process_pull_requests\([^,]+,[^,]+,[^,]+,\s*None\s*\)',
        r'process_pull_requests(\1, \2, \3)',
        content
    )

    # Write back the file
    with open('tests/test_exclusive_processing_label.py', 'w') as f:
        f.write(content)

    print("Fixed test_exclusive_processing_label.py")

def fix_issue_processor_skip_sub_issues() -> None:
    """Fix test_issue_processor_skip_sub_issues.py test failures."""

    # Read the current file
    with open('tests/test_issue_processor_skip_sub_issues.py', 'r') as f:
        content = f.read()

    # Fix _process_issues_normal calls (remove llm_client parameter)
    content = content.replace(
        '_process_issues_normal(repo_name, issue_data, config, llm_client=None)',
        '_process_issues_normal(repo_name, issue_data, config)'
    )

    # Write back the file
    with open('tests/test_issue_processor_skip_sub_issues.py', 'w') as f:
        f.write(content)

    print("Fixed test_issue_processor_skip_sub_issues.py")

if __name__ == "__main__":
    print("Fixing test failures...")

    try:
        fix_exclusive_processing_label()
        fix_issue_processor_skip_sub_issues()
        print("All test fixes applied successfully!")
    except Exception as e:
        print(f"Error fixing tests: {e}")
        import traceback
        traceback.print_exc()