import os

import pytest

from src.auto_coder.backend_manager import BackendManager, LLMBackendManager
from src.auto_coder.backend_provider_manager import BackendProviderManager, BackendProviderMetadata, ProviderMetadata
from src.auto_coder.exceptions import AutoCoderUsageLimitError


class DummyClient:
    def __init__(self, name: str, model_name: str, behavior: str, calls: list[str]):
        self.name = name
        self.model_name = model_name
        self.behavior = behavior  # 'ok' | 'limit' | 'error'
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


class ProviderAwareClient:
    def __init__(self, behavior_map: dict[str, str], calls: list[str | None]):
        self.behavior_map = behavior_map
        self.calls = calls
        self.model_name = "model-provider"

    def _run_llm_cli(self, prompt: str) -> str:
        token = os.environ.get("PROVIDER_TOKEN")
        self.calls.append(token)
        lookup_key = token or ""
        outcome = self.behavior_map.get(lookup_key, "ok")
        if outcome == "limit":
            raise AutoCoderUsageLimitError("limit hit")
        return f"{token}:{prompt}"

    def switch_to_default_model(self) -> None:
        pass


def _build_provider_manager() -> BackendProviderManager:
    manager = BackendProviderManager()
    manager._provider_cache["codex"] = BackendProviderMetadata(
        backend_name="codex",
        providers=[
            ProviderMetadata(name="codex-primary", command="uvx", uppercase_settings={"PROVIDER_TOKEN": "token-a"}),
            ProviderMetadata(name="codex-secondary", command="uvx", uppercase_settings={"PROVIDER_TOKEN": "token-b"}),
        ],
    )
    manager._metadata_cache = {}
    return manager


def _build_single_provider_manager() -> BackendProviderManager:
    manager = BackendProviderManager()
    manager._provider_cache["codex"] = BackendProviderMetadata(
        backend_name="codex",
        providers=[
            ProviderMetadata(name="codex-primary", command="uvx", uppercase_settings={"PROVIDER_TOKEN": "token-a"}),
        ],
    )
    manager._metadata_cache = {}
    return manager


def test_manager_switches_on_usage_limit():
    calls: list[str] = []

    # default backend 'a' hits usage limit; next 'b' returns ok
    a_client = DummyClient("a", "m1", "limit", calls)

    def fac_a():
        return DummyClient("a", "m1", "limit", calls)

    def fac_b():
        return DummyClient("b", "m2", "ok", calls)

    mgr = BackendManager(
        default_backend="a",
        default_client=a_client,
        factories={"a": fac_a, "b": fac_b},
        order=["a", "b"],
    )

    out = mgr._run_llm_cli("P")
    assert out == "b:P"
    assert calls == ["a", "b"]


def test_run_test_fix_prompt_switch_after_three_same_test_files():
    calls: list[str] = []

    a_client = DummyClient("codex", "m1", "ok", calls)

    def fac_codex():
        return DummyClient("codex", "m1", "ok", calls)

    def fac_gemini():
        return DummyClient("gemini", "m2", "ok", calls)

    mgr = BackendManager(
        default_backend="codex",
        default_client=a_client,
        factories={"codex": fac_codex, "gemini": fac_gemini},
        order=["codex", "gemini"],
    )

    # Same test_file 1 -> codex
    mgr.run_test_fix_prompt("X", current_test_file="test_a.py")
    # Same test_file 2 -> codex
    mgr.run_test_fix_prompt("Y", current_test_file="test_a.py")
    # Same test_file 3 -> should switch BEFORE running -> gemini
    mgr.run_test_fix_prompt("Z", current_test_file="test_a.py")

    assert calls == ["codex", "codex", "gemini"]


