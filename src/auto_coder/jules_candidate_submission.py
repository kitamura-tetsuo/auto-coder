"""Crash-safe submission and reconciliation for Jules competition candidates."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping, Optional

from .jules_client import JulesClient, JulesSessionOutcomeUncertainError, JulesSessionRejectedError
from .jules_competition_ledger import (
    CandidateAuthorityState,
    GenerationLifecycleState,
    JulesCompetitionLedger,
    StaleJulesCompetitionEpochError,
)


class CandidateSubmissionOutcome(str, Enum):
    ACCEPTED = "ACCEPTED"
    DEFINITELY_NOT_ACCEPTED = "DEFINITELY_NOT_ACCEPTED"
    UNKNOWN = "UNKNOWN"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class CandidateRequest:
    repository: str
    issue_number: int
    generation_id: str
    candidate_id: str
    logical_owner: str
    task_payload: str


@dataclass(frozen=True)
class CandidateSubmissionResult:
    outcome: CandidateSubmissionOutcome
    session_id: str = ""
    correlation_marker: str = ""
    reason: str = ""
    observed_session_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class _PersistedRequest:
    marker: str
    repository: str
    generation_id: str
    candidate_id: str
    owner: str
    branch: str
    payload: str
    payload_hash: str


def default_candidate_submission_db_path() -> Path:
    return Path.home() / ".auto-coder" / "jules_candidate_submissions.db"


class JulesCandidateSubmissionAdapter:
    """One-way send boundary for candidates belonging to an active generation.

    This adapter is deliberately not called by the legacy singleton Jules path.
    A later fan-out integration can invoke it only after creating a generation.
    """

    _lock = threading.RLock()

    def __init__(self, ledger: JulesCompetitionLedger, client: JulesClient, db_path: Optional[Path] = None):
        self._ledger = ledger
        self._client = client
        self._db_path = Path(db_path) if db_path else default_candidate_submission_db_path()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS candidate_requests (
                repository TEXT NOT NULL, issue_number INTEGER NOT NULL,
                generation_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
                logical_owner TEXT NOT NULL, source_branch TEXT NOT NULL,
                task_payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
                correlation_marker TEXT NOT NULL UNIQUE,
                observed_session_ids TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY(repository, generation_id, candidate_id)
            )"""
        )
        return conn

    @staticmethod
    def _fingerprint(payload: str) -> str:
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _persist_request(self, request: CandidateRequest, branch: str) -> _PersistedRequest:
        payload_hash = self._fingerprint(request.task_payload)
        marker = f"auto-coder-jules:{hashlib.sha256(f'{request.repository}:{request.generation_id}:{request.candidate_id}:{uuid.uuid4().hex}'.encode()).hexdigest()}"
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT correlation_marker, logical_owner, source_branch, task_payload, payload_hash FROM candidate_requests " "WHERE repository=? AND generation_id=? AND candidate_id=?",
                    (request.repository, request.generation_id, request.candidate_id),
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO candidate_requests(repository, issue_number, generation_id, candidate_id, logical_owner, source_branch, task_payload, payload_hash, correlation_marker) VALUES(?,?,?,?,?,?,?,?,?)",
                        (request.repository, request.issue_number, request.generation_id, request.candidate_id, request.logical_owner, branch, request.task_payload, payload_hash, marker),
                    )
                else:
                    marker, owner, saved_branch, payload, saved_hash = row
                    if (owner, saved_branch, payload, saved_hash) != (request.logical_owner, branch, request.task_payload, payload_hash):
                        raise ValueError("Candidate request conflicts with its durably captured owner or payload")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
        return _PersistedRequest(marker, request.repository, request.generation_id, request.candidate_id, request.logical_owner, branch, request.task_payload, payload_hash)

    def _retain_observed_identities(self, saved: _PersistedRequest, identities: tuple[str, ...]) -> None:
        """Retain every ambiguous identity so restart cannot hide quarantine evidence."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT observed_session_ids FROM candidate_requests WHERE repository=? AND generation_id=? AND candidate_id=?",
                    (saved.repository, saved.generation_id, saved.candidate_id),
                ).fetchone()
                previous = json.loads(row[0]) if row else []
                retained = sorted(set(str(value) for value in previous) | set(identities))
                conn.execute(
                    "UPDATE candidate_requests SET observed_session_ids=? WHERE repository=? AND generation_id=? AND candidate_id=?",
                    (json.dumps(retained), saved.repository, saved.generation_id, saved.candidate_id),
                )
            finally:
                conn.close()

    @staticmethod
    def _candidate_prompt(saved: _PersistedRequest) -> str:
        candidate_branch = f"auto-coder/{saved.generation_id}/{saved.candidate_id}"
        return (
            f"{saved.payload}\n\n"
            "Jules competition isolation (mandatory):\n"
            f"- Correlation marker: {saved.marker}\n"
            f"- Candidate identity: {saved.candidate_id}\n"
            f"- Create and use only this candidate branch: {candidate_branch}\n"
            "- Publish your own independent pull request from that candidate-specific branch.\n"
            "- Do not reuse another candidate's pull request or branch.\n"
            "- Do not merge any pull request, close the source Issue, or modify sibling artifacts."
        )

    def submit(self, request: CandidateRequest) -> CandidateSubmissionResult:
        snapshot = self._ledger.get_namespace_snapshot(request.repository, request.issue_number)
        generation = snapshot.get_generation(request.generation_id)
        if generation is None or generation.lifecycle_state != GenerationLifecycleState.ACTIVE:
            return CandidateSubmissionResult(CandidateSubmissionOutcome.BLOCKED, reason="Generation is not active")
        candidate = generation.get_candidate(request.candidate_id)
        if candidate is None:
            return CandidateSubmissionResult(CandidateSubmissionOutcome.BLOCKED, reason="Candidate does not belong to generation")
        if request.logical_owner != f"{request.repository}#{request.issue_number}":
            return CandidateSubmissionResult(CandidateSubmissionOutcome.BLOCKED, reason="Logical Issue owner does not match the admitted repository/Issue")
        saved = self._persist_request(request, generation.source_branch)
        if candidate.authority_state != CandidateAuthorityState.NEVER_SUBMITTED:
            return CandidateSubmissionResult(
                CandidateSubmissionOutcome.ACCEPTED if candidate.session_id else CandidateSubmissionOutcome.BLOCKED,
                session_id=candidate.session_id,
                correlation_marker=saved.marker,
                reason="Candidate already has a suppressing submission claim",
            )

        try:
            claimed = self._ledger.claim_candidate_submission(
                request.repository,
                request.issue_number,
                request.generation_id,
                request.candidate_id,
                f"candidate-submit-claim:{request.generation_id}:{request.candidate_id}",
                snapshot.epoch,
            )
        except StaleJulesCompetitionEpochError:
            return CandidateSubmissionResult(CandidateSubmissionOutcome.BLOCKED, correlation_marker=saved.marker, reason="Candidate claim was acquired concurrently")
        if not claimed.applied:
            return CandidateSubmissionResult(CandidateSubmissionOutcome.BLOCKED, correlation_marker=saved.marker, reason=claimed.denial_reason or "Candidate is ineligible")

        # Re-read after the durable claim so retirement/winner selection that
        # won the boundary race prevents the external send.
        current = self._ledger.get_namespace_snapshot(request.repository, request.issue_number)
        current_generation = current.get_generation(request.generation_id)
        current_candidate = current_generation.get_candidate(request.candidate_id) if current_generation else None
        if current_generation is None or not current_generation.is_active() or current_candidate is None or current_candidate.authority_state != CandidateAuthorityState.SUBMISSION_CLAIMED:
            return CandidateSubmissionResult(CandidateSubmissionOutcome.BLOCKED, correlation_marker=saved.marker, reason="Candidate retired before external send")

        try:
            session_id = self._client.start_session(
                self._candidate_prompt(saved),
                request.repository,
                saved.branch,
                title=f"Auto-Coder candidate {request.candidate_id} [{saved.marker}]",
            )
        except JulesSessionRejectedError as exc:
            latest = self._ledger.get_namespace_snapshot(request.repository, request.issue_number)
            result = self._ledger.record_candidate_not_accepted(request.repository, request.issue_number, request.generation_id, request.candidate_id, f"candidate-rejected:{request.generation_id}:{request.candidate_id}", latest.epoch, evidence=str(exc))
            return CandidateSubmissionResult(CandidateSubmissionOutcome.DEFINITELY_NOT_ACCEPTED, correlation_marker=saved.marker, reason=result.denial_reason or str(exc))
        except (JulesSessionOutcomeUncertainError, Exception) as exc:
            latest = self._ledger.get_namespace_snapshot(request.repository, request.issue_number)
            self._ledger.record_candidate_outcome_unknown(request.repository, request.issue_number, request.generation_id, request.candidate_id, f"candidate-unknown:{request.generation_id}:{request.candidate_id}", latest.epoch, evidence=str(exc))
            return CandidateSubmissionResult(CandidateSubmissionOutcome.UNKNOWN, correlation_marker=saved.marker, reason=str(exc))

        latest = self._ledger.get_namespace_snapshot(request.repository, request.issue_number)
        try:
            recorded = self._ledger.record_candidate_accepted(request.repository, request.issue_number, request.generation_id, request.candidate_id, f"candidate-accepted:{request.generation_id}:{request.candidate_id}", latest.epoch, provider_id="jules", session_id=session_id)
        except Exception as exc:
            return CandidateSubmissionResult(CandidateSubmissionOutcome.BLOCKED, correlation_marker=saved.marker, reason=f"Accepted identity could not be durably retained: {exc}")
        recorded_generation = recorded.snapshot.get_generation(request.generation_id)
        recorded_candidate = recorded_generation.get_candidate(request.candidate_id) if recorded_generation else None
        if not recorded.applied and (recorded_candidate is None or not recorded_candidate.session_id):
            return CandidateSubmissionResult(CandidateSubmissionOutcome.BLOCKED, correlation_marker=saved.marker, reason="Accepted identity could not be durably retained")
        return CandidateSubmissionResult(CandidateSubmissionOutcome.ACCEPTED, session_id=session_id, correlation_marker=saved.marker)

    @staticmethod
    def _canonical_session_id(session: Mapping[str, object]) -> Optional[str]:
        raw_id, raw_name = session.get("id"), session.get("name")
        sid = raw_id if isinstance(raw_id, str) and raw_id and "/" not in raw_id else None
        named = raw_name.removeprefix("sessions/") if isinstance(raw_name, str) and raw_name.startswith("sessions/") and "/" not in raw_name.removeprefix("sessions/") else None
        if raw_id is not None and sid is None or raw_name is not None and named is None or sid and named and sid != named:
            return None
        return sid or named

    def reconcile(self, request: CandidateRequest, sessions: Iterable[Mapping[str, object]]) -> CandidateSubmissionResult:
        snapshot = self._ledger.get_namespace_snapshot(request.repository, request.issue_number)
        generation = snapshot.get_generation(request.generation_id)
        if generation is None:
            return CandidateSubmissionResult(CandidateSubmissionOutcome.BLOCKED, reason="Generation is unknown")
        saved = self._persist_request(request, generation.source_branch)
        matches: list[str] = []
        contradictory: list[str] = []
        for session in sessions:
            sid = self._canonical_session_id(session)
            prompt = session.get("prompt")
            source = session.get("source")
            branch = session.get("startingBranch")
            source_context = session.get("sourceContext")
            if isinstance(source_context, Mapping):
                source = source_context.get("source", source)
                github_context = source_context.get("githubRepoContext")
                if isinstance(github_context, Mapping):
                    branch = github_context.get("startingBranch", branch)
            if saved.marker not in str(prompt):
                continue
            if sid is None:
                contradictory.append(str(session.get("id") or session.get("name") or "malformed"))
                continue
            if source == f"sources/github/{saved.repository}" and branch == saved.branch and self._fingerprint(str(prompt).split("\n\nJules competition isolation", 1)[0]) == saved.payload_hash:
                matches.append(sid)
        observed = tuple(sorted(set(matches + contradictory)))
        if contradictory or len(set(matches)) > 1:
            self._retain_observed_identities(saved, observed)
            return CandidateSubmissionResult(CandidateSubmissionOutcome.BLOCKED, correlation_marker=saved.marker, reason="Contradictory or multiple exact provider sessions", observed_session_ids=observed)
        if not matches:
            return CandidateSubmissionResult(CandidateSubmissionOutcome.UNKNOWN, correlation_marker=saved.marker, reason="No exact authenticated session match")
        latest = self._ledger.get_namespace_snapshot(request.repository, request.issue_number)
        try:
            recorded = self._ledger.record_candidate_accepted(request.repository, request.issue_number, request.generation_id, request.candidate_id, f"candidate-recovered:{request.generation_id}:{request.candidate_id}:{matches[0]}", latest.epoch, provider_id="jules", session_id=matches[0])
        except Exception as exc:
            self._retain_observed_identities(saved, observed)
            return CandidateSubmissionResult(CandidateSubmissionOutcome.BLOCKED, correlation_marker=saved.marker, reason=f"Recovered identity could not be durably retained: {exc}", observed_session_ids=observed)
        recorded_generation = recorded.snapshot.get_generation(request.generation_id)
        recorded_candidate = recorded_generation.get_candidate(request.candidate_id) if recorded_generation else None
        if not recorded.applied and (recorded_candidate is None or not recorded_candidate.session_id):
            return CandidateSubmissionResult(CandidateSubmissionOutcome.BLOCKED, correlation_marker=saved.marker, reason="Recovered identity could not be durably retained", observed_session_ids=observed)
        self._retain_observed_identities(saved, observed)
        return CandidateSubmissionResult(CandidateSubmissionOutcome.ACCEPTED, session_id=matches[0], correlation_marker=saved.marker, observed_session_ids=observed)
