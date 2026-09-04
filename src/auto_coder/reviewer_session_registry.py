"""Persistent, PR-scoped adversarial reviewer session associations."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import ClassVar, List, Optional


@dataclass
class TestOracleGap:
    """Persisted material gap in regression protection for an Issue requirement."""

    __test__: ClassVar[bool] = False

    gap_id: str = ""
    requirement_id: str = ""
    requirement_text: str = ""
    authoritative_boundary: str = ""
    invariant: str = ""
    plausible_incorrect_implementation: str = ""
    why_tests_still_pass: str = ""
    material_consequence: str = ""
    focused_regression_scenario: str = ""
    anchor_path: str = ""
    anchor_line: Optional[int] = None
    anchor_side: str = "RIGHT"
    anchor_start_line: Optional[int] = None
    discovery_phase: str = "INITIAL"
    rereview_exception_reason: str = "NONE"
    rereview_exception_evidence: str = ""
    status: str = "OPEN"
    resolution_evidence: str = ""


@dataclass
class RecoveredFileEvidence:
    """Current-head evidence that resolved an initially incomplete file."""

    path: str = ""
    source: str = ""
    status: str = "RECOVERED"
    evidence: str = ""
    requirement_ids: List[str] = field(default_factory=list)


@dataclass
class ReviewerSession:
    repository: str = ""
    pr_number: int = 0
    backend_name: str = ""
    backend_type: str = ""
    model_name: str = ""
    session_id: str = ""
    last_head_sha: str = ""
    test_oracle_gaps: List[TestOracleGap] = field(default_factory=list)
    evidence_head_sha: str = ""
    recovered_file_evidence: List[RecoveredFileEvidence] = field(default_factory=list)


class ReviewerSessionRegistry:
    """Store reviewer sessions independently from global implementation sessions."""

    _lock = threading.RLock()

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or Path.home() / ".auto-coder" / "reviewer_sessions.json"

    @staticmethod
    def _key(repository: str, pr_number: int, backend_name: str, backend_type: str, model_name: str) -> str:
        return json.dumps([repository, pr_number, backend_name, backend_type, model_name], separators=(",", ":"))

    def _load(self) -> dict[str, dict[str, object]]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def get(self, repository: str, pr_number: int, backend_name: str, backend_type: str, model_name: str) -> Optional[ReviewerSession]:
        with self._lock:
            raw = self._load().get(self._key(repository, pr_number, backend_name, backend_type, model_name))
            if not isinstance(raw, dict):
                return None
            try:
                stored_pr_number = raw["pr_number"]
                if not isinstance(stored_pr_number, int):
                    return None
                raw_gaps = raw.get("test_oracle_gaps", [])
                if not isinstance(raw_gaps, list):
                    return None
                gaps: List[TestOracleGap] = []
                for raw_gap in raw_gaps:
                    if not isinstance(raw_gap, dict):
                        return None
                    gaps.append(TestOracleGap(**raw_gap))
                raw_evidence = raw.get("recovered_file_evidence", [])
                if not isinstance(raw_evidence, list):
                    return None
                recovered_evidence: List[RecoveredFileEvidence] = []
                for raw_entry in raw_evidence:
                    if not isinstance(raw_entry, dict):
                        return None
                    recovered_evidence.append(RecoveredFileEvidence(**raw_entry))
                return ReviewerSession(
                    repository=str(raw["repository"]),
                    pr_number=stored_pr_number,
                    backend_name=str(raw["backend_name"]),
                    backend_type=str(raw["backend_type"]),
                    model_name=str(raw["model_name"]),
                    session_id=str(raw["session_id"]),
                    last_head_sha=str(raw["last_head_sha"]),
                    test_oracle_gaps=gaps,
                    evidence_head_sha=str(raw.get("evidence_head_sha", "")),
                    recovered_file_evidence=recovered_evidence,
                )
            except (KeyError, TypeError, ValueError):
                return None

    def save(self, session: ReviewerSession) -> None:
        if not session.session_id:
            return
        with self._lock:
            data = self._load()
            data[self._key(session.repository, session.pr_number, session.backend_name, session.backend_type, session.model_name)] = asdict(session)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(f"{self.path.suffix}.{os.getpid()}.tmp")
            temporary.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(temporary, self.path)

    def remove_pr(self, repository: str, pr_number: int) -> None:
        with self._lock:
            data = self._load()
            retained = {key: value for key, value in data.items() if not (isinstance(value, dict) and value.get("repository") == repository and value.get("pr_number") == pr_number)}
            if retained == data:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(retained, indent=2, sort_keys=True), encoding="utf-8")
