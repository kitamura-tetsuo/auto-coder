import os
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.backend_manager import BackendManager, LLMBackendManager
from src.auto_coder.backend_provider_manager import (
    BackendProviderManager,
    BackendProviderMetadata,
    ProviderMetadata,
)
from src.auto_coder.backend_session_manager import BackendSessionState
from src.auto_coder.exceptions import AutoCoderTimeoutError, AutoCoderUsageLimitError
from src.auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration


class DummyClient:
    def __init__(self, name: str, model_name: str, behavior: str, calls: list[str], session_id: Optional[str] = None):
        self.name = name
        self.model_name = model_name
        self.behavior = behavior
        self.calls = calls
        self.session_id = session_id
        self._extra_args: list[str] = []

    def _run_llm_cli(self, prompt: str) -> str:
        self.calls.append(self.name)
        if self.behavior == "limit":
            raise AutoCoderUsageLimitError("rate limit")
        if self.behavior == "timeout":
            raise AutoCoderTimeoutError("timeout")
        if self.behavior == "error":
            raise RuntimeError("other error")
        return f"{self.name}:{prompt}"

    def switch_to_default_model(self) -> None:
        pass

    def get_last_session_id(self) -> Optional[str]:
        return self.session_id

    def set_extra_args(self, args: list[str]) -> None:
        self._extra_args = args


@pytest.fixture
def mock_llm_config():
    with patch("src.auto_coder.backend_manager.get_llm_config") as mock_get_config:
        config = LLMBackendConfiguration()
        mock_get_config.return_value = config
        yield config


def test_backend_retries_on_usage_limit(mock_llm_config):
    mock_llm_config.backends["a"] = BackendConfig(name="a", usage_limit_retry_count=3, usage_limit_retry_wait_seconds=0.1)

    calls = []
    client_a = DummyClient("a", "m1", "limit", calls)
    client_b = DummyClient("b", "m2", "ok", calls)

    mgr = BackendManager(
        default_backend="a",
        default_client=client_a,
        factories={"a": lambda: client_a, "b": lambda: client_b},
        order=["a", "b"],
    )

    with patch("time.sleep") as mock_sleep:
        result = mgr._run_llm_cli("test")
        assert result == "b:test"
        assert calls == ["a", "a", "a", "b"]
        assert mock_sleep.call_count == 3


def test_wait_time_is_observed(mock_llm_config):
    mock_llm_config.backends["a"] = BackendConfig(name="a", usage_limit_retry_count=3, usage_limit_retry_wait_seconds=5)

    calls = []
    client_a = DummyClient("a", "m1", "limit", calls)
    client_b = DummyClient("b", "m2", "ok", calls)

    mgr = BackendManager(
        default_backend="a",
        default_client=client_a,
        factories={"a": lambda: client_a, "b": lambda: client_b},
        order=["a", "b"],
    )

    with patch("time.sleep") as mock_sleep:
        mgr._run_llm_cli("test")
        mock_sleep.assert_any_call(5)
        assert mock_sleep.call_count == 3


def test_rotation_after_exhausting_retries(mock_llm_config):
    mock_llm_config.backends["a"] = BackendConfig(name="a", usage_limit_retry_count=2)

    calls = []
    client_a = DummyClient("a", "m1", "limit", calls)
    client_b = DummyClient("b", "m2", "ok", calls)

    mgr = BackendManager(
        default_backend="a",
        default_client=client_a,
        factories={"a": lambda: client_a, "b": lambda: client_b},
        order=["a", "b"],
    )

    with patch("time.sleep"):
        result = mgr._run_llm_cli("test")
        assert result == "b:test"
        assert calls == ["a", "a", "b"]


def test_immediate_rotation_with_zero_retries(mock_llm_config):
    mock_llm_config.backends["a"] = BackendConfig(name="a", usage_limit_retry_count=0)

    calls = []
    client_a = DummyClient("a", "m1", "limit", calls)
    client_b = DummyClient("b", "m2", "ok", calls)

    mgr = BackendManager(
        default_backend="a",
        default_client=client_a,
        factories={"a": lambda: client_a, "b": lambda: client_b},
        order=["a", "b"],
    )

    with patch("time.sleep") as mock_sleep:
        result = mgr._run_llm_cli("test")
        assert result == "b:test"
        assert calls == ["a", "b"]
        mock_sleep.assert_not_called()


