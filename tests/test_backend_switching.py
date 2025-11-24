"""Tests for backend switching after single execution.

This module contains comprehensive tests to verify that backends rotate
after one call when the always_switch_after_execution flag is enabled.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.backend_manager import BackendManager
from src.auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration


class DummyClient:
    """Mock client for testing backend switching."""

    def __init__(self, name: str, model_name: str, calls: list[str]):
        self.name = name
        self.model_name = model_name
        self.calls = calls

    def _run_llm_cli(self, prompt: str) -> str:
        """Record call and return formatted response."""
        self.calls.append(self.name)
        return f"{self.name}:{prompt}"

    def switch_to_default_model(self) -> None:
        """No-op for testing."""
        pass


@pytest.fixture
def mock_llm_config():
    """Mock LLM configuration for testing."""
    with patch("src.auto_coder.backend_manager.get_llm_config") as mock_get_config:
        config = LLMBackendConfiguration()
        mock_get_config.return_value = config
        yield config


def test_backend_rotates_after_single_execution_with_flag_enabled(mock_llm_config):
    """Test that backend rotates after one execution when flag is enabled."""
    # Configure backends with switch flag enabled
    mock_llm_config.backends["a"] = BackendConfig(name="a", always_switch_after_execution=True)
    mock_llm_config.backends["b"] = BackendConfig(name="b", always_switch_after_execution=True)
    mock_llm_config.backends["c"] = BackendConfig(name="c", always_switch_after_execution=True)

    calls = []
    client_a = DummyClient("a", "m1", calls)
    client_b = DummyClient("b", "m2", calls)
    client_c = DummyClient("c", "m3", calls)

    mgr = BackendManager(
        default_backend="a",
        default_client=client_a,
        factories={"a": lambda: client_a, "b": lambda: client_b, "c": lambda: client_c},
        order=["a", "b", "c"],
    )

    # Execute on backend 'a'
    result = mgr._run_llm_cli("test1")
    assert result == "a:test1"
    assert calls == ["a"]
    # Verify backend switched to 'b'
    assert mgr._current_backend_name() == "b"

    # Execute on backend 'b'
    result = mgr._run_llm_cli("test2")
    assert result == "b:test2"
    assert calls == ["a", "b"]
    # Verify backend switched to 'c'
    assert mgr._current_backend_name() == "c"

    # Execute on backend 'c'
    result = mgr._run_llm_cli("test3")
    assert result == "c:test3"
    assert calls == ["a", "b", "c"]
    # Verify backend rotated back to 'a' (circular rotation)
    assert mgr._current_backend_name() == "a"


def test_backend_rotates_multiple_times_consecutively(mock_llm_config):
    """Test backend rotation over multiple consecutive executions."""
    mock_llm_config.backends["backend1"] = BackendConfig(name="backend1", always_switch_after_execution=True)
    mock_llm_config.backends["backend2"] = BackendConfig(name="backend2", always_switch_after_execution=True)

    calls = []
    client1 = DummyClient("backend1", "m1", calls)
    client2 = DummyClient("backend2", "m2", calls)

    mgr = BackendManager(
        default_backend="backend1",
        default_client=client1,
        factories={"backend1": lambda: client1, "backend2": lambda: client2},
        order=["backend1", "backend2"],
    )

    # Execute 10 times to verify consistent rotation
    for i in range(10):
        expected_backend = "backend1" if i % 2 == 0 else "backend2"
        expected_next = "backend2" if i % 2 == 0 else "backend1"

        result = mgr._run_llm_cli(f"test{i}")
        assert result == f"{expected_backend}:test{i}"
        assert mgr._current_backend_name() == expected_next

    # Verify total call count
    assert len(calls) == 10


def test_circular_rotation_wraps_to_first_backend(mock_llm_config):
    """Test that rotation wraps around to the first backend."""
    mock_llm_config.backends["x"] = BackendConfig(name="x", always_switch_after_execution=True)
    mock_llm_config.backends["y"] = BackendConfig(name="y", always_switch_after_execution=True)

    calls = []
    client_x = DummyClient("x", "m1", calls)
    client_y = DummyClient("y", "m2", calls)

    mgr = BackendManager(
        default_backend="x",
        default_client=client_x,
        factories={"x": lambda: client_x, "y": lambda: client_y},
        order=["x", "y"],
    )

    # Execute on x, should switch to y
    mgr._run_llm_cli("test1")
    assert mgr._current_backend_name() == "y"

    # Execute on y, should switch back to x (circular)
    mgr._run_llm_cli("test2")
    assert mgr._current_backend_name() == "x"

    # Execute on x again, should switch to y
    mgr._run_llm_cli("test3")
    assert mgr._current_backend_name() == "y"


def test_mixed_flags_partial_rotation(mock_llm_config):
    """Test rotation when only some backends have the flag enabled."""
    # Only backend 'a' has the flag enabled
    mock_llm_config.backends["a"] = BackendConfig(name="a", always_switch_after_execution=True)
    mock_llm_config.backends["b"] = BackendConfig(name="b", always_switch_after_execution=False)
    mock_llm_config.backends["c"] = BackendConfig(name="c", always_switch_after_execution=True)

    calls = []
    client_a = DummyClient("a", "m1", calls)
    client_b = DummyClient("b", "m2", calls)
    client_c = DummyClient("c", "m3", calls)

    mgr = BackendManager(
        default_backend="a",
        default_client=client_a,
        factories={"a": lambda: client_a, "b": lambda: client_b, "c": lambda: client_c},
        order=["a", "b", "c"],
    )

    # Execute on 'a' (flag enabled) -> should switch to 'b'
    mgr._run_llm_cli("test1")
    assert mgr._current_backend_name() == "b"

    # Execute on 'b' (flag disabled) -> should stay on 'b'
    mgr._run_llm_cli("test2")
    assert mgr._current_backend_name() == "b"

    # Execute on 'b' again (flag disabled) -> should stay on 'b'
    mgr._run_llm_cli("test3")
    assert mgr._current_backend_name() == "b"

    # Manually switch to 'c' to test it
    mgr.switch_to_next_backend()
    assert mgr._current_backend_name() == "c"

    # Execute on 'c' (flag enabled) -> should switch to 'a' (circular)
    mgr._run_llm_cli("test4")
    assert mgr._current_backend_name() == "a"


def test_rotation_does_not_occur_when_flag_disabled(mock_llm_config):
    """Test that no rotation occurs when flag is disabled on all backends."""
    mock_llm_config.backends["alpha"] = BackendConfig(name="alpha", always_switch_after_execution=False)
    mock_llm_config.backends["beta"] = BackendConfig(name="beta", always_switch_after_execution=False)

    calls = []
    client_alpha = DummyClient("alpha", "m1", calls)
    client_beta = DummyClient("beta", "m2", calls)

    mgr = BackendManager(
        default_backend="alpha",
        default_client=client_alpha,
        factories={"alpha": lambda: client_alpha, "beta": lambda: client_beta},
        order=["alpha", "beta"],
    )

    # Execute multiple times, should always stay on 'alpha'
    for i in range(5):
        result = mgr._run_llm_cli(f"test{i}")
        assert result == f"alpha:test{i}"
        assert mgr._current_backend_name() == "alpha"

    assert len(calls) == 5


def test_rotation_with_usage_limit_retry(mock_llm_config):
    """Test that rotation works correctly after usage limit retry."""
    from src.auto_coder.exceptions import AutoCoderUsageLimitError

    mock_llm_config.backends["backend1"] = BackendConfig(
        name="backend1",
        usage_limit_retry_count=2,
        always_switch_after_execution=True,
    )
    mock_llm_config.backends["backend2"] = BackendConfig(name="backend2", always_switch_after_execution=True)

    calls = []
    client1 = DummyClient("backend1", "m1", calls)
    client2 = DummyClient("backend2", "m2", calls)

    # Make client1 raise usage limit errors
    def run_with_limit(prompt: str) -> str:
        if len(calls) < 2:  # First two calls raise limit
            calls.append("backend1")
            raise AutoCoderUsageLimitError("rate limit")
        return client1._run_llm_cli(prompt)

    client1._run_llm_cli = run_with_limit

    mgr = BackendManager(
        default_backend="backend1",
        default_client=client1,
        factories={"backend1": lambda: client1, "backend2": lambda: client2},
        order=["backend1", "backend2"],
    )

    with patch("time.sleep"):
        # This should:
        # 1. Try backend1 (raise limit)
        # 2. Retry backend1 (raise limit)
        # 3. Switch to backend2 (success)
        # 4. Switch to backend1 (due to always_switch_after_execution)
        result = mgr._run_llm_cli("test")
        assert result == "backend2:test"

        # After successful execution on backend2, should switch to next (backend1)
        assert mgr._current_backend_name() == "backend1"


def test_rotation_state_persists_across_calls(mock_llm_config):
    """Test that rotation state persists correctly across multiple calls."""
    mock_llm_config.backends["first"] = BackendConfig(name="first", always_switch_after_execution=True)
    mock_llm_config.backends["second"] = BackendConfig(name="second", always_switch_after_execution=True)
    mock_llm_config.backends["third"] = BackendConfig(name="third", always_switch_after_execution=True)

    calls = []
    client_first = DummyClient("first", "m1", calls)
    client_second = DummyClient("second", "m2", calls)
    client_third = DummyClient("third", "m3", calls)

    mgr = BackendManager(
        default_backend="first",
        default_client=client_first,
        factories={
            "first": lambda: client_first,
            "second": lambda: client_second,
            "third": lambda: client_third,
        },
        order=["first", "second", "third"],
    )

    # Execute several times and verify state
    mgr._run_llm_cli("test1")  # on first, switch to second
    assert mgr._current_backend_name() == "second"

    mgr._run_llm_cli("test2")  # on second, switch to third
    assert mgr._current_backend_name() == "third"

    mgr._run_llm_cli("test3")  # on third, switch to first (circular)
    assert mgr._current_backend_name() == "first"

    mgr._run_llm_cli("test4")  # on first, switch to second
    assert mgr._current_backend_name() == "second"

    # Verify all backends were used
    assert set(calls) == {"first", "second", "third"}


def test_rotation_with_single_backend(mock_llm_config):
    """Test rotation behavior with only one backend configured."""
    mock_llm_config.backends["solo"] = BackendConfig(name="solo", always_switch_after_execution=True)

    calls = []
    client_solo = DummyClient("solo", "m1", calls)

    mgr = BackendManager(
        default_backend="solo",
        default_client=client_solo,
        factories={"solo": lambda: client_solo},
        order=["solo"],
    )

    # Execute multiple times on the single backend
    for i in range(3):
        result = mgr._run_llm_cli(f"test{i}")
        assert result == f"solo:test{i}"
        # With only one backend, should always stay on it (circular rotation)
        assert mgr._current_backend_name() == "solo"

    assert len(calls) == 3


def test_rotation_after_manual_switch(mock_llm_config):
    """Test that automatic rotation works after manual backend switching."""
    mock_llm_config.backends["primary"] = BackendConfig(name="primary", always_switch_after_execution=True)
    mock_llm_config.backends["secondary"] = BackendConfig(name="secondary", always_switch_after_execution=True)

    calls = []
    client_primary = DummyClient("primary", "m1", calls)
    client_secondary = DummyClient("secondary", "m2", calls)

    mgr = BackendManager(
        default_backend="primary",
        default_client=client_primary,
        factories={"primary": lambda: client_primary, "secondary": lambda: client_secondary},
        order=["primary", "secondary"],
    )

    # Manually switch to secondary
    mgr.switch_to_next_backend()
    assert mgr._current_backend_name() == "secondary"

    # Execute on secondary, should switch to primary (circular)
    mgr._run_llm_cli("test1")
    assert mgr._current_backend_name() == "primary"

    # Execute on primary, should switch to secondary
    mgr._run_llm_cli("test2")
    assert mgr._current_backend_name() == "secondary"


def test_rotation_preserves_backend_client_state(mock_llm_config):
    """Test that rotation doesn't interfere with client state."""
    mock_llm_config.backends["client1"] = BackendConfig(name="client1", always_switch_after_execution=True)
    mock_llm_config.backends["client2"] = BackendConfig(name="client2", always_switch_after_execution=True)

    # Create clients with state
    calls = []

    class StatefulClient:
        def __init__(self, name: str, model_name: str):
            self.name = name
            self.model_name = model_name
            self.call_count = 0
            self.calls = calls

        def _run_llm_cli(self, prompt: str) -> str:
            self.call_count += 1
            self.calls.append(self.name)
            return f"{self.name}:{prompt} (call #{self.call_count})"

        def switch_to_default_model(self) -> None:
            pass

    client1 = StatefulClient("client1", "m1")
    client2 = StatefulClient("client2", "m2")

    mgr = BackendManager(
        default_backend="client1",
        default_client=client1,
        factories={"client1": lambda: client1, "client2": lambda: client2},
        order=["client1", "client2"],
    )

    # Execute and verify client state is preserved
    result1 = mgr._run_llm_cli("test1")
    assert "call #1" in result1
    assert client1.call_count == 1
    assert client2.call_count == 0
    assert mgr._current_backend_name() == "client2"

    # Execute on client2, verify its state
    result2 = mgr._run_llm_cli("test2")
    assert "call #1" in result2
    assert client1.call_count == 1
    assert client2.call_count == 1
    assert mgr._current_backend_name() == "client1"