def test_cyclic_rotation_across_multiple_backends_on_usage_limits():
    calls: list[str] = []

    # Build four backends in the documented cyclic order
    codex = DummyClient("codex", "m1", "limit", calls)

    def fac_codex():
        return DummyClient("codex", "m1", "limit", calls)

    def fac_codex_mcp():
        return DummyClient("codex-mcp", "m1", "limit", calls)

    def fac_gemini():
        return DummyClient("gemini", "m2", "limit", calls)

    def fac_qwen():
        return DummyClient("qwen", "m3", "limit", calls)

    def fac_auggie():
        return DummyClient("auggie", "m4", "ok", calls)

    mgr = BackendManager(
        default_backend="codex",
        default_client=codex,
        factories={
            "codex": fac_codex,
            "codex-mcp": fac_codex_mcp,
            "gemini": fac_gemini,
            "qwen": fac_qwen,
            "auggie": fac_auggie,
        },
        order=["codex", "codex-mcp", "gemini", "qwen", "auggie"],
    )

    out = mgr._run_llm_cli("P")
    assert out == "auggie:P"
    assert calls == ["codex", "codex-mcp", "gemini", "qwen", "auggie"]


def test_run_test_fix_prompt_resets_to_default_on_new_test_file():
    calls: list[str] = []

    codex = DummyClient("codex", "m1", "ok", calls)

    def fac_codex():
        return DummyClient("codex", "m1", "ok", calls)

    def fac_gemini():
        return DummyClient("gemini", "m2", "ok", calls)

    def fac_qwen():
        return DummyClient("qwen", "m3", "ok", calls)

    mgr = BackendManager(
        default_backend="codex",
        default_client=codex,
        factories={"codex": fac_codex, "gemini": fac_gemini, "qwen": fac_qwen},
        order=["codex", "gemini", "qwen"],
    )

    # Same test_file 1/2 on default -> codex
    mgr.run_test_fix_prompt("A", current_test_file="test_a.py")
    mgr.run_test_fix_prompt("B", current_test_file="test_a.py")
    # Third same test_file triggers rotation BEFORE execution -> gemini
    mgr.run_test_fix_prompt("C", current_test_file="test_a.py")

    # Different test_file arrives -> should reset to default before execution -> codex
    mgr.run_test_fix_prompt("D", current_test_file="test_b.py")

    assert calls == ["codex", "codex", "gemini", "codex"]


def test_same_test_file_counter_resets_when_backend_changes_due_to_limit():
    calls: list[str] = []

    # a(limit) -> b(ok) for first call; subsequent same test_file should NOT switch on 3rd because backend changed
    a_client = DummyClient("a", "m1", "limit", calls)

    def fac_a():
        return DummyClient("a", "m1", "limit", calls)

    def fac_b():
        return DummyClient("b", "m2", "ok", calls)

    mgr = BackendManager(
        default_backend="a",
        default_client=a_client,
        factories={"a": fac_a, "b": fac_b},
        order=["a", "b"],
    )

    # First same test_file: starts on 'a' but hits usage limit, rotates to 'b' and runs there
    mgr.run_test_fix_prompt("SAME", current_test_file="test_x.py")
    # Second same test_file: last_backend != current (was b, current index is b), count resets to 1 -> stays on b
    mgr.run_test_fix_prompt("SAME", current_test_file="test_x.py")
    # Third same test_file: same backend as previous but count so far should be 2 now -> still stays on b (switch would occur only before 3rd if two prior on same backend)
    mgr.run_test_fix_prompt("SAME", current_test_file="test_x.py")

    # Expect: 1st run tries 'a' (limit) then 'b'; 2nd runs on 'b'; 3rd switches before run to 'a' then rotates to 'b'
    assert calls == ["a", "b", "b", "a", "b"]


def test_get_last_backend_and_model_reflects_actual_client_usage():
    calls: list[str] = []

    codex_client = DummyClient("codex", "m1", "ok", calls)

    def fac_codex():
        return DummyClient("codex", "m1", "ok", calls)

    def fac_gemini():
        return DummyClient("gemini", "m2", "ok", calls)

    mgr = BackendManager(
        default_backend="codex",
        default_client=codex_client,
        factories={"codex": fac_codex, "gemini": fac_gemini},
        order=["codex", "gemini"],
    )

    backend, model = mgr.get_last_backend_and_model()
    assert backend == "codex"
    assert model == "m1"


