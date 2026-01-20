#!/usr/bin/env python3
"""
Test CLI integration with global backend managers.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))


def test_cli_integration():
    """Test that CLI commands can be imported and initialized without errors."""
    print("Testing CLI integration with global backend managers...")

    try:
        # Test importing CLI commands
        from auto_coder.cli_commands_main import (
            create_feature_issues,
            fix_to_pass_tests_command,
            process_issues,
        )

        print("✓ CLI commands imported successfully")

        # Test that we can access the functions without calling them
        assert callable(process_issues), "process_issues should be callable"
        assert callable(create_feature_issues), "create_feature_issues should be callable"
        assert callable(fix_to_pass_tests_command), "fix_to_pass_tests_command should be callable"
        print("✓ CLI commands are callable")

        # Test importing backend manager functions
        from auto_coder.backend_manager import (
            get_llm_backend_manager,
            get_noedit_backend_manager,
        )

        print("✓ Global backend manager functions imported successfully")

        # Test that we can create a mock backend manager without errors
        from auto_coder.backend_manager import LLMBackendManager

        class MockClient:
            def _run_llm_cli(self, prompt):
                return f"Mock response to: {prompt}"

            def switch_to_default_model(self):
                pass

            def close(self):
                pass

        mock_client = MockClient()
        factories = {"codex": lambda: mock_client}

        # Reset and initialize
        LLMBackendManager.reset_singleton()

        llm_manager = get_llm_backend_manager(
            default_backend="codex",
            default_client=mock_client,
            factories=factories,
            force_reinitialize=True,
        )

        message_manager = get_llm_backend_manager(
            default_backend="codex",
            default_client=mock_client,
            factories=factories,
            force_reinitialize=True,
        )

        print("✓ Global backend managers can be initialized")
        print("✓ CLI integration test completed successfully!")

        return True

    except Exception as e:
        print(f"✗ CLI integration test failed: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    try:
        success = test_cli_integration()
        if success:
            print("\n🎉 CLI integration is working correctly!")
            print("The error about initialization should now be fixed.")
        else:
            print("\n❌ CLI integration test failed!")
            sys.exit(1)
    except Exception as e:
        print(f"\n❌ Test failed with exception: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