def test_different_retry_configurations(mock_llm_config):
    mock_llm_config.backends["a"] = BackendConfig(name="a", usage_limit_retry_count=3, usage_limit_retry_wait_seconds=1)
    mock_llm_config.backends["b"] = BackendConfig(name="b", usage_limit_retry_count=1, usage_limit_retry_wait_seconds=2)

    calls = []
    client_a = DummyClient("a", "m1", "limit", calls)
    client_b = DummyClient("b", "m2", "limit", calls)
    client_c = DummyClient("c", "m3", "ok", calls)

    mgr = BackendManager(
        default_backend="a",
        default_client=client_a,
        factories={"a": lambda: client_a, "b": lambda: client_b, "c": lambda: client_c},
        order=["a", "b", "c"],
    )

    with patch("time.sleep") as mock_sleep:
        result = mgr._run_llm_cli("test")
        assert result == "c:test"
        assert calls == ["a", "a", "a", "b", "c"]
        assert mock_sleep.call_count == 4
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)


def test_edge_case_negative_retries(mock_llm_config):
    mock_llm_config.backends["a"] = BackendConfig(name="a", usage_limit_retry_count=-1)

    calls = []
    client_a = DummyClient("a", "m1", "limit", calls)
    client_b = DummyClient("b", "m2", "ok", calls)

    mgr = BackendManager(
        default_backend="a",
        default_client=client_a,
        factories={"a": lambda: client_a, "b": lambda: client_b},
        order=["a", "b"],
    )

    with patch("time.sleep") as mock_sleep:
        result = mgr._run_llm_cli("test")
        assert result == "b:test"
        assert calls == ["a", "b"]
        mock_sleep.assert_not_called()


def test_edge_case_large_retries(mock_llm_config):
    mock_llm_config.backends["a"] = BackendConfig(name="a", usage_limit_retry_count=100)

    calls = []

    # Succeeds on the 5th attempt
    class SucceedAfterN:
        def __init__(self):
            self.count = 0

        def __call__(self, prompt):
            self.count += 1
            calls.append("a")
            if self.count < 5:
                raise AutoCoderUsageLimitError("limit")
            return "a:success"

    client_a = MagicMock()
    client_a.name = "a"
    client_a.model_name = "m1"
    client_a._run_llm_cli = SucceedAfterN()

    mgr = BackendManager(
        default_backend="a",
        default_client=client_a,
        factories={"a": lambda: client_a},
        order=["a"],
    )

    with patch("time.sleep"):
        result = mgr._run_llm_cli("test")
        assert result == "a:success"
        assert calls == ["a"] * 5


def test_backend_manager_no_duplicate_logging(mock_llm_config):
    """Verify that BackendManager delegates logging to clients and does not duplicate output."""
    mock_llm_config.backends["a"] = BackendConfig(name="a", usage_limit_retry_count=2, usage_limit_retry_wait_seconds=3)

    calls = []
    client_a = DummyClient("a", "m1", "limit", calls)
    client_b = DummyClient("b", "m2", "ok", calls)

    mgr = BackendManager(
        default_backend="a",
        default_client=client_a,
        factories={"a": lambda: client_a, "b": lambda: client_b},
        order=["a", "b"],
    )

    # Ensure backend rotation works correctly without duplicate logging
    # With retry_count=2: 1 initial + 1 retry = 2 attempts on "a", then 1 on "b"
    with patch("time.sleep") as mock_sleep:
        result = mgr._run_llm_cli("test")
        assert result == "b:test"
        assert calls == ["a", "a", "b"]
        assert mock_sleep.call_count == 2


def test_no_retry_on_other_exceptions(mock_llm_config):
    mock_llm_config.backends["a"] = BackendConfig(name="a", usage_limit_retry_count=3)

    calls = []
    client_a = DummyClient("a", "m1", "error", calls)

    mgr = BackendManager(
        default_backend="a",
        default_client=client_a,
        factories={"a": lambda: client_a},
        order=["a"],
    )

    with pytest.raises(RuntimeError):
        mgr._run_llm_cli("test")

    assert calls == ["a"]