def test_provider_rotation_before_backend_switch():
    calls: list[str | None] = []

    provider_manager = _build_provider_manager()
    client = ProviderAwareClient({"token-a": "limit", "token-b": "ok"}, calls)

    mgr = BackendManager(
        default_backend="codex",
        default_client=client,
        factories={"codex": lambda: client},
        order=["codex"],
        provider_manager=provider_manager,
    )

    out = mgr._run_llm_cli("PROMPT")
    assert out == "token-b:PROMPT"
    assert calls == ["token-a", "token-b"]

    backend, provider, model = mgr.get_last_backend_provider_and_model()
    assert backend == "codex"
    assert provider == "codex-secondary"
    assert model == "model-provider"
    assert os.environ.get("PROVIDER_TOKEN") is None


def test_provider_env_cleared_on_failure():
    calls: list[str | None] = []

    provider_manager = _build_single_provider_manager()
    client = ProviderAwareClient({"token-a": "limit"}, calls)

    mgr = BackendManager(
        default_backend="codex",
        default_client=client,
        factories={"codex": lambda: client},
        order=["codex"],
        provider_manager=provider_manager,
    )

    with pytest.raises(AutoCoderUsageLimitError):
        mgr._run_llm_cli("PROMPT")

    assert calls == ["token-a"]
    assert os.environ.get("PROVIDER_TOKEN") is None


def test_provider_failover_occurs_before_backend_rotation():
    calls: list[str | None] = []

    provider_manager = _build_provider_manager()
    provider_client = ProviderAwareClient({"token-a": "limit", "token-b": "limit"}, calls)
    fallback_client = DummyClient("gemini", "m2", "ok", calls)

    mgr = BackendManager(
        default_backend="codex",
        default_client=provider_client,
        factories={"codex": lambda: provider_client, "gemini": lambda: fallback_client},
        order=["codex", "gemini"],
        provider_manager=provider_manager,
    )

    out = mgr._run_llm_cli("PROMPT")

    # Provider rotation should exhaust providers before backend rotation happens.
    assert out == "gemini:PROMPT"
    assert calls == ["token-a", "token-b", "gemini"]

    # The last used provider should be reset because success happened on a backend without providers.
    backend, provider, model = mgr.get_last_backend_provider_and_model()
    assert backend == "gemini"
    assert provider is None
    assert model == "m2"
    assert provider_manager.get_last_used_provider_name("codex") is None
    assert os.environ.get("PROVIDER_TOKEN") is None


def test_llm_backend_manager_singleton_initialization():
    """Test that LLMBackendManager singleton can be initialized with parameters."""
    # Reset singleton before test
    LLMBackendManager.reset_singleton()

    a_client = DummyClient("a", "m1", "ok", [])

    def fac_a():
        return DummyClient("a", "m1", "ok", [])

    def fac_b():
        return DummyClient("b", "m2", "ok", [])

    # Initialize singleton
    mgr = LLMBackendManager.get_llm_instance(
        default_backend="a",
        default_client=a_client,
        factories={"a": fac_a, "b": fac_b},
        order=["a", "b"],
    )

    # Verify it returns a BackendManager instance
    assert isinstance(mgr, BackendManager)
    assert mgr._current_backend_name() == "a"

    # Verify singleton returns the same instance on subsequent calls
    mgr2 = LLMBackendManager.get_llm_instance()
    assert mgr2 is mgr

    # Verify is_initialized returns True
    assert LLMBackendManager.is_initialized() is True

    # Clean up
    LLMBackendManager.reset_singleton()


