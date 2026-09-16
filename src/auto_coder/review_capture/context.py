import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class BoundReviewContext:
    review_id: str
    repository: str
    target_type: str
    target_number: str
    review_kind: str
    generation_identity: str


_active_review_context: contextvars.ContextVar[Optional[BoundReviewContext]] = contextvars.ContextVar("active_review_context", default=None)

_active_interaction_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("active_interaction_id", default=None)


@contextmanager
def bind_review_context(
    review_id: str,
    repository: str,
    target_type: str,
    target_number: str,
    review_kind: str,
    generation_identity: str,
):
    """
    Bind a scoped review context for the duration of the 'with' block.
    This context is thread-local and propagated safely via contextvars.
    """
    ctx = BoundReviewContext(
        review_id=review_id,
        repository=repository,
        target_type=target_type,
        target_number=target_number,
        review_kind=review_kind,
        generation_identity=generation_identity,
    )
    token = _active_review_context.set(ctx)
    try:
        yield
    finally:
        _active_review_context.reset(token)


@contextmanager
def bind_interaction_id(interaction_id: str):
    """
    Bind an interaction ID for the duration of a backend call.
    """
    token = _active_interaction_id.set(interaction_id)
    try:
        yield
    finally:
        _active_interaction_id.reset(token)


def get_active_review_context() -> Optional[BoundReviewContext]:
    return _active_review_context.get()


def get_active_interaction_id() -> Optional[str]:
    return _active_interaction_id.get()
