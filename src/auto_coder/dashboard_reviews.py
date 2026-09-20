"""Read-only dashboard projections for the durable review audit."""

from __future__ import annotations

import dataclasses
import json
from typing import Optional, Tuple

from .review_audit import ReviewAuditRecord


@dataclasses.dataclass(frozen=True)
class ReviewListRow:
    review_id: str
    kind: str
    target: str
    observed_at: str
    lifecycle: str
    mode: str
    verdict: str
    generation: str
    backend: str
    detail_path: str


def backend_summary(record: ReviewAuditRecord) -> str:
    """Keep configured/requested and provider-reported models distinct."""
    values = []
    for interaction in record.interactions:
        configured = interaction.backend_alias or interaction.backend_type or "unknown backend"
        requested = interaction.requested_model or "model not recorded"
        reported = interaction.reported_model or "reported model unavailable"
        values.append(f"{configured} / requested {requested} / {reported}")
    return "; ".join(values) if values else "No backend invocation recorded"


def list_row(record: ReviewAuditRecord) -> ReviewListRow:
    return ReviewListRow(
        review_id=record.review_id,
        kind=record.review_kind,
        target=f"{record.target_type} #{record.target_number}",
        observed_at=record.creation_time,
        lifecycle=record.lifecycle.value,
        mode=record.execution_mode.value,
        verdict=record.native_verdict or "No native verdict",
        generation=record.reviewed_generation or "Unavailable",
        backend=backend_summary(record),
        detail_path=f"/detail/{record.target_type}/{record.target_number}?review_id={record.review_id}",
    )


def record_signature(record: ReviewAuditRecord) -> Tuple[object, ...]:
    """Stable signature used to avoid rebuilding unchanged report DOM."""
    return (
        record.review_id,
        record.lifecycle.value,
        record.execution_mode.value,
        record.native_verdict,
        json.dumps(record.native_report, sort_keys=True, default=str),
        tuple(dataclasses.astuple(item) for item in record.interactions),
        tuple((item.effect_id, item.observation_time, item.disposition, json.dumps(item.details, sort_keys=True, default=str)) for item in record.effects),
    )


def selection_error(record: Optional[ReviewAuditRecord], target_type: str, target_number: int) -> Optional[str]:
    if record is None:
        return "Review unavailable or removed; no other review was selected."
    if record.target_type != target_type or record.target_number != str(target_number):
        return "Review ID is not recorded for this repository and target; no other review was selected."
    return None