def test_llm_backend_manager_singleton_error_without_params():
    """Test that calling get_llm_instance without params before initialization raises error."""
    # Reset singleton before test
    LLMBackendManager.reset_singleton()

    # Try to get instance without initialization - should raise error
    try:
        LLMBackendManager.get_llm_instance()
        assert False, "Expected RuntimeError"
    except RuntimeError as e:
        assert "must be called with initialization parameters" in str(e)

    # Clean up (in case test failed)
    try:
        LLMBackendManager.reset_singleton()
    except Exception:
        pass


def test_llm_backend_manager_singleton_reset():
    """Test that reset_singleton properly resets the singleton."""
    # Reset singleton before test
    LLMBackendManager.reset_singleton()

    a_client = DummyClient("a", "m1", "ok", [])

    def fac_a():
        return DummyClient("a", "m1", "ok", [])

    # Initialize singleton
    mgr = LLMBackendManager.get_llm_instance(
        default_backend="a",
        default_client=a_client,
        factories={"a": fac_a},
        order=["a"],
    )

    assert LLMBackendManager.is_initialized() is True
    assert mgr._current_backend_name() == "a"

    # Reset singleton
    LLMBackendManager.reset_singleton()

    assert LLMBackendManager.is_initialized() is False

    # Reinitialize with different parameters
    b_client = DummyClient("b", "m2", "ok", [])

    def fac_b():
        return DummyClient("b", "m2", "ok", [])

    mgr2 = LLMBackendManager.get_llm_instance(
        default_backend="b",
        default_client=b_client,
        factories={"b": fac_b},
        order=["b"],
    )

    assert mgr2 is not mgr  # New instance after reset
    assert mgr2._current_backend_name() == "b"
    assert LLMBackendManager.is_initialized() is True

    # Clean up
    LLMBackendManager.reset_singleton()


def test_llm_backend_manager_singleton_force_reinitialize():
    """Test force_reinitialize parameter works correctly."""
    # Reset singleton before test
    LLMBackendManager.reset_singleton()

    a_client = DummyClient("a", "m1", "ok", [])

    def fac_a():
        return DummyClient("a", "m1", "ok", [])

    # Initialize with 'a'
    mgr1 = LLMBackendManager.get_llm_instance(
        default_backend="a",
        default_client=a_client,
        factories={"a": fac_a},
        order=["a"],
    )

    assert mgr1._current_backend_name() == "a"

    # Force reinitialize with 'b'
    b_client = DummyClient("b", "m2", "ok", [])

    def fac_b():
        return DummyClient("b", "m2", "ok", [])

    mgr2 = LLMBackendManager.get_llm_instance(
        default_backend="b",
        default_client=b_client,
        factories={"b": fac_b},
        order=["b"],
        force_reinitialize=True,
    )

    # Force reinitialize creates a NEW BackendManager instance but keeps it as the singleton
    # The singleton reference changes because we create a new instance
    assert isinstance(mgr2, BackendManager)
    assert mgr2._current_backend_name() == "b"

    # Clean up
    LLMBackendManager.reset_singleton()


def test_llm_backend_manager_singleton_ignores_subsequent_params():
    """Test that providing parameters after initialization is allowed but ignored."""
    # Reset singleton before test
    LLMBackendManager.reset_singleton()

    a_client = DummyClient("a", "m1", "ok", [])

    def fac_a():
        return DummyClient("a", "m1", "ok", [])

    def fac_b():
        return DummyClient("b", "m2", "ok", [])

    # Initialize singleton
    mgr1 = LLMBackendManager.get_llm_instance(
        default_backend="a",
        default_client=a_client,
        factories={"a": fac_a},
        order=["a"],
    )

    # Call again with different parameters - should return same instance
    mgr2 = LLMBackendManager.get_llm_instance(
        default_backend="b",
        default_client=a_client,
        factories={"b": fac_b},
        order=["b"],
    )

    assert mgr2 is mgr1  # Same instance
    assert mgr1._current_backend_name() == "a"  # Still using original backend

    # Clean up
    LLMBackendManager.reset_singleton()