def test_get_last_backend_reflects_rotation(mock_llm_config):
    """Test that get_last_backend_and_model reflects the rotated backend."""
    mock_llm_config.backends["backend_a"] = BackendConfig(name="backend_a", always_switch_after_execution=True)
    mock_llm_config.backends["backend_b"] = BackendConfig(name="backend_b", always_switch_after_execution=True)

    calls = []
    client_a = DummyClient("backend_a", "model-a", calls)
    client_b = DummyClient("backend_b", "model-b", calls)

    mgr = BackendManager(
        default_backend="backend_a",
        default_client=client_a,
        factories={"backend_a": lambda: client_a, "backend_b": lambda: client_b},
        order=["backend_a", "backend_b"],
    )

    # Execute on backend_a
    mgr._run_llm_cli("test1")

    # Get last backend info
    backend, model = mgr.get_last_backend_and_model()
    assert backend == "backend_a"
    assert model == "model-a"
    # After execution, should be on backend_b
    assert mgr._current_backend_name() == "backend_b"

    # Execute on backend_b
    mgr._run_llm_cli("test2")

    # Get last backend info again
    backend, model = mgr.get_last_backend_and_model()
    assert backend == "backend_b"
    assert model == "model-b"
    # After execution, should be on backend_a (circular)
    assert mgr._current_backend_name() == "backend_a"


