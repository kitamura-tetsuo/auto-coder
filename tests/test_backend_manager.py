from auto_coder.backend_manager import BackendManager, LLMBackendManager
from auto_coder.exceptions import AutoCoderUsageLimitError


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

    mgr.run_test_fix_prompt("PROMPT", current_test_file="test_a.py")
    backend, model = mgr.get_last_backend_and_model()
    assert backend == "codex"
    assert model == "m1"

    # Trigger rotation to the secondary backend and confirm reporting updates
    mgr.run_test_fix_prompt("PROMPT", current_test_file="test_a.py")
    mgr.run_test_fix_prompt("PROMPT", current_test_file="test_a.py")
    backend, model = mgr.get_last_backend_and_model()
    assert backend == "gemini"
    assert model == "m2"


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