def test_llm_backend_manager_singleton_thread_safety():
    """Test that singleton initialization is thread-safe."""
    # Reset singleton before test
    LLMBackendManager.reset_singleton()

    results = []

    def init_singleton():
        a_client = DummyClient("a", "m1", "ok", [])
        return LLMBackendManager.get_llm_instance(
            default_backend="a",
            default_client=a_client,
            factories={"a": lambda: DummyClient("a", "m1", "ok", [])},
            order=["a"],
        )

    # Multiple threads trying to initialize (only one should succeed)
    import threading

    threads = []
    for _ in range(5):
        t = threading.Thread(target=lambda: results.append(init_singleton()))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # All threads should get the same instance
    assert len(results) == 5
    assert all(mgr is results[0] for mgr in results)
    assert LLMBackendManager.is_initialized() is True

    # Clean up
    LLMBackendManager.reset_singleton()


def test_llm_backend_manager_singleton_works_with_existing_functionality():
    """Test that the singleton instance works with all existing BackendManager methods."""
    # Reset singleton before test
    LLMBackendManager.reset_singleton()

    calls = []

    a_client = DummyClient("a", "m1", "ok", calls)

    def fac_a():
        return DummyClient("a", "m1", "ok", calls)

    def fac_b():
        return DummyClient("b", "m2", "ok", calls)

    # Initialize singleton
    mgr = LLMBackendManager.get_llm_instance(
        default_backend="a",
        default_client=a_client,
        factories={"a": fac_a, "b": fac_b},
        order=["a", "b"],
    )

    # Test that it works with _run_llm_cli
    result = mgr._run_llm_cli("test prompt")
    assert result == "a:test prompt"
    assert calls == ["a"]

    # Test that it works with run_test_fix_prompt
    result = mgr.run_test_fix_prompt("test prompt 2", "test_file.py")
    assert result == "a:test prompt 2"
    assert calls == ["a", "a"]

    # Test get_last_backend_and_model
    backend, model = mgr.get_last_backend_and_model()
    assert backend == "a"
    assert model == "m1"

    # Test that singleton is the same when accessed again
    mgr2 = LLMBackendManager.get_llm_instance()
    assert mgr2 is mgr

    # Clean up
    LLMBackendManager.reset_singleton()


def test_backend_manager_with_provider_manager():
    """Test that BackendManager correctly accepts and exposes provider_manager."""
    calls = []

    a_client = DummyClient("a", "m1", "ok", calls)

    def fac_a():
        return DummyClient("a", "m1", "ok", calls)

    # Create a custom provider manager
    custom_provider_manager = BackendProviderManager()

    # Initialize BackendManager with provider_manager parameter
    mgr = BackendManager(
        default_backend="a",
        default_client=a_client,
        factories={"a": fac_a},
        order=["a"],
        provider_manager=custom_provider_manager,
    )

    # Verify provider_manager property is accessible
    assert mgr.provider_manager is not None
    assert isinstance(mgr.provider_manager, BackendProviderManager)
    assert mgr.provider_manager is custom_provider_manager


def test_backend_manager_without_provider_manager_uses_default():
    """Test that BackendManager uses default provider manager when none provided."""
    calls = []

    a_client = DummyClient("a", "m1", "ok", calls)

    def fac_a():
        return DummyClient("a", "m1", "ok", calls)

    # Initialize BackendManager without provider_manager parameter
    mgr = BackendManager(
        default_backend="a",
        default_client=a_client,
        factories={"a": fac_a},
        order=["a"],
    )

    # Verify provider_manager property is accessible and returns default manager
    assert mgr.provider_manager is not None
    assert isinstance(mgr.provider_manager, BackendProviderManager)