def test_rotation_respects_toml_config(tmp_path):
    """Ensure always_switch_after_execution loaded from TOML triggers rotation."""
    config_path = tmp_path / "llm_config.toml"
    file_config = LLMBackendConfiguration(
        backend_order=["first", "second"],
        default_backend="first",
        backends={
            "first": BackendConfig(name="first", always_switch_after_execution=True),
            "second": BackendConfig(name="second", always_switch_after_execution=False),
        },
    )
    file_config.save_to_file(config_path)
    loaded_config = LLMBackendConfiguration.load_from_file(str(config_path))

    calls: list[str] = []
    client_first = DummyClient("first", "m1", calls)
    client_second = DummyClient("second", "m2", calls)

    with patch("src.auto_coder.backend_manager.get_llm_config", return_value=loaded_config):
        mgr = BackendManager(
            default_backend="first",
            default_client=client_first,
            factories={"first": lambda: client_first, "second": lambda: client_second},
            order=["first", "second"],
        )

        # First call uses first backend and rotates to second because flag is true
        result_first = mgr._run_llm_cli("hello")
        assert result_first == "first:hello"
        assert calls == ["first"]
        assert mgr._current_backend_name() == "second"

        # Second backend keeps running because its flag is false
        result_second = mgr._run_llm_cli("world")
        assert result_second == "second:world"
        assert calls == ["first", "second"]
        assert mgr._current_backend_name() == "second"
