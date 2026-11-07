"""Bounded, non-authorizing projection of retained PR processing evidence."""

from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .github_pending_work import inspect_pending_work
from .public_api import sanitize_text
from .runtime_locks import lock_path

SCHEMA_VERSION = 1
MAX_RECORDS = 100
READ_DEADLINE_SECONDS = 0.25


def _safe(value: object) -> str:
    return sanitize_text(str(value), paths=True)[0]


def _section(producer: str, availability: str, **values: object) -> dict[str, object]:
    return {"producer": producer, "availability": availability, **values}


def _read_json(path: Path, lock: Path, producer: str) -> tuple[str, object | None, str]:
    """Read behind an existing writer lock without creating either store or lock."""
    if not path.is_file():
        return "ABSENT", None, "store_not_present"
    if not lock.is_file():
        # A pre-lock legacy store can still be inspected; the before/after
        # identity check below prevents presenting a changing read as stable.
        lock_fd = None
    else:
        lock_fd = os.open(lock, os.O_RDONLY | os.O_CLOEXEC)
    deadline = time.monotonic() + READ_DEADLINE_SECONDS
    try:
        if lock_fd is not None:
            while True:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        return "UNAVAILABLE", None, "read_lock_contended"
                    time.sleep(0.01)
        before = path.stat()
        if before.st_size > 16 * 1024 * 1024:
            return "UNSUPPORTED", None, "store_exceeds_read_limit"
        value = json.loads(path.read_text(encoding="utf-8"))
        after = path.stat()
        if (before.st_ino, before.st_mtime_ns, before.st_size) != (after.st_ino, after.st_mtime_ns, after.st_size):
            return "CONFLICT", None, "store_changed_during_read"
        return "COMPLETE", value, ""
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        return "CORRUPT", None, _safe(type(exc).__name__)
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)


def _json_section(path: Path, lock: Path, producer: str, select: Callable[[object], list[dict[str, object]]]) -> dict[str, object]:
    availability, value, reason = _read_json(path, lock, producer)
    if availability != "COMPLETE":
        return _section(producer, availability, reason=reason, records=[])
    try:
        records = select(value)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        return _section(producer, "CORRUPT", reason=_safe(type(exc).__name__), records=[])
    return _section(producer, "COMPLETE", observed_revision=path.stat().st_mtime_ns, records=records)