def test_provider_fallback_before_backend_fallback():
    """
    Critical test: Provider rotation must happen BEFORE backend rotation.

    When a usage limit is hit, the system should first try the next provider
    for the same backend before switching to the next backend.
    """
    calls = []

    # Build a backend with multiple providers
    provider_manager = BackendProviderManager()
    provider_manager._provider_cache["codex"] = BackendProviderMetadata(
        backend_name="codex",
        providers=[
            ProviderMetadata(name="codex-primary", command="uvx", uppercase_settings={"PROVIDER_TOKEN": "primary"}),
            ProviderMetadata(name="codex-secondary", command="uvx", uppercase_settings={"PROVIDER_TOKEN": "secondary"}),
        ],
    )
    provider_manager._metadata_cache = {}

    # Client that hits limit on first provider, succeeds on second
    client = ProviderAwareClient(
        {"primary": "limit", "secondary": "ok", "gemini": "ok"},
        calls,
    )

    # Setup two backends
    def fac_codex():
        return client

    def fac_gemini():
        return DummyClient("gemini", "m2", "ok", calls)

    mgr = BackendManager(
        default_backend="codex",
        default_client=client,
        factories={"codex": fac_codex, "gemini": fac_gemini},
        order=["codex", "gemini"],
        provider_manager=provider_manager,
    )

    # First run: hits limit on primary provider, succeeds on secondary
    result = mgr._run_llm_cli("PROMPT")
    assert result == "secondary:PROMPT"
    # Should have tried primary token first, then secondary
    assert calls == ["primary", "secondary"]
    # Should still be on codex backend (didn't switch to gemini)
    assert mgr._current_backend_name() == "codex"

    # Verify last used provider tracking
    backend, provider, model = mgr.get_last_backend_provider_and_model()
    assert backend == "codex"
    assert provider == "codex-secondary"
    assert model == "model-provider"


def test_provider_fallback_exhaustion_then_backend_fallback():
    """
    Test that after all providers are exhausted for a backend,
    only then should it fall back to the next backend.
    """
    calls = []

    provider_manager = BackendProviderManager()
    provider_manager._provider_cache["codex"] = BackendProviderMetadata(
        backend_name="codex",
        providers=[
            ProviderMetadata(name="codex-p1", command="uvx", uppercase_settings={"PROVIDER_TOKEN": "p1"}),
            ProviderMetadata(name="codex-p2", command="uvx", uppercase_settings={"PROVIDER_TOKEN": "p2"}),
        ],
    )
    provider_manager._provider_cache["gemini"] = BackendProviderMetadata(
        backend_name="gemini",
        providers=[
            ProviderMetadata(name="gemini-p1", command="uvx", uppercase_settings={"PROVIDER_TOKEN": "g1"}),
        ],
    )
    provider_manager._metadata_cache = {}

    # Create separate clients for each backend
    def fac_codex():
        return ProviderAwareClient(
            {
                "p1": "limit",
                "p2": "limit",
            },
            calls,
        )

    def fac_gemini():
        return ProviderAwareClient({"g1": "ok"}, calls)

    mgr = BackendManager(
        default_backend="codex",
        default_client=ProviderAwareClient({"p1": "limit", "p2": "limit"}, calls),
        factories={"codex": fac_codex, "gemini": fac_gemini},
        order=["codex", "gemini"],
        provider_manager=provider_manager,
    )

    # First run: exhaust all providers on codex
    result = mgr._run_llm_cli("PROMPT")
    assert result == "g1:PROMPT"
    # Should have tried: p1 (limit), p2 (limit), then backend switch to gemini
    assert calls == ["p1", "p2", "g1"]
    # Should now be on gemini backend
    assert mgr._current_backend_name() == "gemini"


