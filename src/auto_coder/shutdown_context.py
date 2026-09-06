"""Cross-module admission checkpoint for a daemon processing operation."""

from contextvars import ContextVar, Token
from typing import Callable

_admission_check: ContextVar[Callable[[], bool]] = ContextVar("auto_coder_admission_check", default=lambda: True)


def install_admission_check(check: Callable[[], bool]) -> Token[Callable[[], bool]]:
    """Install the engine lifecycle check in the current operation context."""
    return _admission_check.set(check)


def reset_admission_check(token: Token[Callable[[], bool]]) -> None:
    _admission_check.reset(token)


def new_work_allowed() -> bool:
    """Return whether the current daemon operation may enter a later stage."""
    return _admission_check.get()()