def test_timeout_fallback_to_next_backend(mock_llm_config):
    """Test that BackendManager switches to next backend when timeout occurs."""
    mock_llm_config.backends["a"] = BackendConfig(name="a", usage_limit_retry_count=0)
    mock_llm_config.backends["b"] = BackendConfig(name="b", usage_limit_retry_count=0)

    calls = []
    client_a = DummyClient("a", "m1", "timeout", calls)
    client_b = DummyClient("b", "m2", "ok", calls)

    mgr = BackendManager(
        default_backend="a",
        default_client=client_a,
        factories={"a": lambda: client_a, "b": lambda: client_b},
        order=["a", "b"],
    )

    result = mgr._run_llm_cli("test")
    assert result == "b:test"
    assert calls == ["a", "b"]


def test_timeout_fallback_multiple_backends(mock_llm_config):
    """Test timeout fallback with multiple backend rotations."""
    mock_llm_config.backends["a"] = BackendConfig(name="a", usage_limit_retry_count=0)
    mock_llm_config.backends["b"] = BackendConfig(name="b", usage_limit_retry_count=0)
    mock_llm_config.backends["c"] = BackendConfig(name="c", usage_limit_retry_count=0)

    calls = []
    client_a = DummyClient("a", "m1", "timeout", calls)
    client_b = DummyClient("b", "m2", "timeout", calls)
    client_c = DummyClient("c", "m3", "ok", calls)

    mgr = BackendManager(
        default_backend="a",
        default_client=client_a,
        factories={"a": lambda: client_a, "b": lambda: client_b, "c": lambda: client_c},
        order=["a", "b", "c"],
    )

    result = mgr._run_llm_cli("test")
    assert result == "c:test"
    assert calls == ["a", "b", "c"]


def test_timeout_fallback_exhausts_all_backends(mock_llm_config):
    """Test that all backends timeout raises the last timeout error."""
    mock_llm_config.backends["a"] = BackendConfig(name="a", usage_limit_retry_count=0)
    mock_llm_config.backends["b"] = BackendConfig(name="b", usage_limit_retry_count=0)

    calls = []
    client_a = DummyClient("a", "m1", "timeout", calls)
    client_b = DummyClient("b", "m2", "timeout", calls)

    mgr = BackendManager(
        default_backend="a",
        default_client=client_a,
        factories={"a": lambda: client_a, "b": lambda: client_b},
        order=["a", "b"],
    )

    with pytest.raises(AutoCoderTimeoutError):
        mgr._run_llm_cli("test")

    # Both backends should have been tried
    assert calls == ["a", "b"]


def test_backend_switch_after_execution_with_flag_enabled(mock_llm_config):
    """Test that backend switches after successful execution when always_switch_after_execution is True."""
    mock_llm_config.backends["a"] = BackendConfig(name="a", always_switch_after_execution=True)
    mock_llm_config.backends["b"] = BackendConfig(name="b", always_switch_after_execution=False)

    calls = []
    client_a = DummyClient("a", "m1", "ok", calls)
    client_b = DummyClient("b", "m2", "ok", calls)

    mgr = BackendManager(
        default_backend="a",
        default_client=client_a,
        factories={"a": lambda: client_a, "b": lambda: client_b},
        order=["a", "b"],
    )

    # Execute a successful prompt
    result = mgr._run_llm_cli("test")
    assert result == "a:test"
    assert calls == ["a"]

    # Verify backend switched to the next one (b)
    assert mgr._current_backend_name() == "b"


def test_backend_no_switch_after_execution_with_flag_disabled(mock_llm_config):
    """Test that backend does NOT switch after successful execution when always_switch_after_execution is False."""
    mock_llm_config.backends["a"] = BackendConfig(name="a", always_switch_after_execution=False)
    mock_llm_config.backends["b"] = BackendConfig(name="b", always_switch_after_execution=False)

    calls = []
    client_a = DummyClient("a", "m1", "ok", calls)
    client_b = DummyClient("b", "m2", "ok", calls)

    mgr = BackendManager(
        default_backend="a",
        default_client=client_a,
        factories={"a": lambda: client_a, "b": lambda: client_b},
        order=["a", "b"],
    )

    # Execute a successful prompt
    result = mgr._run_llm_cli("test")
    assert result == "a:test"
    assert calls == ["a"]

    # Verify backend did NOT switch (stayed on a)
    assert mgr._current_backend_name() == "a"


# ==================== Backend Auto-Reset Integration Tests ====================


