"""Observe controller dispatch to a local LLM without claiming provider liveness."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from .execution_trace import EventKind, Outcome, current_scope, get_trace_collector
from .logger_config import get_logger

logger = get_logger(__name__)


@contextmanager
def observe_local_llm_call(*, backend: str, backend_type: str, provider: str | None, model: str | None, invocation_id: str, continuation: bool, is_noedit: bool) -> Iterator[None]:
    """Bracket the actual client call after workspace preparation and admission."""
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