def project_pr_diagnostics(repository: str, pr_number: int, *, limit: int = 50, cursor: int = 0, home: Path | None = None) -> dict[str, object]:
    """Inspect existing stores only; this function has no mutation dependencies."""
    if type(pr_number) is not int or not 0 < pr_number <= 999_999_999 or type(limit) is not int or not 1 <= limit <= MAX_RECORDS or type(cursor) is not int or cursor < 0:
        raise ValueError("invalid PR diagnostic selection")
    root = home or Path.home()
    store_root = root / ".auto-coder" / repository

    def cycle(value: object) -> list[dict[str, object]]:
        raw = value["prs"].get(str(pr_number))  # type: ignore[index]
        if raw is None:
            return []
        claim = raw.get("active_claim")
        rounds = raw.get("strong_rounds", [])
        closures = raw.get("closures", [])
        return [
            {
                "pr_number": pr_number,
                "open_epoch": raw.get("open_epoch"),
                "transition_version": raw.get("transition_version"),
                "finding_set_revision": raw.get("finding_set_revision"),
                "accepted_strong_round_id": raw.get("accepted_strong_round_id"),
                "accepted_closure_id": raw.get("accepted_closure_id"),
                "pending_effect": raw.get("pending_effect"),
                "active_claim": None if not isinstance(claim, dict) else {key: claim.get(key) for key in ("claim_id", "kind", "round_id", "head_sha", "base_sha", "contract_identity", "policy_identity", "claimed_at")},
                "rounds": [{key: item.get(key) for key in ("round_id", "sequence", "head_sha", "base_sha", "contract_identity", "policy_identity", "finding_set_revision", "publication_status", "publication_receipt", "accepted_at")} for item in rounds if isinstance(item, dict)],
                "closures": [{key: item.get(key) for key in ("closure_id", "round_id", "head_sha", "base_sha", "contract_identity", "policy_identity", "finding_set_revision", "publication_status", "publication_receipt", "accepted_at")} for item in closures if isinstance(item, dict)],
            }
        ]

    def decision(value: object) -> list[dict[str, object]]:
        if not isinstance(value, dict):
            raise ValueError("effective-decision root is invalid")
        raw = value.get(str(pr_number))
        if not isinstance(raw, dict):
            return []
        allowed = (
            "pr_number",
            "head_sha",
            "base_sha",
            "status",
            "next_action",
            "attempt_id",
            "attempt_sequence",
            "source_revision",
            "finding_set_revision",
            "association_revision",
            "reopen_epoch",
            "evidence_revision",
            "publication",
            "handoff",
            "wait_reason",
            "handoff_generation",
            "handoff_observation",
            "updated_at",
        )
        return [{key: (_safe(raw.get(key)) if key in {"wait_reason", "handoff_observation"} else raw.get(key)) for key in allowed}]

    def effects(value: object) -> list[dict[str, object]]:
        operations = value["operations"].values()  # type: ignore[index]
        allowed = ("operation_id", "payload_identity", "pr_number", "mode", "round_id", "target_head", "contract_identity", "finding_set_revision", "purpose", "destination", "status", "receipt", "reason", "updated_at")
        selected = []
        for raw in operations:
            if isinstance(raw, dict) and raw.get("repository") == repository and raw.get("pr_number") == pr_number:
                selected.append({key: (_safe(raw.get(key)) if key in {"destination", "receipt", "reason"} else raw.get(key)) for key in allowed})
        return sorted(selected, key=lambda item: float(item.get("updated_at") or 0))

    files = {
        "review_cycle": ("PrReviewCycleRepository", store_root / "pr_review_cycle.json", "pr-review-cycle-store", cycle),
        "effective_decision": ("EffectiveDecisionStore", store_root / "effective_review_decisions.json", "effective-decision-store", decision),
        "review_effects": ("ReviewEffectRepository", store_root / "pr_review_effects.json", "pr-review-effects", effects),
    }
    sections: dict[str, object] = {}
    for name, (producer, path, purpose, selector) in files.items():
        section = _json_section(path, lock_path(repository, path, purpose), producer, selector)
        records = section.get("records", [])
        if isinstance(records, list):
            page = records[cursor : cursor + limit]
            section["records"] = page
            section["truncated"] = cursor + len(page) < len(records)
            section["next_cursor"] = cursor + len(page) if section["truncated"] else None
        sections[name] = section

    inspection = inspect_pending_work(repository, home=root)
    pending = []
    for view in inspection.views:
        for obligation in view.obligations:
            identity = obligation.identity
            if identity.entity in {f"pr:{pr_number}", f"pr#{pr_number}", str(pr_number)}:
                raw = asdict(obligation)
                raw["identity"] = asdict(identity)
                raw["last_error"] = _safe(raw.get("last_error", ""))
                pending.append(raw)
    pending_page = pending[cursor : cursor + limit]
    pending_available = bool(inspection.views)
    pending_truncated = cursor + len(pending_page) < len(pending)
    sections["pending_work"] = _section(
        "PendingWorkStore",
        "UNAVAILABLE" if inspection.errors else "COMPLETE" if pending_available else "ABSENT",
        reason="; ".join(_safe(error) for error in inspection.errors) or (None if pending_available else "store_not_present"),
        records=pending_page,
        truncated=pending_truncated,
        next_cursor=cursor + len(pending_page) if pending_truncated else None,
    )
    return {"schema_version": SCHEMA_VERSION, "pr_number": pr_number, "repository": repository, "selector": {"limit": limit, "cursor": cursor}, "sections": sections, "overall_readiness": None}