class TestBackendAutoReset:
    """Integration tests for BackendManager auto-reset logic with BackendStateManager."""

    def test_auto_reset_fresh_start_default_backend(self, mock_llm_config):
        """Scenario 1: Fresh start (no state file) -> Should use default backend."""
        from unittest.mock import MagicMock

        # Mock BackendStateManager to return empty state (no state file)
        mock_state_manager = MagicMock()
        mock_state_manager.load_state.return_value = {}  # No state

        client_a = DummyClient("a", "m1", "ok", [])
        client_b = DummyClient("b", "m2", "ok", [])

        mgr = BackendManager(
            default_backend="a",
            default_client=client_a,
            factories={"a": lambda: client_a, "b": lambda: client_b},
            order=["a", "b"],
        )

        # Patch the instance's _state_manager attribute
        with patch.object(mgr, "_state_manager", mock_state_manager):
            # Should start on default backend 'a'
            assert mgr._current_backend_name() == "a"

            # check_and_reset_backend_if_needed should do nothing (no state)
            mgr.check_and_reset_backend_if_needed()
            assert mgr._current_backend_name() == "a"

    def test_auto_reset_switched_backend_under_2_hours(self, mock_llm_config):
        """Scenario 2: Start on default, saved state shows non-default < 2 hours ago -> Should sync to saved backend."""
        import time
        from unittest.mock import MagicMock

        client_a = DummyClient("a", "m1", "ok", [])
        client_b = DummyClient("b", "m2", "ok", [])

        mgr = BackendManager(
            default_backend="a",
            default_client=client_a,
            factories={"a": lambda: client_a, "b": lambda: client_b},
            order=["a", "b"],
        )

        # Mock the state manager instance directly
        current_time = time.time()
        one_hour_ago = current_time - 3600  # 1 hour ago (< 2 hours)
        mock_state_manager = MagicMock()
        mock_state_manager.load_state.return_value = {
            "current_backend": "b",
            "last_switch_timestamp": one_hour_ago,
        }

        # Patch the instance's _state_manager attribute
        with patch.object(mgr, "_state_manager", mock_state_manager):
            # Initially on 'a' (default)
            assert mgr._current_backend_name() == "a"

            # Call check_and_reset_backend_if_needed - should sync to saved backend 'b'
            mgr.check_and_reset_backend_if_needed()

            # Should switch to saved backend to allow session resume continuity
            assert mgr._current_backend_name() == "b"

    def test_auto_reset_switched_backend_over_2_hours(self, mock_llm_config):
        """Scenario 3: Started on non-default, saved state > 2 hours ago -> Should reset to default."""
        import time
        from unittest.mock import MagicMock

        client_a = DummyClient("a", "m1", "ok", [])
        client_b = DummyClient("b", "m2", "ok", [])

        mgr = BackendManager(
            default_backend="a",
            default_client=client_a,
            factories={"a": lambda: client_a, "b": lambda: client_b},
            order=["a", "b"],
        )

        # First switch to backend 'b' manually
        mgr.switch_to_next_backend()
        assert mgr._current_backend_name() == "b"

        # Mock the state manager instance directly
        current_time = time.time()
        three_hours_ago = current_time - 10800  # 3 hours ago (> 2 hours)
        mock_state_manager = MagicMock()
        mock_state_manager.load_state.return_value = {
            "current_backend": "b",
            "last_switch_timestamp": three_hours_ago,
        }

        # Patch the instance's _state_manager attribute
        with patch.object(mgr, "_state_manager", mock_state_manager):
            # Currently on 'b' (non-default), saved state also says 'b' but from > 2 hours ago
            assert mgr._current_backend_name() == "b"

            # Call check_and_reset_backend_if_needed
            mgr.check_and_reset_backend_if_needed()

            # Should reset to 'a' (default) because > 2 hours have passed
            assert mgr._current_backend_name() == "a"

            # Verify state was saved when switching to default
            mock_state_manager.save_state.assert_called_once()
            call_args = mock_state_manager.save_state.call_args
            assert call_args[0][0] == "a"  # First arg: current_backend

    def test_auto_reset_state_saved_on_switch(self, mock_llm_config):
        """Scenario 4: Verify state is saved when switch_to_next_backend is called."""
        import time
        from unittest.mock import MagicMock

        # Mock BackendStateManager
        with patch("src.auto_coder.backend_manager.BackendStateManager") as MockStateManager:
            mock_state_manager = MagicMock()
            mock_state_manager.load_state.return_value = {}  # No initial state
            MockStateManager.return_value = mock_state_manager

            client_a = DummyClient("a", "m1", "ok", [])
            client_b = DummyClient("b", "m2", "ok", [])

            mgr = BackendManager(
                default_backend="a",
                default_client=client_a,
                factories={"a": lambda: client_a, "b": lambda: client_b},
                order=["a", "b"],
            )

            # Initially on 'a'
            assert mgr._current_backend_name() == "a"

            # Manually switch to next backend
            mgr.switch_to_next_backend()

            # Should be on 'b' now
            assert mgr._current_backend_name() == "b"

            # Verify state was saved when switching
            mock_state_manager.save_state.assert_called_once()
            call_args = mock_state_manager.save_state.call_args
            assert call_args[0][0] == "b"  # First arg: current_backend should be 'b'
            assert isinstance(call_args[0][1], float)  # Second arg: timestamp should be a float

    def test_auto_reset_state_saved_on_default_switch(self, mock_llm_config):
        """Verify state is saved when switch_to_default_backend is called."""
        import time
        from unittest.mock import MagicMock

        # Mock BackendStateManager
        with patch("src.auto_coder.backend_manager.BackendStateManager") as MockStateManager:
            mock_state_manager = MagicMock()
            mock_state_manager.load_state.return_value = {}  # No initial state
            MockStateManager.return_value = mock_state_manager

            client_a = DummyClient("a", "m1", "ok", [])
            client_b = DummyClient("b", "m2", "ok", [])

            mgr = BackendManager(
                default_backend="a",
                default_client=client_a,
                factories={"a": lambda: client_a, "b": lambda: client_b},
                order=["a", "b"],
            )

            # Switch to 'b' first
            mgr.switch_to_next_backend()
            assert mgr._current_backend_name() == "b"

            # Reset mock to clear previous call
            mock_state_manager.reset_mock()

            # Switch back to default
            mgr.switch_to_default_backend()

            # Should be on 'a' now
            assert mgr._current_backend_name() == "a"

            # Verify state was saved when switching to default
            mock_state_manager.save_state.assert_called_once()
            call_args = mock_state_manager.save_state.call_args
            assert call_args[0][0] == "a"  # First arg: current_backend should be 'a'
            assert isinstance(call_args[0][1], float)  # Second arg: timestamp should be a float

    def test_auto_reset_already_on_default(self, mock_llm_config):
        """When already on default backend, should not reset even if > 2 hours."""
        import time
        from unittest.mock import MagicMock

        # Mock BackendStateManager with state showing 'a' (default) 3 hours ago
        with patch("src.auto_coder.backend_manager.BackendStateManager") as MockStateManager:
            mock_state_manager = MagicMock()
            current_time = time.time()
            three_hours_ago = current_time - 10800  # 3 hours ago (> 2 hours)
            mock_state_manager.load_state.return_value = {
                "current_backend": "a",  # Already on default
                "last_switch_timestamp": three_hours_ago,
            }
            MockStateManager.return_value = mock_state_manager

            client_a = DummyClient("a", "m1", "ok", [])
            client_b = DummyClient("b", "m2", "ok", [])

            mgr = BackendManager(
                default_backend="a",
                default_client=client_a,
                factories={"a": lambda: client_a, "b": lambda: client_b},
                order=["a", "b"],
            )

            # Call check_and_reset_backend_if_needed
            mgr.check_and_reset_backend_if_needed()

            # Should stay on 'a' (no change needed)
            assert mgr._current_backend_name() == "a"

    def test_auto_reset_invalid_state_data(self, mock_llm_config):
        """When state has invalid/missing data, should handle gracefully."""
        from unittest.mock import MagicMock

        # Mock BackendStateManager with invalid state
        with patch("src.auto_coder.backend_manager.BackendStateManager") as MockStateManager:
            mock_state_manager = MagicMock()
            mock_state_manager.load_state.return_value = {
                "current_backend": "b"
                # Missing "last_switch_timestamp"
            }
            MockStateManager.return_value = mock_state_manager

            client_a = DummyClient("a", "m1", "ok", [])
            client_b = DummyClient("b", "m2", "ok", [])

            mgr = BackendManager(
                default_backend="a",
                default_client=client_a,
                factories={"a": lambda: client_a, "b": lambda: client_b},
                order=["a", "b"],
            )

            # Should handle invalid state gracefully and stay on default
            mgr.check_and_reset_backend_if_needed()
            assert mgr._current_backend_name() == "a"

    def test_auto_reset_unknown_saved_backend(self, mock_llm_config):
        """When saved backend is not in current backend list, should ignore and stay on default."""
        import time
        from unittest.mock import MagicMock

        # Mock BackendStateManager with unknown backend
        with patch("src.auto_coder.backend_manager.BackendStateManager") as MockStateManager:
            mock_state_manager = MagicMock()
            current_time = time.time()
            one_hour_ago = current_time - 3600
            mock_state_manager.load_state.return_value = {
                "current_backend": "unknown_backend",  # Not in ["a", "b"]
                "last_switch_timestamp": one_hour_ago,
            }
            MockStateManager.return_value = mock_state_manager

            client_a = DummyClient("a", "m1", "ok", [])
            client_b = DummyClient("b", "m2", "ok", [])

            mgr = BackendManager(
                default_backend="a",
                default_client=client_a,
                factories={"a": lambda: client_a, "b": lambda: client_b},
                order=["a", "b"],
            )

            # Should stay on default 'a' since saved backend is unknown
            mgr.check_and_reset_backend_if_needed()
            assert mgr._current_backend_name() == "a"


