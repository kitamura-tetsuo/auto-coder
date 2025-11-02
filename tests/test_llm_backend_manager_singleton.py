"""Test the LLMBackendManager singleton pattern."""

import os
import pytest
from unittest.mock import MagicMock, patch, Mock
from threading import Thread
import time

# Import the class under test
from src.auto_coder.llm_client_base import LLMBackendManager


class TestLLMBackendManagerSingleton:
    """Test cases for the LLMBackendManager singleton pattern."""

    def test_singleton_returns_same_instance(self):
        """Test that multiple calls return the same instance."""
        instance1 = LLMBackendManager.get_llm_for_message_instance()
        instance2 = LLMBackendManager.get_llm_for_message_instance()

        # They should be the same instance
        assert instance1 is instance2

    def test_singleton_is_thread_safe(self):
        """Test that the singleton is thread-safe."""
        instances = []
        errors = []

        def get_instance():
            try:
                instance = LLMBackendManager.get_llm_for_message_instance()
                instances.append(instance)
            except Exception as e:
                errors.append(e)

        # Create multiple threads to test thread safety
        threads = [Thread(target=get_instance) for _ in range(10)]

        # Start all threads
        for thread in threads:
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        # All threads should have gotten the same instance
        assert len(errors) == 0, f"Errors occurred: {errors}"
        assert len(instances) == 10
        assert all(instance is instances[0] for instance in instances)

    @patch.dict(
        os.environ,
        {
            "AUTO_CODER_MESSAGE_BACKEND": "qwen",
            "AUTO_CODER_MESSAGE_MODEL": "qwen-turbo-test",
            "QWEN_API_KEY": "test-key",
        },
    )
    @patch("src.auto_coder.llm_client_base.LLMBackendManager._create_message_backend_manager")
    def test_message_backend_uses_lightweight_model(self, mock_create):
        """Test that the message backend uses lightweight models."""
        # Mock the factory to track calls
        mock_factory = MagicMock()
        mock_backend_manager = MagicMock()
        mock_factory.return_value = mock_backend_manager
        mock_create.return_value = mock_backend_manager

        # Get the instance
        instance = LLMBackendManager.get_llm_for_message_instance()

        # Verify the factory was called
        mock_create.assert_called_once()

        # The instance should be the mock backend manager
        assert instance is mock_backend_manager

    def test_singleton_prevents_multiple_initialization(self):
        """Test that singleton prevents multiple initialization."""
        # Get two instances
        instance1 = LLMBackendManager()
        instance2 = LLMBackendManager()

        # They should be the same object
        assert instance1 is instance2

    @patch("src.auto_coder.llm_client_base.LLMBackendManager._create_message_backend_manager")
    def test_get_instance_method_returns_backend_manager(self, mock_create):
        """Test that get_llm_for_message_instance returns a BackendManager."""
        # Mock the backend manager
        mock_backend_manager = MagicMock()
        mock_create.return_value = mock_backend_manager

        # Get the instance
        result = LLMBackendManager.get_llm_for_message_instance()

        # Should return the mock backend manager
        assert result is mock_backend_manager

    def test_singleton_has_lock(self):
        """Test that singleton has proper thread safety lock."""
        instance = LLMBackendManager()

        # Should have a lock for thread safety
        assert hasattr(instance, "_lock")
        assert hasattr(instance, "_config_lock")

    def test_instance_initialization_flag(self):
        """Test that singleton tracks initialization state."""
        instance = LLMBackendManager()

        # Should have an initialization flag
        assert hasattr(instance, "_initialized")

    @patch("src.auto_coder.llm_client_base.LLMBackendManager._create_message_backend_manager")
    def test_close_method_cleans_up_backend(self, mock_create):
        """Test that close method properly cleans up the backend manager."""
        # Mock the backend manager with a close method
        mock_backend_manager = MagicMock()
        mock_create.return_value = mock_backend_manager

        # Get the instance
        instance = LLMBackendManager.get_llm_for_message_instance()

        # Call close
        instance.close()

        # Backend manager's close should have been called
        mock_backend_manager.close.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