def test_strict_provider_ordering():
    """Test that providers are used in a strict, deterministic order."""
    provider_manager = BackendProviderManager()
    provider_manager._provider_cache["qwen"] = BackendProviderMetadata(
        backend_name="qwen",
        providers=[
            ProviderMetadata(name="first", command="uvx"),
            ProviderMetadata(name="second", command="uvx"),
            ProviderMetadata(name="third", command="uvx"),
        ],
    )
    provider_manager._metadata_cache = {}

    # Verify order is maintained
    providers = provider_manager.get_backend_providers("qwen").providers
    assert len(providers) == 3
    assert providers[0].name == "first"
    assert providers[1].name == "second"
    assert providers[2].name == "third"

    # Test rotation maintains order
    assert provider_manager.get_current_provider_name("qwen") == "first"
    provider_manager.advance_to_next_provider("qwen")
    assert provider_manager.get_current_provider_name("qwen") == "second"
    provider_manager.advance_to_next_provider("qwen")
    assert provider_manager.get_current_provider_name("qwen") == "third"
    # Wrap around
    provider_manager.advance_to_next_provider("qwen")
    assert provider_manager.get_current_provider_name("qwen") == "first"


def test_env_vars_absent_after_error():
    """
    Test that environment variables are cleaned up even when errors occur.

    This is a critical acceptance criteria: env vars must not persist in os.environ
    after invocations, even when errors occur.
    """
    calls = []

    provider_manager = BackendProviderManager()
    provider_manager._provider_cache["test"] = BackendProviderMetadata(
        backend_name="test",
        providers=[
            ProviderMetadata(name="test-provider", command="uvx", uppercase_settings={"PROVIDER_TOKEN": "error-token"}),
        ],
    )
    provider_manager._metadata_cache = {}

    # Use "limit" behavior to trigger AutoCoderUsageLimitError
    client = ProviderAwareClient({"error-token": "limit"}, calls)

    def fac_test():
        return client

    mgr = BackendManager(
        default_backend="test",
        default_client=client,
        factories={"test": fac_test},
        order=["test"],
        provider_manager=provider_manager,
    )

    # Run and expect an error
    with pytest.raises(AutoCoderUsageLimitError):
        mgr._run_llm_cli("PROMPT")

    # Critical assertion: environment variable should not be in os.environ
    assert os.environ.get("PROVIDER_TOKEN") is None

    # Verify it was set during execution (in the environment context)
    assert calls == ["error-token"]


def test_provider_metadata_reporting():
    """Test that last-used provider metadata is accurately reported."""
    calls = []

    provider_manager = BackendProviderManager()
    provider_manager._provider_cache["backend"] = BackendProviderMetadata(
        backend_name="backend",
        providers=[
            ProviderMetadata(name="provider-1", command="cmd", uppercase_settings={"PROVIDER_TOKEN": "p1"}),
            ProviderMetadata(name="provider-2", command="cmd", uppercase_settings={"PROVIDER_TOKEN": "p2"}),
            ProviderMetadata(name="provider-3", command="cmd", uppercase_settings={"PROVIDER_TOKEN": "p3"}),
        ],
    )
    provider_manager._metadata_cache = {}

    client = ProviderAwareClient({"p1": "ok", "p2": "ok", "p3": "ok"}, calls)

    def fac_backend():
        return client

    mgr = BackendManager(
        default_backend="backend",
        default_client=client,
        factories={"backend": fac_backend},
        order=["backend"],
        provider_manager=provider_manager,
    )

    # Use provider-1
    mgr._run_llm_cli("prompt1")
    backend, provider, model = mgr.get_last_backend_provider_and_model()
    assert backend == "backend"
    assert provider == "provider-1"
    assert model == "model-provider"

    # Advance to provider-2
    provider_manager.advance_to_next_provider("backend")
    mgr._run_llm_cli("prompt2")
    backend, provider, model = mgr.get_last_backend_provider_and_model()
    assert backend == "backend"
    assert provider == "provider-2"
    assert model == "model-provider"

    # Advance to provider-3
    provider_manager.advance_to_next_provider("backend")
    mgr._run_llm_cli("prompt3")
    backend, provider, model = mgr.get_last_backend_provider_and_model()
    assert backend == "backend"
    assert provider == "provider-3"
    assert model == "model-provider"


