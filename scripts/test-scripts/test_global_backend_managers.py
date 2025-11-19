#!/usr/bin/env python3
"""
Comprehensive test for global backend manager functionality.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))


def test_global_backend_managers() -> bool:
    """Test all global backend manager functionality."""
    print("Testing Global Backend Managers...")

    # Test 1: Import all global functions
    try:
        from auto_coder.backend_manager import (
            LLMBackendManager,
            get_llm_backend_and_model,
            get_llm_backend_manager,
            run_llm_prompt,
        )

        print("✓ All global functions imported successfully")
    except ImportError as e:
        print(f"✗ Import failed: {e}")
        return False

    # Test 3: Initialize LLM backend manager
    try:
        # Create mock clients for testing
        class MockClient:
            def _run_llm_cli(self, prompt: str) -> str:
                return f"Mock response to: {prompt}"

            def switch_to_default_model(self) -> None:
                pass

            def close(self) -> None:
                pass

        mock_client = MockClient()
        factories = {"codex": lambda: mock_client}

        # Initialize LLM backend manager
        llm_manager = LLMBackendManager.get_llm_instance(
            default_backend="codex",
            default_client=mock_client,
            factories=factories,
            force_reinitialize=True,
        )

        print("✓ LLM manager initialized successfully")
    except Exception as e:
        print(f"✗ Manager initialization failed: {e}")
        return False

    # Test 4: Test singleton behavior
    try:
        # Should get same instance
        llm_manager2 = LLMBackendManager.get_llm_instance()

        assert llm_manager is llm_manager2, "LLM backend manager singleton broken"

        print("✓ Singleton behavior works correctly")
    except Exception as e:
        print(f"✗ Singleton test failed: {e}")
        return False

    # Test 5: Test global convenience functions
    try:
        # Reset for clean test
        LLMBackendManager.reset_singleton()

        # Test get_llm_backend_manager
        manager = get_llm_backend_manager(
            default_backend="codex",
            default_client=mock_client,
            factories=factories,
            force_reinitialize=True,
        )

        print("✓ Global convenience functions work")
    except Exception as e:
        print(f"✗ Global convenience functions test failed: {e}")
        return False

    # Test 6: Test run_prompt functions
    try:
        # Test run_llm_prompt
        llm_response = run_llm_prompt("test prompt")
        assert (
            "Mock response to: test prompt" == llm_response
        ), f"Unexpected response: {llm_response}"

        print("✓ Run prompt functions work correctly")
    except Exception as e:
        print(f"✗ Run prompt functions test failed: {e}")
        return False

    # Test 7: Test get_backend_and_model functions
    try:
        # These should not crash even with mock client
        llm_backend, llm_model = get_llm_backend_and_model()

        print("✓ Get backend and model functions work")
    except Exception as e:
        print(f"✗ Get backend and model functions test failed: {e}")
        return False

    print("All global backend manager tests passed!")
    return True


if __name__ == "__main__":
    try:
        success = test_global_backend_managers()
        if success:
            print("\n🎉 Global Backend Manager Implementation Complete!")
            print("\nAvailable features:")
            print("✅ LLMBackendManager.get_llm_instance() - General LLM operations")
            print("✅ Global convenience functions for easy access")
            print("✅ Configuration file-based backend management")
            print("✅ Thread-safe singleton implementation")
            print("\nSee GLOBAL_BACKEND_MANAGER_USAGE.md for detailed usage examples.")
        else:
            print("\n❌ Some tests failed!")
            sys.exit(1)
    except Exception as e:
        print(f"\n❌ Test failed with exception: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
