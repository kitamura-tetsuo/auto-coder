#!/usr/bin/env python3
"""
Quick verification script for test fixes.
"""


def test_backend_manager_initialization():
    """Test that backend manager can be properly initialized."""
    from unittest.mock import Mock

    from src.auto_coder.backend_manager import LLMBackendManager

    # Reset singleton
    LLMBackendManager.reset_singleton()

    # Create mock clients
    mock_gemini_client = Mock()
    mock_gemini_client.model_name = "gemini-2.5-pro"

    # Initialize manager
    try:
        manager = LLMBackendManager.get_llm_instance(
            default_backend="gemini",
            default_client=mock_gemini_client,
            factories={"gemini": lambda: mock_gemini_client},
        )

        # Test get_last_backend_and_model
        backend, model = manager.get_last_backend_and_model()
        print(f"Backend: {backend}, Model: {model}")

        # Test automation engine
        from unittest.mock import Mock

        from src.auto_coder.automation_engine import AutomationEngine

        mock_github = Mock()
        engine = AutomationEngine(mock_github)

        # Debug the _get_llm_backend_info method
        print("Testing _get_llm_backend_info...")
        try:
            info = engine._get_llm_backend_info()
            print(f"Engine info: {info}")
        except Exception as e:
            print(f"Exception in _get_llm_backend_info: {e}")
            import traceback

            traceback.print_exc()
            raise

        # Test LLMBackendManager.is_initialized() directly
        print("Testing LLMBackendManager.is_initialized()...")
        from src.auto_coder.backend_manager import LLMBackendManager

        is_initialized = LLMBackendManager.is_initialized()
        print(f"Is manager initialized: {is_initialized}")

        # Let's also check the singleton state
        print("Checking singleton state...")
        instance = LLMBackendManager._instance
        print(f"_instance: {instance}")
        print(f"_init_params: {LLMBackendManager._init_params}")

        # Check instance internal state
        if instance:
            print(f"Instance type: {type(instance)}")
            print(
                f"Instance _last_backend: {getattr(instance, '_last_backend', 'MISSING')}"
            )
            print(
                f"Instance _last_model: {getattr(instance, '_last_model', 'MISSING')}"
            )
            print(
                f"Instance _current_idx: {getattr(instance, '_current_idx', 'MISSING')}"
            )
            print(
                f"Instance _all_backends: {getattr(instance, '_all_backends', 'MISSING')}"
            )
            print(
                f"Instance _default_backend: {getattr(instance, '_default_backend', 'MISSING')}"
            )

            # Test current backend name
            try:
                current_backend_name = instance._current_backend_name()
                print(f"Current backend name: {current_backend_name}")

                # Test get_last_backend_and_model directly on instance
                print("Testing get_last_backend_and_model on instance directly...")
                direct_backend, direct_model = instance.get_last_backend_and_model()
                print(f"Direct instance call result: {direct_backend}, {direct_model}")

            except Exception as e:
                print(f"Error testing instance methods: {e}")
                import traceback

                traceback.print_exc()

        # Also test direct get_llm_backend_manager call
        print("Testing get_llm_backend_manager directly...")
        from src.auto_coder.backend_manager import get_llm_backend_manager

        try:
            direct_manager = get_llm_backend_manager()
            print(f"Direct manager call successful: {direct_manager}")
            direct_backend, direct_model = direct_manager.get_last_backend_and_model()
            print(f"Direct call result: {direct_backend}, {direct_model}")
        except Exception as e:
            print(f"Direct manager call failed: {e}")
            import traceback

            traceback.print_exc()

        assert info["backend"] == "gemini", f"Expected 'gemini', got {info['backend']}"
        assert (
            info["model"] == "gemini-2.5-pro"
        ), f"Expected 'gemini-2.5-pro', got {info['model']}"

        print("✅ All tests passed!")
        return True

    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback

        traceback.print_exc()
        return False


def test_git_utils_fix():
    """Test that git utils fix works."""
    from src.auto_coder.git_utils import check_unpushed_commits

    try:
        # This should not crash even if git returns "Everything up-to-date"
        result = check_unpushed_commits()
        print(f"Git utils test result: {result}")
        print("✅ Git utils test passed!")
        return True
    except Exception as e:
        print(f"❌ Git utils test failed: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("Running test verification...")

    success = True
    success &= test_backend_manager_initialization()
    success &= test_git_utils_fix()

    if success:
        print("\n🎉 All verification tests passed!")
    else:
        print("\n💥 Some tests failed!")

    exit(0 if success else 1)