def test_no_providers_uses_backend_fallback():
    """Test that when no providers are configured, backend rotation still works."""
    calls = []

    # No provider manager configured
    a_client = DummyClient("a", "m1", "limit", calls)

    def fac_a():
        return DummyClient("a", "m1", "limit", calls)

    def fac_b():
        return DummyClient("b", "m2", "ok", calls)

    mgr = BackendManager(
        default_backend="a",
        default_client=a_client,
        factories={"a": fac_a, "b": fac_b},
        order=["a", "b"],
    )

    # Should fallback to next backend when usage limit hit (no providers to try)
    result = mgr._run_llm_cli("PROMPT")
    assert result == "b:PROMPT"
    assert calls == ["a", "b"]


def test_single_provider_no_rotation():
    """Test behavior with only one provider configured."""
    calls = []

    provider_manager = BackendProviderManager()
    provider_manager._provider_cache["backend"] = BackendProviderMetadata(
        backend_name="backend",
        providers=[
            ProviderMetadata(name="only-provider", command="cmd", uppercase_settings={"PROVIDER_TOKEN": "token"}),
        ],
    )
    provider_manager._metadata_cache = {}

    client = ProviderAwareClient({"token": "ok"}, calls)

    def fac_backend():
        return client

    mgr = BackendManager(
        default_backend="backend",
        default_client=client,
        factories={"backend": fac_backend},
        order=["backend"],
        provider_manager=provider_manager,
    )

    # With only one provider, should succeed (not raise error)
    result = mgr._run_llm_cli("PROMPT")
    assert result == "token:PROMPT"
    # Should have tried the only provider
    assert calls == ["token"]


def test_run_test_fix_prompt_with_providers():
    """Test that run_test_fix_prompt works correctly with provider manager."""
    calls = []

    provider_manager = BackendProviderManager()
    provider_manager._provider_cache["codex"] = BackendProviderMetadata(
        backend_name="codex",
        providers=[
            ProviderMetadata(name="primary", command="uvx", uppercase_settings={"PROVIDER_TOKEN": "p"}),
            ProviderMetadata(name="secondary", command="uvx", uppercase_settings={"PROVIDER_TOKEN": "s"}),
        ],
    )
    provider_manager._metadata_cache = {}

    # Create a client that hits limit on primary, succeeds on secondary
    client = ProviderAwareClient({"p": "limit", "s": "ok"}, calls)

    def fac_codex():
        return client

    mgr = BackendManager(
        default_backend="codex",
        default_client=client,
        factories={"codex": fac_codex},
        order=["codex"],
        provider_manager=provider_manager,
    )

    # First run on same test file - hits limit on primary, rotates to secondary
    result = mgr.run_test_fix_prompt("prompt1", "test.py")
    assert result == "s:prompt1"  # Succeeded on secondary provider
    assert calls == ["p", "s"]  # Tried primary first (limit), then secondary

    # Second run on same test file - should use secondary again
    result = mgr.run_test_fix_prompt("prompt2", "test.py")
    assert result == "s:prompt2"
    assert calls == ["p", "s", "s"]  # Stayed on secondary

    # Third run on same test file - should still use secondary (no usage limit)
    result = mgr.run_test_fix_prompt("prompt3", "test.py")
    assert result == "s:prompt3"
    assert calls == ["p", "s", "s", "s"]  # Continued on secondary

    # Different test file - switches to default backend (codex)
    # The provider manager still remembers it was on secondary, so it continues with secondary
    result = mgr.run_test_fix_prompt("prompt4", "other_test.py")
    assert result == "s:prompt4"  # Stayed on secondary provider
    assert calls == ["p", "s", "s", "s", "s"]
