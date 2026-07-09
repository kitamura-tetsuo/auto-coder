"""Tests for SafeGhApiProxy coroutine unwrapping in gh_cache."""

import pytest

from src.auto_coder.util.gh_cache import SafeGhApiProxy


class _FakeApi:
    """Stand-in for a GhApi object whose methods may return coroutines."""

    def __init__(self, func):
        self._func = func

    def call(self, *args, **kwargs):
        return self._func(*args, **kwargs)


class TestSafeGhApiProxy:
    def test_sync_result_passes_through(self):
        proxy = SafeGhApiProxy(_FakeApi(lambda: {"ok": True}))
        assert proxy.call() == {"ok": True}

    def test_fresh_coroutine_is_unwrapped(self):
        async def coro_func():
            return [1, 2, 3]

        proxy = SafeGhApiProxy(_FakeApi(lambda: coro_func()))
        assert proxy.call() == [1, 2, 3]

    def test_exception_inside_coroutine_propagates_unchanged(self):
        async def failing():
            raise ValueError("real underlying error")

        proxy = SafeGhApiProxy(_FakeApi(lambda: failing()))
        with pytest.raises(ValueError, match="real underlying error"):
            proxy.call()

    def test_reused_coroutine_raises_clear_error(self):
        async def coro_func():
            return "value"

        # The same coroutine object is returned for every call (e.g. a Mock
        # with a fixed coroutine return_value).
        shared = coro_func()
        proxy = SafeGhApiProxy(_FakeApi(lambda: shared))

        assert proxy.call() == "value"
        with pytest.raises(RuntimeError, match="already-awaited coroutine"):
            proxy.call()

    def test_suspending_coroutine_raises_clear_error(self):
        class _Suspend:
            def __await__(self):
                yield

        async def suspending():
            await _Suspend()

        proxy = SafeGhApiProxy(_FakeApi(lambda: suspending()))
        with pytest.raises(RuntimeError, match="requires a running event loop"):
            proxy.call()

    def test_attribute_chain_is_proxied(self):
        class Inner:
            def method(self):
                return "inner-result"

        class Outer:
            inner = Inner()

        proxy = SafeGhApiProxy(Outer())
        assert proxy.inner.method() == "inner-result"
