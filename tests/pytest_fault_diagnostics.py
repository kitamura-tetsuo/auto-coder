"""Keep nonfatal scheduled pytest dumps on a Python thread holding the GIL."""

from __future__ import annotations

import faulthandler
import math
import os
from collections.abc import Generator
from dataclasses import dataclass, field
from threading import Event, Thread
from types import ModuleType

import pytest


@dataclass
class ScheduledTraceback:
    timeout: float
    output_fd: int
    _cancelled: Event = field(default_factory=Event, init=False)
    _thread: Thread | None = field(default=None, init=False)
    _failure: BaseException | None = field(default=None, init=False)
    _started: bool = field(default=False, init=False)

    def start(self) -> None:
        if self._started or not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("A diagnostic timer needs one finite positive interval")
        self._started = True
        descriptor = os.dup(self.output_fd)

        def run() -> None:
            try:
                if not self._cancelled.wait(self.timeout):
                    os.write(descriptor, f"Timeout ({self.timeout:g}s)! Scheduled Python-owned traceback\n".encode())
                    # Unlike dump_traceback_later's native watchdog, this call
                    # enters through Python with the GIL and retains live code
                    # metadata while the other threads' frames are inspected.
                    faulthandler.dump_traceback(file=descriptor, all_threads=True)
            except BaseException as exc:
                self._failure = exc
            finally:
                os.close(descriptor)

        self._thread = Thread(target=run, name="pytest-safe-traceback", daemon=True)
        try:
            self._thread.start()
        except BaseException:
            os.close(descriptor)
            self._thread = None
            raise

    def stop(self) -> None:
        self._cancelled.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(5)
        if thread.is_alive():
            raise RuntimeError("Scheduled diagnostic thread did not stop")
        self._thread = None
        if self._failure is not None:
            raise RuntimeError("Scheduled diagnostic traceback failed") from self._failure


@dataclass
class _SafeFaultHandler:
    original: ModuleType
    current: ScheduledTraceback | None = None
    native_active: bool = False

    def cancel(self) -> None:
        if self.native_active:
            self.original.pytest_enter_pdb()
        timer, self.current = self.current, None
        if timer is not None:
            timer.stop()

    @pytest.hookimpl(wrapper=True, trylast=True)
    def pytest_runtest_protocol(self, item: pytest.Item) -> Generator[None, object, object]:
        timeout = self.original.get_timeout_config_value(item.config)
        if self.original.get_exit_on_timeout_config_value(item.config):
            # A Python timer cannot enforce a hard exit while native code holds
            # the GIL. Retain pytest's original native hard-deadline protocol.
            self.native_active = True
            try:
                return (yield from self.original.pytest_runtest_protocol(item))
            finally:
                self.native_active = False
        if timeout > 0:
            if self.current is not None:
                raise RuntimeError("Overlapping pytest diagnostic ownership")
            descriptor = item.config.stash[self.original.fault_handler_stderr_fd_key]
            self.current = ScheduledTraceback(timeout, descriptor)
            self.current.start()
        try:
            return (yield)
        finally:
            self.cancel()

    @pytest.hookimpl(tryfirst=True)
    def pytest_enter_pdb(self) -> None:
        self.cancel()

    @pytest.hookimpl(tryfirst=True)
    def pytest_exception_interact(self) -> None:
        self.cancel()

    def pytest_unconfigure(self, config: pytest.Config) -> None:
        try:
            self.cancel()
        finally:
            # The original plugin configured native fatal-signal handling and
            # owns its stderr descriptor and original-handler restoration.
            self.original.pytest_unconfigure(config)


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    original = config.pluginmanager.get_plugin("faulthandler")
    if original is None or isinstance(original, _SafeFaultHandler):
        return
    required = ("get_timeout_config_value", "get_exit_on_timeout_config_value", "fault_handler_stderr_fd_key", "pytest_runtest_protocol", "pytest_enter_pdb", "pytest_unconfigure")
    if not isinstance(original, ModuleType) or any(not hasattr(original, name) for name in required) or original.fault_handler_stderr_fd_key not in config.stash:
        raise pytest.UsageError("The installed pytest faulthandler adapter is unsupported")
    # Use the public plugin-manager API after the original configure hook.
    # No options, capture settings, test selection or fatal handlers change.
    config.pluginmanager.unregister(original)
    config.pluginmanager.register(_SafeFaultHandler(original), "faulthandler")