class TestResumeLogic:
    """Tests for session resume logic in BackendManager."""

    def test_inject_resume_options_when_conditions_met(self, mock_llm_config):
        """Resume options should be injected when backend, session ID, and config match."""
        # Configure backend with resume options
        mock_llm_config.backends["a"] = BackendConfig(name="a", options_for_resume=["--resume", "[sessionId]"])

        calls = []
        client_a = DummyClient("a", "m1", "ok", calls, session_id="test-session-123")

        mgr = BackendManager(
            default_backend="a",
            default_client=client_a,
            factories={"a": lambda: client_a},
            order=["a"],
        )

        # First call to establish session ID and backend
        result1 = mgr._run_llm_cli("first prompt")
        assert result1 == "a:first prompt"
        assert mgr._last_session_id == "test-session-123"
        assert mgr._last_backend == "a"

        # Second call should inject resume options
        result2 = mgr._run_llm_cli("second prompt")
        assert result2 == "a:second prompt"
        # Verify that extra args were set with session ID replaced
        assert client_a._extra_args == ["--resume", "test-session-123"]

    def test_no_resume_when_backend_changes(self, mock_llm_config):
        """Resume options should NOT be injected when backend changes."""
        # Configure both backends with resume options
        mock_llm_config.backends["a"] = BackendConfig(name="a", options_for_resume=["--resume", "[sessionId]"])
        mock_llm_config.backends["b"] = BackendConfig(name="b", options_for_resume=["--resume", "[sessionId]"])

        calls = []
        client_a = DummyClient("a", "m1", "ok", calls, session_id="session-a")
        client_b = DummyClient("b", "m2", "ok", calls, session_id="session-b")

        mgr = BackendManager(
            default_backend="a",
            default_client=client_a,
            factories={"a": lambda: client_a, "b": lambda: client_b},
            order=["a", "b"],
        )

        # First call on backend a
        result1 = mgr._run_llm_cli("first prompt")
        assert result1 == "a:first prompt"
        assert mgr._last_backend == "a"
        assert mgr._last_session_id == "session-a"

        # Switch to backend b
        mgr.switch_to_next_backend()
        assert mgr._current_backend_name() == "b"

        # Second call on backend b should NOT use resume (backend changed)
        result2 = mgr._run_llm_cli("second prompt")
        assert result2 == "b:second prompt"
        # Extra args should be empty (no resume)
        assert client_b._extra_args == []

    def test_no_resume_when_no_session_id(self, mock_llm_config):
        """Resume options should NOT be injected when no session ID is available."""
        # Configure backend with resume options
        mock_llm_config.backends["a"] = BackendConfig(name="a", options_for_resume=["--resume", "[sessionId]"])

        calls = []
        # Client without session ID
        client_a = DummyClient("a", "m1", "ok", calls, session_id=None)

        mgr = BackendManager(
            default_backend="a",
            default_client=client_a,
            factories={"a": lambda: client_a},
            order=["a"],
        )

        # First call - no session ID will be set
        result1 = mgr._run_llm_cli("first prompt")
        assert result1 == "a:first prompt"
        assert mgr._last_session_id is None

        # Second call should NOT inject resume options (no session ID)
        result2 = mgr._run_llm_cli("second prompt")
        assert result2 == "a:second prompt"
        assert client_a._extra_args == []

    def test_no_resume_when_no_config(self, mock_llm_config):
        """Resume options should NOT be injected when backend has no resume config."""
        # Configure backend WITHOUT resume options
        mock_llm_config.backends["a"] = BackendConfig(name="a", options_for_resume=[])  # Empty resume options

        calls = []
        client_a = DummyClient("a", "m1", "ok", calls, session_id="test-session")

        mgr = BackendManager(
            default_backend="a",
            default_client=client_a,
            factories={"a": lambda: client_a},
            order=["a"],
        )

        # First call
        result1 = mgr._run_llm_cli("first prompt")
        assert result1 == "a:first prompt"
        assert mgr._last_session_id == "test-session"

        # Second call should NOT inject resume options (no config)
        result2 = mgr._run_llm_cli("second prompt")
        assert result2 == "a:second prompt"
        assert client_a._extra_args == []

    def test_multiple_placeholders_replaced(self, mock_llm_config):
        """Multiple [sessionId] placeholders should all be replaced."""
        # Configure backend with multiple placeholders
        mock_llm_config.backends["a"] = BackendConfig(name="a", options_for_resume=["--resume", "[sessionId]", "--session-id=[sessionId]"])

        calls = []
        client_a = DummyClient("a", "m1", "ok", calls, session_id="multi-session-456")

        mgr = BackendManager(
            default_backend="a",
            default_client=client_a,
            factories={"a": lambda: client_a},
            order=["a"],
        )

        # First call
        mgr._run_llm_cli("first prompt")

        # Second call should replace all placeholders
        mgr._run_llm_cli("second prompt")
        assert client_a._extra_args == ["--resume", "multi-session-456", "--session-id=multi-session-456"]

    def test_session_id_reset_on_backend_switch(self, mock_llm_config):
        """Session ID should be reset when switching backends."""
        calls = []
        client_a = DummyClient("a", "m1", "ok", calls, session_id="session-a")
        client_b = DummyClient("b", "m2", "ok", calls, session_id="session-b")

        mgr = BackendManager(
            default_backend="a",
            default_client=client_a,
            factories={"a": lambda: client_a, "b": lambda: client_b},
            order=["a", "b"],
        )

        # First call on backend a
        mgr._run_llm_cli("first prompt")
        assert mgr._last_session_id == "session-a"

        # Switch to backend b - session ID should be reset
        mgr.switch_to_next_backend()
        assert mgr._last_session_id is None

        # Call on backend b establishes new session
        mgr._run_llm_cli("second prompt")
        assert mgr._last_session_id == "session-b"

    def test_resume_uses_persisted_session_state(self, mock_llm_config):
        """Resume options should be applied from persisted session state on startup."""
        mock_llm_config.backends["a"] = BackendConfig(name="a", options_for_resume=["--resume", "[sessionId]"])

        calls = []
        client_a = DummyClient("a", "m1", "ok", calls, session_id="new-session-id")

        saved_state = BackendSessionState(last_backend="a", last_session_id="persisted-session", last_used_timestamp=123.0)

        with patch("src.auto_coder.backend_manager.BackendSessionManager") as MockSessionManager:
            mock_session_manager = MagicMock()
            mock_session_manager.load_state.return_value = saved_state
            mock_session_manager.save_state.return_value = True
            MockSessionManager.return_value = mock_session_manager

            mgr = BackendManager(
                default_backend="a",
                default_client=client_a,
                factories={"a": lambda: client_a},
                order=["a"],
            )

            mgr._run_llm_cli("resume prompt")

            # Resume options should use persisted session id
            assert client_a._extra_args == ["--resume", "persisted-session"]

            # The new session id should be persisted after execution
            mock_session_manager.save_state.assert_called()
            persisted_arg = mock_session_manager.save_state.call_args[0][0]
            assert isinstance(persisted_arg, BackendSessionState)
            assert persisted_arg.last_backend == "a"
            assert persisted_arg.last_session_id == "new-session-id"
