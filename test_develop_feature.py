#!/usr/bin/env python3
"""
Test feature for develop branch PR verification.

This file is created to test that the auto-coder tool correctly
identifies and merges the PR's base branch (develop) instead of main.
"""

def test_develop_feature():
    """Test function for develop branch feature."""
    print("This is a test feature targeting develop branch")
    return "develop-feature-works"

if __name__ == "__main__":
    result = test_develop_feature()
    print(f"Result: {result}")
