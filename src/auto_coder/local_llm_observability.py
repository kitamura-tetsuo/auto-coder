"""Observe controller dispatch to a local LLM without claiming provider liveness."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Callable, Iterator

from .execution_trace import EventKind, Outcome, current_scope, get_trace_collector
from .logger_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class LocalLLMCallStart:
    backend: str
    model: str


LocalLLMStartCallback = Callable[[LocalLLMCallStart], None]
_start_callback: ContextVar[LocalLLMStartCallback | None] = ContextVar("local_llm_start_callback", default=None)


def bind_local_llm_start_callback(callback: LocalLLMStartCallback) -> Token[LocalLLMStartCallback | None]:
    """Bind an Issue-owned side effect to the actual local client boundary."""
    return _start_callback.set(callback)


def reset_local_llm_start_callback(token: Token[LocalLLMStartCallback | None]) -> None:
    _start_callback.reset(token)


@contextmanager
def observe_local_llm_call(*, backend: str, backend_type: str, provider: str | None, model: str | None, invocation_id: str, continuation: bool, is_noedit: bool) -> Iterator[None]:
    """Bracket the actual client call after workspace preparation and admission."""
    callback = _start_callback.get()
    if callback is not None and not is_noedit:
        try:
            callback(LocalLLMCallStart(backend=backend, model=model or "unknown"))
        except Exception as exc:
            # GitHub notification evidence is deliberately outside invocation
            # authority. Even a durable-state failure cannot stop the client.
            logger.warning("Local Issue start notification failed before client dispatch: {}", type(exc).__name__)
    collector = get_trace_collector()
    scope = current_scope()
    facts = {
        "backend": backend,
        "backend_type": backend_type,
        "provider": provider,
        "model": model,
        "invocation_id": invocation_id,
        "invocation_mode": "continuation" if continuation else "fresh",
        "phase": "read-only" if is_noedit else "implementation",
    }
    label = f"Local LLM call: {backend}"
    collector.record_event(EventKind.STAGE_STARTED, "llm.local-execution", "local-backend", label=label, facts=facts, scope=scope)
    logger.info(
        "Local LLM call started: backend={} type={} provider={} model={} mode={} phase={} invocation_id={} repository={} target={} execution_id={}",
        backend,
        backend_type,
        provider,
        model,
        facts["invocation_mode"],
        facts["phase"],
        invocation_id,
        scope.repository if scope else None,
        f"{scope.item_type}#{scope.item_number}" if scope else None,
        scope.execution_id if scope else None,
    )
    try:
        yield
    except BaseException as exc:
        collector.record_event(EventKind.STAGE_RESULT, "llm.local-execution", "local-backend", label=label, outcome=Outcome.FAILED, facts={**facts, "error": type(exc).__name__}, scope=scope)
        logger.warning("Local LLM call failed: backend={} invocation_id={} error={}", backend, invocation_id, type(exc).__name__)
        raise
    else:
        collector.record_event(EventKind.STAGE_RESULT, "llm.local-execution", "local-backend", label=label, outcome=Outcome.COMPLETED, facts=facts, scope=scope)
        logger.info("Local LLM call returned: backend={} invocation_id={}", backend, invocation_id)
