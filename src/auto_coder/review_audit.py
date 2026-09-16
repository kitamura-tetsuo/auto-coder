"""Durable, non-authorizing review audit model and local read interface."""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from loguru import logger

SCHEMA_VERSION = 1
_REDACTION_REPLACEMENT = "[REDACTED]"

# Mandatory token patterns (REQ-008)
_TOKEN_PATTERNS = [
    re.compile(r"gh[pousr]_[a-zA-Z0-9]+"),
    re.compile(r"github_pat_[a-zA-Z0-9_]+"),
    re.compile(r"AIza[0-9A-Za-z-_]{35}"),
    re.compile(r"sk-[a-zA-Z0-9]{48}"),
    re.compile(r"sk-proj-[a-zA-Z0-9_-]+"),
    re.compile(r"sk-ant-api\d{2}-[\w-]{20,}"),
    re.compile(r"(AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-([0-9a-zA-Z]{10,48})?"),
    re.compile(r"glpat-[0-9a-zA-Z\-_]{20}"),
]

_SENSITIVE_KEYS = {
    "authorization",
    "proxy_authorization",
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "apikey",
    "x_api_key",
}


class StorageHealth(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNINITIALIZED = "UNINITIALIZED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclasses.dataclass
class AuditReadResult:
    health: StorageHealth
    records: List[ReviewAuditRecord]


@dataclasses.dataclass
class AuditSingleReadResult:
    health: StorageHealth
    record: Optional[ReviewAuditRecord]


class EvaluationLifecycle(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    FINISHED = "FINISHED"
    CANCELLED = "CANCELLED"


class ExecutionMode(str, Enum):
    UNKNOWN = "UNKNOWN"
    EXECUTED = "EXECUTED"
    REUSED = "REUSED"
    LOCAL_ONLY = "LOCAL_ONLY"
    BYPASSED = "BYPASSED"


@dataclasses.dataclass
class ReviewEffectRecord:
    review_id: str
    effect_id: str
    observation_time: str
    disposition: str  # pending, confirmed, failed, superseded, unknown
    details: Optional[Dict[str, Any]] = None


@dataclasses.dataclass
class ReviewInteractionRecord:
    interaction_id: str
    review_id: str
    start_time: str
    end_time: Optional[str]
    duration_ms: Optional[int]
    backend_alias: Optional[str]
    backend_type: Optional[str]
    provider_alias: Optional[str]
    requested_model: Optional[str]
    reported_model: Optional[str]
    invocation_mode: Optional[str]
    session_identity: Optional[str]
    completion_status: str  # RETURNED, RAISED, unrecorded


@dataclasses.dataclass
class ReviewAuditRecord:
    review_id: str
    repository: str
    target_type: str
    target_number: str
    review_kind: str  # issue_specification, issue_decomposition, pr_adversarial
    origin: str
    process_identity: str
    creation_time: str
    creation_sequence: int
    reviewed_generation: str
    policy_identity: str
    related_issue_membership: Optional[str]
    diagnostic_execution_references: Optional[List[str]]
    lifecycle: EvaluationLifecycle
    execution_mode: ExecutionMode
    native_verdict: Optional[str]
    native_report: Optional[Dict[str, Any]]
    source_review_id: Optional[str]  # For REUSED
    interactions: List[ReviewInteractionRecord] = dataclasses.field(default_factory=list)
    effects: List[ReviewEffectRecord] = dataclasses.field(default_factory=list)


def _normalize_key(key: str) -> str:
    return key.lower().replace("-", "_")


def redact_sensitive_data(data: Any, credentials: Optional[Sequence[str]] = None) -> Any:
    """Redact sensitive credentials and known token patterns."""
    creds_list = [c for c in (credentials or []) if c]

    if isinstance(data, str):
        # 1. Exact string matches of supplied credentials
        for cred in creds_list:
            data = data.replace(cred, _REDACTION_REPLACEMENT)

        # 2. Leftmost non-overlapping regex matches for token patterns
        for pattern in _TOKEN_PATTERNS:
            data = pattern.sub(_REDACTION_REPLACEMENT, data)
        return data
    elif isinstance(data, dict):
        result = {}
        for k, v in data.items():
            if _normalize_key(str(k)) in _SENSITIVE_KEYS:
                result[k] = _REDACTION_REPLACEMENT
            else:
                result[k] = redact_sensitive_data(v, creds_list)
        return result
    elif isinstance(data, list):
        return [redact_sensitive_data(item, creds_list) for item in data]
    else:
        return data


class ReviewAuditStore:
    def __init__(self, audit_root: Optional[Union[str, Path]] = None):
        self._audit_root = self._resolve_root(audit_root)
        self._local_lock = threading.Lock()

    @staticmethod
    def _resolve_root(supplied: Optional[Union[str, Path]]) -> Path:
        if supplied:
            p = Path(supplied)
            if str(p).strip():
                return p.resolve()

        env_root = os.environ.get("AUTO_CODER_REVIEW_AUDIT_ROOT", "").strip()
        if env_root:
            return Path(env_root).resolve()

        return Path.home() / ".auto-coder" / "review_audit"

    def _get_db_path(self, repository: str) -> Path:
        import hashlib

        # Hash to prevent collision and path traversal
        hash_digest = hashlib.sha256(repository.encode("utf-8")).hexdigest()
        repo_dir = self._audit_root / hash_digest
        return repo_dir / "audit.db"

    def _connect_readonly(self, repository: str) -> Tuple[Optional[sqlite3.Connection], StorageHealth]:
        db_path = self._get_db_path(repository)
        if not db_path.exists():
            return None, StorageHealth.UNINITIALIZED

        try:
            # open read-only URI
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            return conn, StorageHealth.AVAILABLE
        except sqlite3.Error as e:
            logger.error(f"Audit read failed for {repository}: {e}")
            return None, StorageHealth.UNAVAILABLE
        except Exception as e:
            logger.error(f"Audit read error for {repository}: {e}")
            return None, StorageHealth.UNAVAILABLE

    def _ensure_db(self, repository: str) -> Optional[sqlite3.Connection]:
        """Ensures the directory and database exist, returning a connection or None on failure."""
        db_path = self._get_db_path(repository)
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

            # Use os.open to securely create the file if it doesn't exist
            if not db_path.exists():
                fd = os.open(str(db_path), os.O_CREAT | os.O_WRONLY, 0o600)
                os.close(fd)

            conn = sqlite3.connect(str(db_path), timeout=5.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")

            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS evaluation (
                        review_id TEXT PRIMARY KEY,
                        repository TEXT,
                        target_type TEXT,
                        target_number TEXT,
                        review_kind TEXT,
                        origin TEXT,
                        process_identity TEXT,
                        creation_time TEXT,
                        creation_sequence INTEGER,
                        reviewed_generation TEXT,
                        policy_identity TEXT,
                        related_issue_membership TEXT,
                        diagnostic_execution_references TEXT, -- JSON list
                        lifecycle TEXT,
                        execution_mode TEXT,
                        native_verdict TEXT,
                        native_report TEXT, -- JSON object
                        source_review_id TEXT
                    )
                """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS interaction (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        interaction_id TEXT UNIQUE,
                        review_id TEXT,
                        start_time TEXT,
                        end_time TEXT,
                        duration_ms INTEGER,
                        backend_alias TEXT,
                        backend_type TEXT,
                        provider_alias TEXT,
                        requested_model TEXT,
                        reported_model TEXT,
                        invocation_mode TEXT,
                        session_identity TEXT,
                        completion_status TEXT
                    )
                """
                )
                # Fix: drop autoincrement primary key and use composite or separate id
                # actually sqlite does not allow multiple primary keys if one is autoincrement
                # wait, let me fix this in a separate command.
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS effect (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        effect_id TEXT UNIQUE,
                        review_id TEXT,
                        observation_time TEXT,
                        disposition TEXT,
                        details TEXT -- JSON object
                    )
                """
                )
            return conn
        except Exception as e:
            logger.error(f"Failed to ensure audit db for {repository}: {e}")
            try:
                with open("audit_diagnostics.log", "a") as f:
                    f.write(f"Failed to ensure audit db for {repository}: {e}\n")
            except Exception:
                pass
            return None

    def record_evaluation(self, record: ReviewAuditRecord, credentials: Optional[Sequence[str]] = None) -> bool:
        """Records a new evaluation or returns False if recording fails/conflicts."""
        conn = self._ensure_db(record.repository)
        if not conn:
            return False

        redacted_report = redact_sensitive_data(record.native_report, credentials) if record.native_report else None

        try:
            with conn:
                try:
                    conn.execute(
                        """
                        INSERT INTO evaluation (
                            review_id, repository, target_type, target_number, review_kind, origin,
                            process_identity, creation_time, creation_sequence, reviewed_generation,
                            policy_identity, related_issue_membership, diagnostic_execution_references,
                            lifecycle, execution_mode, native_verdict, native_report, source_review_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            record.review_id,
                            record.repository,
                            record.target_type,
                            record.target_number,
                            record.review_kind,
                            record.origin,
                            record.process_identity,
                            record.creation_time,
                            record.creation_sequence,
                            record.reviewed_generation,
                            record.policy_identity,
                            record.related_issue_membership,
                            json.dumps(record.diagnostic_execution_references) if record.diagnostic_execution_references else None,
                            record.lifecycle.value,
                            record.execution_mode.value,
                            record.native_verdict,
                            json.dumps(redacted_report) if redacted_report is not None else None,
                            record.source_review_id,
                        ),
                    )
                    return True
                except sqlite3.IntegrityError:
                    # Idempotent check
                    cursor = conn.execute("SELECT * FROM evaluation WHERE review_id = ?", (record.review_id,))
                    existing = cursor.fetchone()
                    if existing:
                        # Check for conflict: cannot overwrite a different generation or reopen a terminal result
                        if existing["reviewed_generation"] != record.reviewed_generation:
                            logger.error(f"Audit conflict: review_id {record.review_id} has generation {existing['reviewed_generation']}, got {record.reviewed_generation}")
                            return False

                        # Terminal states: FINISHED, CANCELLED
                        # If existing is terminal and new is not, conflict
                        if existing["lifecycle"] in (EvaluationLifecycle.FINISHED.value, EvaluationLifecycle.CANCELLED.value):
                            if record.lifecycle.value != existing["lifecycle"]:
                                return False
                            if record.execution_mode.value != existing["execution_mode"] and record.execution_mode != ExecutionMode.UNKNOWN:
                                return False
                            if record.native_verdict and record.native_verdict != existing["native_verdict"]:
                                return False
                            return True

                        # Update fields
                        updates = []
                        params: list[Any] = []
                        if existing["lifecycle"] != EvaluationLifecycle.FINISHED.value and record.lifecycle.value == EvaluationLifecycle.FINISHED.value:
                            updates.append("lifecycle = ?")
                            params.append(record.lifecycle.value)

                        if record.execution_mode != ExecutionMode.UNKNOWN and existing["execution_mode"] == ExecutionMode.UNKNOWN.value:
                            updates.append("execution_mode = ?")
                            params.append(record.execution_mode.value)

                        if record.native_verdict and not existing["native_verdict"]:
                            updates.append("native_verdict = ?")
                            params.append(record.native_verdict)

                        if redacted_report and not existing["native_report"]:
                            updates.append("native_report = ?")
                            params.append(json.dumps(redacted_report))

                        if updates:
                            params.append(record.review_id)
                            conn.execute(f"UPDATE evaluation SET {', '.join(updates)} WHERE review_id = ?", params)
                        return True
                    return False
        except Exception as e:
            logger.error(f"Failed to record evaluation {record.review_id}: {e}")
            try:
                with open("audit_diagnostics.log", "a") as f:
                    f.write(f"Failed to record evaluation {record.review_id}: {e}\n")
            except Exception:
                pass
            return False

    def update_evaluation(self, review_id: str, repository: str, lifecycle: EvaluationLifecycle, execution_mode: ExecutionMode, native_verdict: Optional[str] = None, native_report: Optional[Dict[str, Any]] = None, credentials: Optional[Sequence[str]] = None) -> bool:
        """Updates an existing evaluation."""
        conn = self._ensure_db(repository)
        if not conn:
            return False

        redacted_report = redact_sensitive_data(native_report, credentials) if native_report else None

        try:
            with conn:
                cursor = conn.execute("SELECT * FROM evaluation WHERE review_id = ?", (review_id,))
                existing = cursor.fetchone()
                if not existing:
                    logger.error(f"Audit update failed: review_id {review_id} not found")
                    return False

                # Terminal state check
                if existing["lifecycle"] in (EvaluationLifecycle.FINISHED.value, EvaluationLifecycle.CANCELLED.value):
                    if lifecycle.value != existing["lifecycle"]:
                        return False
                    if execution_mode.value != existing["execution_mode"] and execution_mode != ExecutionMode.UNKNOWN:
                        return False
                    if native_verdict and native_verdict != existing["native_verdict"]:
                        return False
                    return True

                updates = ["lifecycle = ?", "execution_mode = ?"]
                params: list[Any] = [lifecycle.value, execution_mode.value]

                if native_verdict:
                    updates.append("native_verdict = ?")
                    params.append(native_verdict)
                if redacted_report:
                    updates.append("native_report = ?")
                    params.append(json.dumps(redacted_report))

                params.append(review_id)
                conn.execute(f"UPDATE evaluation SET {', '.join(updates)} WHERE review_id = ?", params)
                return True
        except Exception as e:
            logger.error(f"Failed to update evaluation {review_id}: {e}")
            return False

    def record_interaction(self, repository: str, interaction: ReviewInteractionRecord, credentials: Optional[Sequence[str]] = None) -> bool:
        conn = self._ensure_db(repository)
        if not conn:
            return False

        try:
            with conn:
                try:
                    conn.execute(
                        """
                        INSERT INTO interaction (
                            interaction_id, review_id, start_time, end_time, duration_ms,
                            backend_alias, backend_type, provider_alias, requested_model,
                            reported_model, invocation_mode, session_identity, completion_status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            interaction.interaction_id,
                            interaction.review_id,
                            interaction.start_time,
                            interaction.end_time,
                            interaction.duration_ms,
                            interaction.backend_alias,
                            interaction.backend_type,
                            redact_sensitive_data(interaction.provider_alias, credentials) if interaction.provider_alias else None,
                            redact_sensitive_data(interaction.requested_model, credentials) if interaction.requested_model else None,
                            redact_sensitive_data(interaction.reported_model, credentials) if interaction.reported_model else None,
                            redact_sensitive_data(interaction.invocation_mode, credentials) if interaction.invocation_mode else None,
                            redact_sensitive_data(interaction.session_identity, credentials) if interaction.session_identity else None,
                            interaction.completion_status,
                        ),
                    )
                    return True
                except sqlite3.IntegrityError:
                    # check if end_time / status can be updated
                    cursor = conn.execute("SELECT * FROM interaction WHERE interaction_id = ?", (interaction.interaction_id,))
                    existing = cursor.fetchone()
                    if existing:
                        updates = []
                        params: list[Any] = []
                        if interaction.end_time and not existing["end_time"]:
                            updates.append("end_time = ?")
                            params.append(interaction.end_time)
                        if interaction.duration_ms is not None and existing["duration_ms"] is None:
                            updates.append("duration_ms = ?")
                            params.append(interaction.duration_ms)
                        if interaction.completion_status != "unrecorded" and existing["completion_status"] == "unrecorded":
                            updates.append("completion_status = ?")
                            params.append(interaction.completion_status)

                        if updates:
                            params.append(interaction.interaction_id)
                            conn.execute(f"UPDATE interaction SET {', '.join(updates)} WHERE interaction_id = ?", params)
                        return True
                    return False
        except Exception as e:
            logger.error(f"Failed to record interaction {interaction.interaction_id}: {e}")
            return False

    def record_effect(self, repository: str, effect: ReviewEffectRecord, credentials: Optional[Sequence[str]] = None) -> bool:
        conn = self._ensure_db(repository)
        if not conn:
            return False

        redacted_details = redact_sensitive_data(effect.details, credentials) if effect.details else None

        try:
            with conn:
                try:
                    conn.execute(
                        """
                        INSERT INTO effect (
                            effect_id, review_id, observation_time, disposition, details
                        ) VALUES (?, ?, ?, ?, ?)
                    """,
                        (effect.effect_id, effect.review_id, effect.observation_time, effect.disposition, json.dumps(redacted_details) if redacted_details is not None else None),
                    )
                    return True
                except sqlite3.IntegrityError:
                    return True  # already recorded
        except Exception as e:
            logger.error(f"Failed to record effect {effect.effect_id}: {e}")
            return False

    def get_evaluation(self, repository: str, review_id: str) -> AuditSingleReadResult:
        """Gets a single evaluation and its interactions/effects."""
        conn, health = self._connect_readonly(repository)
        if not conn:
            return AuditSingleReadResult(health=health, record=None)

        try:
            with conn:
                cursor = conn.execute("SELECT * FROM evaluation WHERE review_id = ?", (review_id,))
                row = cursor.fetchone()
                if not row:
                    return AuditSingleReadResult(health=StorageHealth.AVAILABLE, record=None)

                eval_record = self._row_to_evaluation(row)

                # Fetch interactions
                cursor = conn.execute("SELECT * FROM interaction WHERE review_id = ? ORDER BY seq ASC", (review_id,))
                for ir in cursor.fetchall():
                    eval_record.interactions.append(self._row_to_interaction(ir))

                # Fetch effects
                cursor = conn.execute("SELECT * FROM effect WHERE review_id = ? ORDER BY seq ASC", (review_id,))
                for er in cursor.fetchall():
                    eval_record.effects.append(self._row_to_effect(er))

                return AuditSingleReadResult(health=StorageHealth.AVAILABLE, record=eval_record)
        except Exception as e:
            logger.error(f"Failed to get evaluation {review_id}: {e}")
            return AuditSingleReadResult(health=StorageHealth.UNAVAILABLE, record=None)

    def get_recent_history(self, repository: str, limit: int = 50, high_water_mark_seq: Optional[int] = None, before_seq: Optional[int] = None) -> AuditReadResult:
        """Gets recent evaluations. Page sizes strictly 1-200."""
        if not 1 <= limit <= 200:
            logger.error("Page size must be between 1 and 200")
            return AuditReadResult(health=StorageHealth.UNAVAILABLE, records=[])

        conn, health = self._connect_readonly(repository)
        if not conn:
            return AuditReadResult(health=health, records=[])

        try:
            with conn:
                query = "SELECT * FROM evaluation"
                params: list[Any] = []
                conditions = []

                if high_water_mark_seq is not None:
                    conditions.append("creation_sequence <= ?")
                    params.append(high_water_mark_seq)

                if before_seq is not None:
                    conditions.append("creation_sequence < ?")
                    params.append(before_seq)

                if conditions:
                    query += " WHERE " + " AND ".join(conditions)

                query += " ORDER BY creation_sequence DESC LIMIT ?"
                params.append(limit)

                cursor = conn.execute(query, params)

                records = []
                for row in cursor.fetchall():
                    rec = self._row_to_evaluation(row)

                    # fetch related
                    ic = conn.execute("SELECT * FROM interaction WHERE review_id = ? ORDER BY seq ASC", (rec.review_id,))
                    rec.interactions = [self._row_to_interaction(r) for r in ic.fetchall()]

                    ec = conn.execute("SELECT * FROM effect WHERE review_id = ? ORDER BY seq ASC", (rec.review_id,))
                    rec.effects = [self._row_to_effect(r) for r in ec.fetchall()]

                    records.append(rec)
                return AuditReadResult(health=StorageHealth.AVAILABLE, records=records)
        except Exception as e:
            logger.error(f"Failed to get recent history: {e}")
            return AuditReadResult(health=StorageHealth.UNAVAILABLE, records=[])

    def get_related_evaluations(self, repository: str, target_type: str, target_number: str, review_kind: Optional[str] = None) -> AuditReadResult:
        """Looks up decomposition or other related records."""
        conn, health = self._connect_readonly(repository)
        if not conn:
            return AuditReadResult(health=health, records=[])

        try:
            with conn:
                query = "SELECT * FROM evaluation WHERE target_type = ? AND target_number = ?"
                params: list[Any] = [target_type, target_number]
                if review_kind:
                    query += " AND review_kind = ?"
                    params.append(review_kind)

                query += " ORDER BY creation_sequence ASC LIMIT 500"
                cursor = conn.execute(query, params)

                records = []
                for row in cursor.fetchall():
                    rec = self._row_to_evaluation(row)
                    records.append(rec)
                return AuditReadResult(health=StorageHealth.AVAILABLE, records=records)
        except Exception as e:
            logger.error(f"Failed to get related evaluations: {e}")
            return AuditReadResult(health=StorageHealth.UNAVAILABLE, records=[])

    def _row_to_evaluation(self, row: sqlite3.Row) -> ReviewAuditRecord:
        return ReviewAuditRecord(
            review_id=row["review_id"],
            repository=row["repository"],
            target_type=row["target_type"],
            target_number=row["target_number"],
            review_kind=row["review_kind"],
            origin=row["origin"],
            process_identity=row["process_identity"],
            creation_time=row["creation_time"],
            creation_sequence=row["creation_sequence"],
            reviewed_generation=row["reviewed_generation"],
            policy_identity=row["policy_identity"],
            related_issue_membership=row["related_issue_membership"],
            diagnostic_execution_references=json.loads(row["diagnostic_execution_references"]) if row["diagnostic_execution_references"] else None,
            lifecycle=EvaluationLifecycle(row["lifecycle"]),
            execution_mode=ExecutionMode(row["execution_mode"]),
            native_verdict=row["native_verdict"],
            native_report=json.loads(row["native_report"]) if row["native_report"] else None,
            source_review_id=row["source_review_id"],
            interactions=[],
            effects=[],
        )

    def _row_to_interaction(self, row: sqlite3.Row) -> ReviewInteractionRecord:
        return ReviewInteractionRecord(
            interaction_id=row["interaction_id"],
            review_id=row["review_id"],
            start_time=row["start_time"],
            end_time=row["end_time"],
            duration_ms=row["duration_ms"],
            backend_alias=row["backend_alias"],
            backend_type=row["backend_type"],
            provider_alias=row["provider_alias"],
            requested_model=row["requested_model"],
            reported_model=row["reported_model"],
            invocation_mode=row["invocation_mode"],
            session_identity=row["session_identity"],
            completion_status=row["completion_status"],
        )

    def _row_to_effect(self, row: sqlite3.Row) -> ReviewEffectRecord:
        return ReviewEffectRecord(review_id=row["review_id"], effect_id=row["effect_id"], observation_time=row["observation_time"], disposition=row["disposition"], details=json.loads(row["details"]) if row["details"] else None)
