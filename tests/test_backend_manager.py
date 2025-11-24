import os
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.backend_manager import BackendManager, LLMBackendManager
from src.auto_coder.backend_provider_manager import BackendProviderManager, BackendProviderMetadata, ProviderMetadata
from src.auto_coder.exceptions import AutoCoderUsageLimitError
from src.auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration


class DummyClient:
    def __init__(self, name: str, model_name: str, behavior: str, calls: list[str]):
        self.name = name
        self.model_name = model_name
        self.behavior = behavior
        self.calls = calls

    def _run_llm_cli(self, prompt: str) -> str:
        self.calls.append(self.name)
        if self.behavior == "limit":
            raise AutoCoderUsageLimitError("rate limit")
        if self.behavior == "error":
            raise RuntimeError("other error")
        return f"{self.name}:{prompt}"

    def switch_to_default_model(self) -> None:
        pass


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
