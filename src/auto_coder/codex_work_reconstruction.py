"""Bounded reconstruction of durable Codex producer journals."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

from .codex_work_accounting import CodexWorkOperation, ReconstructionReceipt

REQUIRED_CODEX_SOURCES = (
    "cloud-runs",
    "cloud-bindings",
    "retry-authorizations",
    "retry-dispatch",
    "follow-ups",
    "repair-admissions",
    "pr-recovery",
)


@dataclass(frozen=True)
class CodexSourceSnapshot:
    """One atomic source enumeration and its store-provided consistency token."""

    source: str
    consistency_id: str
    operations: tuple[CodexWorkOperation, ...] = ()


class CodexWorkReconstructor:
    """Create receipts only from two identical complete source observations."""

    def __init__(self, readers: Mapping[str, Callable[[], CodexSourceSnapshot]]) -> None:
        self.readers = dict(readers)

    def consistency_ids(self, sources: tuple[str, ...] = REQUIRED_CODEX_SOURCES) -> Mapping[str, str]:
        snapshots = self._read(sources)
        return {snapshot.source: snapshot.consistency_id for snapshot in snapshots}

    def reconstruct(self) -> ReconstructionReceipt:
        missing = set(REQUIRED_CODEX_SOURCES) - set(self.readers)
        extra = set(self.readers) - set(REQUIRED_CODEX_SOURCES)
        if missing or extra:
            raise ValueError(f"Codex reconstruction sources mismatch; missing={sorted(missing)}, extra={sorted(extra)}")
        first = self._read(REQUIRED_CODEX_SOURCES)
        second = self._read(REQUIRED_CODEX_SOURCES)
        first_ids = {item.source: item.consistency_id for item in first}
        second_ids = {item.source: item.consistency_id for item in second}
        if first_ids != second_ids:
            raise RuntimeError("Codex source changed during reconstruction")
        operations = tuple(operation for item in second for operation in item.operations)
        operation_ids = [operation.logical_operation_id for operation in operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("Codex source enumeration contains conflicting logical operations")
        manifest = tuple((item.source, tuple(operation.logical_operation_id for operation in item.operations)) for item in second)
        identity = hashlib.sha256(json.dumps({"sources": second_ids, "operations": operation_ids}, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return ReconstructionReceipt(
            receipt_id=f"reconstruction:{identity}:{uuid.uuid4().hex}",
            sources=REQUIRED_CODEX_SOURCES,
            consistent_sources=tuple(item.source for item in second),
            source_operation_ids=manifest,
            operations=operations,
            source_consistency_ids=tuple(sorted(second_ids.items())),
        )

    def _read(self, sources: Iterable[str]) -> tuple[CodexSourceSnapshot, ...]:
        result: list[CodexSourceSnapshot] = []
        for source in sources:
            snapshot = self.readers[source]()
            if snapshot.source != source or not snapshot.consistency_id:
                raise ValueError(f"Invalid Codex source snapshot for {source}")
            result.append(snapshot)
        return tuple(result)
