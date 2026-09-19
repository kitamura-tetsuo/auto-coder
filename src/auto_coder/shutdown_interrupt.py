"""Marks the local subprocess/tool tree of an admitted LLM invocation.

Graceful draining (Issue #2010) must stop an unrelated local subprocess --
an update check, a git/gh maintenance call, a controller-run test -- without
waiting for its natural completion, external response, or normal network
timeout, while never touching the subprocess tree that belongs to an
already-admitted, still in-flight LLM invocation (see
``invocation_admission.py`` and Issue #2009). Command execution has a single
production boundary (``utils.CommandExecutor``), so this ambient flag lets
that boundary distinguish the two without every maintenance call site having
to thread an explicit parameter through.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_invocation_active: ContextVar[bool] = ContextVar("auto_coder_invocation_active", default=False)


@contextmanager
def mark_invocation_active() -> Iterator[None]:
    """Mark the current context as running inside an admitted LLM invocation.

    A subprocess spawned while this is active is part of that invocation's
    own controlled provider action or tool tree; graceful draining's
    cooperative interruption must never kill it. Only an explicit
    force-stop may abandon it (REQ-004).
    """
    token = _invocation_active.set(True)
    try:
        yield
    finally:
        _invocation_active.reset(token)


def is_invocation_active() -> bool:
    """Return whether the current context is inside an admitted invocation."""
    return _invocation_active.get()
