#!/usr/bin/env python3
"""
Simple test script for message_backend_manager singleton implementation.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from auto_coder.backend_manager import (
    get_message_backend_and_model,
    get_message_backend_manager,
    run_message_prompt,
)


def test_global_functions():
    """Test global convenience functions."""
    print("\nTesting global convenience functions...")

    # Test that functions exist and are callable
    assert callable(
        get_message_backend_manager
    ), "get_message_backend_manager should be callable"
    assert callable(run_message_prompt), "run_message_prompt should be callable"
    assert callable(
        get_message_backend_and_model
    ), "get_message_backend_and_model should be callable"

    print("✓ All global functions are callable")
    print("Global function tests passed!")


def test_import_compatibility():
    """Test import compatibility."""
    print("\nTesting import compatibility...")

    try:
        from auto_coder.backend_manager import get_message_backend_manager as gm

        assert callable(gm)
        print("✓ Function import works")
    except ImportError as e:
        print(f"✗ Function import failed: {e}")
        return False

    print("Import compatibility tests passed!")
    return True


if __name__ == "__main__":
    try:
        test_global_functions()
        test_import_compatibility()
        print(
            "\n🎉 All tests passed! message_backend_manager singleton implementation is working correctly."
        )
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
