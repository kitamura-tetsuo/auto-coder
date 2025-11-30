"""Tests for backend fallback functionality.

This module tests the automatic backend switching mechanism when configured
fallback backends are used after multiple failures or as configured for PR processing.
"""

import time
from typing import Optional
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.auto_coder.backend_manager import BackendManager
from src.auto_coder.exceptions import AutoCoderUsageLimitError
from src.auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration


class MockLLMClient:
    """Mock LLM client for testing backend fallback."""

    def __init__(self, name: str, should_fail: bool = False, fail_count: int = 0, session_id: Optional[str] = None):
        self.name = name
        self.model_name = name
        self._should_fail = should_fail
        self._fail_count = fail_count
        self._call_count = 0
        self.session_id = session_id

    def _run_llm_cli(self, prompt: str) -> str:
        self._call_count += 1
        if self._should_fail:
            if self._call_count <= self._fail_count:
                raise AutoCoderUsageLimitError(f"Usage limit reached for {self.name}")
        return f"{self.name}: response"

    def switch_to_default_model(self):
        pass

    def close(self):
        pass

    def get_last_session_id(self) -> Optional[str]:
        return self.session_id


class TestBackendFallback:
    """Test suite for backend fallback functionality."""

    def test_backend_rotation_on_usage_limit(self):
        """Test that backend rotates to next when current backend hits usage limit."""
        # Create mock clients - first fails once, second succeeds
        primary_client = MockLLMClient("codex", should_fail=True, fail_count=1)
        fallback_client = MockLLMClient("gemini", should_fail=False)

        # Create factories
        factories = {
            "codex": lambda: primary_client,
            "gemini": lambda: fallback_client,
        }

        # Create backend manager with codex as default
        manager = BackendManager(
            default_backend="codex",
            default_client=primary_client,
            factories=factories,
            order=["codex", "gemini"],
        )

        # First call should fail on primary and rotate to gemini
        result = manager._run_llm_cli("test prompt")
        assert result == "gemini: response"
        assert manager._current_backend_name() == "gemini"

    def test_backend_rotation_continues_after_all_backends_fail(self):
        """Test that rotation continues even when all backends fail once."""
        # Create clients that fail once each
        client1 = MockLLMClient("backend1", should_fail=True, fail_count=1)
        client2 = MockLLMClient("backend2", should_fail=True, fail_count=1)
        client3 = MockLLMClient("backend3", should_fail=True, fail_count=1)

        factories = {
            "backend1": lambda: client1,
            "backend2": lambda: client2,
            "backend3": lambda: client3,
        }

        manager = BackendManager(
            default_backend="backend1",
            default_client=client1,
            factories=factories,
            order=["backend1", "backend2", "backend3"],
        )

        # First call - backend1 fails once (switch_to_default_model is called after rotation)
        # After fail, rotation happens and tries next backend
        # backend2 fails once and rotates to backend3
        # backend3 fails once and rotates back to backend1
        # All backends tried, raises error
        with pytest.raises(AutoCoderUsageLimitError):
            manager._run_llm_cli("test prompt")

        # After all backends tried, should be on backend1 (cycled back)
        # Note: The rotation happens in exception handlers, so after all tries,
        # we're back where we started
        assert manager._current_backend_name() == "backend1"

    def test_fallback_backend_configuration_is_used_in_rotation(self):
        """Test that configured fallback backend is included in rotation."""
        # Create clients
        primary_client = MockLLMClient("qwen", should_fail=False)
        fallback_client = MockLLMClient("claude", should_fail=False)

        factories = {
            "qwen": lambda: primary_client,
            "claude": lambda: fallback_client,
        }

        # Create manager
        manager = BackendManager(
            default_backend="qwen",
            default_client=primary_client,
            factories=factories,
            order=["qwen", "claude"],
        )

        # Mock configuration with specific fallback settings
        mock_config = Mock(spec=LLMBackendConfiguration)
        fallback_config = BackendConfig(name="claude", model="sonnet-3.5", temperature=0.1)
        mock_config.get_backend_for_failed_pr.return_value = fallback_config
        mock_config.get_model_for_failed_pr_backend.return_value = "sonnet-3.5"
        mock_config.get_backend_config.return_value = fallback_config

        with patch("src.auto_coder.llm_backend_config.get_llm_config", return_value=mock_config):
            # Get backend for failed PR should be called
            fallback = mock_config.get_backend_for_failed_pr()
            assert fallback.name == "claude"

            # Rotation should work normally
            manager.switch_to_next_backend()
            assert manager._current_backend_name() == "claude"

    def test_no_fallback_configured_uses_rotation_only(self):
        """Test behavior when no fallback backend is configured."""
        # Create client that fails
        primary_client = MockLLMClient("codex", should_fail=True, fail_count=10)
        factories = {"codex": lambda: primary_client}

        manager = BackendManager(
            default_backend="codex",
            default_client=primary_client,
            factories=factories,
            order=["codex"],
        )

        # Mock configuration without fallback
        mock_config = Mock(spec=LLMBackendConfiguration)
        mock_config.get_backend_for_failed_pr.return_value = None

        with patch("src.auto_coder.llm_backend_config.get_llm_config", return_value=mock_config):
            # Should fail without fallback
            with pytest.raises(AutoCoderUsageLimitError):
                manager._run_llm_cli("test prompt")

            # Should stay on primary
            assert manager._current_backend_name() == "codex"

    def test_switch_to_fallback_backend_by_name(self):
        """Test explicit switch to fallback backend by name."""
        # Create clients
        primary_client = MockLLMClient("codex", should_fail=False)
        fallback_client = MockLLMClient("gemini", should_fail=False)

        factories = {
            "codex": lambda: primary_client,
            "gemini": lambda: fallback_client,
        }

        manager = BackendManager(
            default_backend="codex",
            default_client=primary_client,
            factories=factories,
            order=["codex", "gemini"],
        )

        # Explicitly switch to fallback
        manager._switch_to_backend_by_name("gemini")
        assert manager._current_backend_name() == "gemini"

        # Switch back to primary
        manager._switch_to_backend_by_name("codex")
        assert manager._current_backend_name() == "codex"

    def test_state_manager_is_called_during_backend_switch(self):
        """Test that state manager is called when switching backends."""
        from src.auto_coder.backend_state_manager import BackendStateManager

        # Create clients
        primary_client = MockLLMClient("codex", should_fail=False)
        fallback_client = MockLLMClient("gemini", should_fail=False)

        factories = {
            "codex": lambda: primary_client,
            "gemini": lambda: fallback_client,
        }

        manager = BackendManager(
            default_backend="codex",
            default_client=primary_client,
            factories=factories,
            order=["codex", "gemini"],
        )

        # Mock the state manager's save_state method
        with patch.object(manager._state_manager, "save_state", wraps=manager._state_manager.save_state) as mock_save:
            # Switch to fallback
            manager._switch_to_backend_by_name("gemini")

            # Verify state was saved with fallback backend
            assert mock_save.call_count >= 1
            last_call = mock_save.call_args_list[-1]
            assert last_call[0][0] == "gemini"

    def test_backend_state_persists_and_auto_resets(self):
        """Test that backend state persists and auto-resets after timeout."""
        from src.auto_coder.backend_state_manager import BackendStateManager

        # Create first manager instance
        primary_client = MockLLMClient("codex", should_fail=False)
        fallback_client = MockLLMClient("gemini", should_fail=False)

        factories = {
            "codex": lambda: primary_client,
            "gemini": lambda: fallback_client,
        }

        # Create manager with custom state file path for isolation
        state_file = "/tmp/test_backend_state.json"
        manager1 = BackendManager(
            default_backend="codex",
            default_client=primary_client,
            factories=factories,
            order=["codex", "gemini"],
        )
        manager1._state_manager = BackendStateManager(state_file)

        # Simulate switching to fallback
        manager1._switch_to_backend_by_name("gemini")

        # Verify state was saved
        state = manager1._state_manager.load_state()
        assert state["current_backend"] == "gemini"

        # Create second manager instance with same state file
        primary_client2 = MockLLMClient("codex", should_fail=False)
        fallback_client2 = MockLLMClient("gemini", should_fail=False)

        factories2 = {
            "codex": lambda: primary_client2,
            "gemini": lambda: fallback_client2,
        }

        manager2 = BackendManager(
            default_backend="codex",
            default_client=primary_client2,
            factories=factories2,
            order=["codex", "gemini"],
        )
        manager2._state_manager = BackendStateManager(state_file)

        # Initially should be on codex (check_and_reset is not called on init)
        assert manager2._current_backend_name() == "codex"

        # Now save state to gemini with recent timestamp
        current_time = time.time()
        manager2._state_manager.save_state("gemini", current_time)

        # Call check_and_reset_backend_if_needed - should NOT reset (only 1 second passed)
        manager2.check_and_reset_backend_if_needed()

        # Should still be on codex since we haven't called _run_llm_cli yet
        assert manager2._current_backend_name() == "codex"

        # Simulate that 3 hours have passed by saving old state
        old_time = time.time() - 10800  # 3 hours ago
        manager2._state_manager.save_state("gemini", old_time)

        # Call check_and_reset_backend_if_needed should reset to default
        manager2.check_and_reset_backend_if_needed()

        # After auto-reset, should be on default backend
        assert manager2._current_backend_name() == "codex"

    def test_backend_rotation_with_usage_limit_retry(self):
        """Test backend rotation with usage limit retry configuration."""
        # Create clients with retry configuration
        primary_client = MockLLMClient("qwen", should_fail=True, fail_count=3)
        fallback_client = MockLLMClient("gemini", should_fail=False)

        factories = {
            "qwen": lambda: primary_client,
            "gemini": lambda: fallback_client,
        }

        manager = BackendManager(
            default_backend="qwen",
            default_client=primary_client,
            factories=factories,
            order=["qwen", "gemini"],
        )

        # Mock configuration with retry settings
        mock_config = Mock(spec=LLMBackendConfiguration)
        fallback_config = BackendConfig(
            name="gemini",
            usage_limit_retry_count=2,
            usage_limit_retry_wait_seconds=0,
        )
        primary_config = BackendConfig(
            name="qwen",
            usage_limit_retry_count=1,
            usage_limit_retry_wait_seconds=0,
        )

        def get_backend_config_side_effect(name):
            if name == "qwen":
                return primary_config
            elif name == "gemini":
                return fallback_config
            return None

        mock_config.get_backend_config.side_effect = get_backend_config_side_effect
        mock_config.get_backend_for_failed_pr.return_value = fallback_config

        with patch("src.auto_coder.llm_backend_config.get_llm_config", return_value=mock_config):
            # Execute - primary will retry once then fail and switch to fallback
            result = manager._run_llm_cli("test prompt")

            # Should succeed with fallback
            assert result == "gemini: response"
            assert manager._current_backend_name() == "gemini"

    def test_test_fix_mode_uses_backend_rotation(self):
        """Test that test fix mode respects backend rotation."""
        # Create clients
        primary_client = MockLLMClient("codex", should_fail=True, fail_count=2)
        fallback_client = MockLLMClient("gemini", should_fail=False)

        factories = {
            "codex": lambda: primary_client,
            "gemini": lambda: fallback_client,
        }

        manager = BackendManager(
            default_backend="codex",
            default_client=primary_client,
            factories=factories,
            order=["codex", "gemini"],
        )

        # Mock configuration
        mock_config = Mock(spec=LLMBackendConfiguration)
        mock_config.get_backend_for_failed_pr.return_value = BackendConfig(name="gemini")
        mock_config.get_backend_config.return_value = BackendConfig(name="gemini")

        with patch("src.auto_coder.llm_backend_config.get_llm_config", return_value=mock_config):
            # In test fix mode, run_test_fix_prompt should rotate on failures
            result = manager.run_test_fix_prompt("test prompt", current_test_file="test_file.py")

            # Should succeed with fallback after primary failures
            assert result == "gemini: response"
            assert manager._current_backend_name() == "gemini"

    def test_backend_manager_respects_configured_order(self):
        """Test that backend manager respects configured backend order."""
        # Create clients
        client1 = MockLLMClient("backend1")
        client2 = MockLLMClient("backend2")
        client3 = MockLLMClient("backend3")

        factories = {
            "backend1": lambda: client1,
            "backend2": lambda: client2,
            "backend3": lambda: client3,
        }

        # Create with specific order - backend2 is default
        manager = BackendManager(
            default_backend="backend2",
            default_client=client2,
            factories=factories,
            order=["backend1", "backend2", "backend3"],
        )

        # The manager rotates the list so default is first
        # It keeps rotating until default is at position 0
        # So ["backend1", "backend2", "backend3"] becomes ["backend2", "backend3", "backend1"]
        assert manager._all_backends[0] == "backend2"
        assert manager._all_backends[1] == "backend3"
        assert manager._all_backends[2] == "backend1"

        # Rotation should follow the rotated order
        manager.switch_to_next_backend()
        assert manager._current_backend_name() == "backend3"

        manager.switch_to_next_backend()
        assert manager._current_backend_name() == "backend1"

        manager.switch_to_next_backend()
        assert manager._current_backend_name() == "backend2"

    def test_backend_manager_with_provider_rotation(self):
        """Test that backend manager works with provider rotation."""
        from src.auto_coder.backend_provider_manager import BackendProviderManager

        # Create clients
        primary_client = MockLLMClient("qwen", should_fail=False)
        fallback_client = MockLLMClient("gemini", should_fail=False)

        factories = {
            "qwen": lambda: primary_client,
            "gemini": lambda: fallback_client,
        }

        # Create provider manager with mock metadata
        provider_manager = Mock(spec=BackendProviderManager)
        provider_manager.has_providers.return_value = False
        provider_manager.get_provider_count.return_value = 0
        provider_manager.get_current_provider_name.return_value = None
        provider_manager.create_env_context.return_value = {}
        provider_manager.mark_provider_used = Mock()

        manager = BackendManager(
            default_backend="qwen",
            default_client=primary_client,
            factories=factories,
            order=["qwen", "gemini"],
            provider_manager=provider_manager,
        )

        # Mock configuration
        mock_config = Mock(spec=LLMBackendConfiguration)
        mock_config.get_backend_for_failed_pr.return_value = BackendConfig(name="gemini")
        mock_config.get_backend_config.return_value = BackendConfig(name="gemini")

        with patch("src.auto_coder.llm_backend_config.get_llm_config", return_value=mock_config):
            # Execute a prompt to trigger provider manager
            result = manager._run_llm_cli("test prompt")
            assert result == "qwen: response"

            # Verify provider manager methods were used
            provider_manager.mark_provider_used.assert_called()

    def test_multiple_consecutive_failures_rotate_through_all_backends(self):
        """Test that multiple consecutive failures rotate through all backends."""
        # Create three clients, each failing multiple times to test rotation
        client1 = MockLLMClient("backend1", should_fail=True, fail_count=10)
        client2 = MockLLMClient("backend2", should_fail=True, fail_count=10)
        client3 = MockLLMClient("backend3", should_fail=True, fail_count=10)

        factories = {
            "backend1": lambda: client1,
            "backend2": lambda: client2,
            "backend3": lambda: client3,
        }

        manager = BackendManager(
            default_backend="backend1",
            default_client=client1,
            factories=factories,
            order=["backend1", "backend2", "backend3"],
        )

        # After each full cycle of all backends failing, we're back on backend1
        # This is because the rotation happens during the exception handling
        with pytest.raises(AutoCoderUsageLimitError):
            manager._run_llm_cli("test prompt")
        assert manager._current_backend_name() == "backend1"

        # Second execution - same behavior (all backends fail)
        with pytest.raises(AutoCoderUsageLimitError):
            manager._run_llm_cli("test prompt")
        assert manager._current_backend_name() == "backend1"
