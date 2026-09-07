"""Durable, non-normative Objective evidence for Issue reviews."""

from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator, Optional

OBJECTIVE_EVIDENCE_POLICY = "objective-anchor-v1"


@dataclass(frozen=True)
class ObjectiveExtraction:
    status: str
    text: Optional[str] = None


@dataclass(frozen=True)
class ObjectiveAnchor:
    issue_number: int
    state: str
    original_text: Optional[str]
    source_identity: str
    current: ObjectiveExtraction


def extract_objective(body: str) -> ObjectiveExtraction:
    """Structurally extract an Objective without interpreting its prose."""
    lines = body.replace("\r\n", "\n").split("\n")
    sections: list[str] = []
    current: Optional[list[str]] = None
    fence: Optional[str] = None
    for line in lines:
        fence_match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker[0]
            elif marker[0] == fence:
                fence = None
            if current is not None:
                current.append(line)
            continue
        if fence is not None:
            if current is not None:
                current.append(line)
            continue
        if re.fullmatch(r"## Objective[ \t]*", line):
            if current is not None:
                sections.append("\n".join(current).strip())
            current = []
            continue
        if current is not None and re.match(r"^#{1,2}(?:[ \t]+|$)", line):
            sections.append("\n".join(current).strip())
            current = None
            continue
        if current is not None:
            current.append(line)
    if current is not None:
        sections.append("\n".join(current).strip())
    if not sections:
        return ObjectiveExtraction("ABSENT")
    if len(sections) != 1 or not sections[0]:
        return ObjectiveExtraction("INVALID")
    return ObjectiveExtraction("PRESENT", sections[0])


class ObjectiveAnchorStore:
    """Atomic repository-scoped anchors shared by every review path."""

    def __init__(self, repository: str, path: Optional[Path] = None) -> None:
        root = Path(os.environ.get("AUTO_CODER_SPECIFICATION_VALIDATION_ROOT", Path.home() / ".auto-coder"))
        self.path = path or root / repository / "individual_review_history.json"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        import fcntl

        lock_path = self.path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _read(self) -> dict[str, object]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(value, dict):
            raise ValueError("Invalid individual-review history root")
        return value

    def _write(self, state: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
        temporary.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.path)

    def capture(self, issue_number: int, current_body: str, source_identity: str) -> ObjectiveAnchor:
        """Capture once, migrating an existing first-valid baseline when present."""
        current = extract_objective(current_body)
        key = str(issue_number)
        with self._locked():
            state = self._read()
            raw = state.get(key)
            if raw is not None and not isinstance(raw, dict):
                raise ValueError(f"Invalid individual-review history for Issue #{issue_number}")
            record = raw if isinstance(raw, dict) else {}
            saved = record.get("objective_anchor")
            if saved is None:
                baseline = record.get("baseline")
                source_body = current_body
                effective_source = source_identity
                if baseline is not None:
                    if not isinstance(baseline, str):
                        raise ValueError(f"Invalid individual-review baseline for Issue #{issue_number}")
                    try:
                        baseline_payload = json.loads(baseline)
                    except (json.JSONDecodeError, TypeError) as exc:
                        raise ValueError(f"Unreadable individual-review baseline for Issue #{issue_number}") from exc
                    if not isinstance(baseline_payload, dict) or not isinstance(baseline_payload.get("body"), str):
                        raise ValueError(f"Unreadable individual-review baseline for Issue #{issue_number}")
                    source_body = baseline_payload["body"]
                    effective_source = "individual-first-valid-baseline:v1"
                extracted = extract_objective(source_body)
                if extracted.status == "INVALID":
                    raise ValueError(f"Invalid Objective evidence for Issue #{issue_number}")
                saved = {
                    "state": "ANCHORED" if extracted.status == "PRESENT" else "UNANCHORED",
                    "original_text": extracted.text,
                    "source_identity": effective_source,
                    "policy": OBJECTIVE_EVIDENCE_POLICY,
                }
                record["objective_anchor"] = saved
                state[key] = record
                self._write(state)
            if not isinstance(saved, dict) or saved.get("state") not in {"ANCHORED", "UNANCHORED"} or saved.get("policy") != OBJECTIVE_EVIDENCE_POLICY:
                raise ValueError(f"Invalid Objective anchor for Issue #{issue_number}")
            original = saved.get("original_text")
            if (saved["state"] == "ANCHORED" and not isinstance(original, str)) or (saved["state"] == "UNANCHORED" and original is not None):
                raise ValueError(f"Invalid Objective anchor for Issue #{issue_number}")
            source = saved.get("source_identity")
            if not isinstance(source, str) or not source:
                raise ValueError(f"Invalid Objective anchor source for Issue #{issue_number}")
            return ObjectiveAnchor(issue_number, str(saved["state"]), original, source, current)


def objective_evidence_json(anchor: ObjectiveAnchor) -> str:
    return json.dumps(asdict(anchor), ensure_ascii=False, sort_keys=True, indent=2)
