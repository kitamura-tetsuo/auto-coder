"""Authoritative, read-only observation of speculative Jules artifacts.

The competition ledger owns candidate authority.  This module deliberately owns
only provider/GitHub reads and their durable evidence: observing an artifact can
record membership, but can never select it or perform a lifecycle mutation.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping, Optional, Protocol, cast

from .jules_competition_ledger import (
    CandidateAuthorityState,
    CandidateBindingObservation,
    GenerationLifecycleState,
    JulesCompetitionLedger,
)


class EvidenceStatus(str, Enum):
    KNOWN = "KNOWN"
    KNOWN_EMPTY = "KNOWN_EMPTY"
    INCOMPLETE = "INCOMPLETE"
    UNAVAILABLE = "UNAVAILABLE"
    CONFLICTING = "CONFLICTING"


class ProviderState(str, Enum):
    QUEUED = "QUEUED"
    PLANNING = "PLANNING"
    IN_PROGRESS = "IN_PROGRESS"
    AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
    AWAITING_USER_FEEDBACK = "AWAITING_USER_FEEDBACK"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"
    UNKNOWN = "UNKNOWN"


class ArtifactClassification(str, Enum):
    ACTIVE_UNSELECTED = "ACTIVE_UNSELECTED"
    SELECTED = "SELECTED"
    RETIRED = "RETIRED"
    SUSPECTED = "SUSPECTED"
    LEGACY = "LEGACY"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class PullRequestIdentity:
    repository: str = ""
    number: int = 0


@dataclass(frozen=True)
class VerifiedPullRequest:
    identity: PullRequestIdentity = field(default_factory=PullRequestIdentity)
    head_repository: str = ""
    head_ref: str = ""
    head_sha: str = ""
    base_repository: str = ""
    base_ref: str = ""
    base_sha: str = ""


@dataclass(frozen=True)
class CandidateObservation:
    repository: str = ""
    issue_number: int = 0
    generation_id: str = ""
    candidate_id: str = ""
    provider_id: str = ""
    session_id: str = ""
    read_id: int = 0
    provider_state: ProviderState = ProviderState.UNKNOWN
    evidence_status: EvidenceStatus = EvidenceStatus.UNAVAILABLE
    artifacts: tuple[VerifiedPullRequest, ...] = ()
    malformed_outputs: int = 0
    diagnostics: tuple[str, ...] = ()
    published: bool = False


@dataclass(frozen=True)
class ClassificationResult:
    classification: ArtifactClassification = ArtifactClassification.SUSPECTED
    candidate_id: Optional[str] = None
    generation_id: Optional[str] = None
    mutation_allowed: bool = False
    cleanup_allowed: bool = False
    diagnostics: tuple[str, ...] = ()


_KNOWN_STATES = {state.value: state for state in ProviderState if state is not ProviderState.UNKNOWN}
_PR_URL = re.compile(r"^https://github\.com/([^/]+/[^/]+)/pull/(\d+)(?:[/?#].*)?$")


class JulesSessionReader(Protocol):
    def get_session(self, session_id: str) -> Mapping[str, object]: ...


class StrictPullRequestReader(Protocol):
    def get_pull_request_metadata_strict(self, repository: str, number: int) -> Mapping[str, object]: ...


def _mapping(value: object) -> Optional[Mapping[str, object]]:
    return value if isinstance(value, Mapping) else None


def _repo_name(value: object) -> str:
    data = _mapping(value)
    if data is None:
        return ""
    name = data.get("full_name") or data.get("fullName") or data.get("name")
    return name if isinstance(name, str) else ""


def normalize_pull_request_outputs(outputs: object) -> tuple[tuple[PullRequestIdentity, ...], int]:
    """Decode every documented PR output without flattening list entries.

    The second result is the number of malformed PR-shaped entries.  Unknown
    non-PR output keys are valid and ignored.
    """
    items: list[tuple[object, object]] = []
    malformed = 0
    if isinstance(outputs, Mapping):
        items.extend(outputs.items())
    elif isinstance(outputs, list):
        for item in outputs:
            if isinstance(item, Mapping):
                items.extend(item.items())
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                items.append((item[0], item[1]))
            else:
                malformed += 1
    else:
        return (), 1

    found: list[PullRequestIdentity] = []
    seen: set[tuple[str, int]] = set()
    for key, raw in items:
        if key not in ("pullRequest", "pull_request"):
            continue
        repository = ""
        number = 0
        if isinstance(raw, str):
            match = _PR_URL.match(raw)
            if match:
                repository, number = match.group(1), int(match.group(2))
        elif isinstance(raw, Mapping):
            raw_number = raw.get("number")
            if isinstance(raw_number, int) and not isinstance(raw_number, bool) and raw_number > 0:
                number = raw_number
            repository = _repo_name(raw.get("repository"))
            if not repository:
                for url_key in ("url", "html_url"):
                    url = raw.get(url_key)
                    match = _PR_URL.match(url) if isinstance(url, str) else None
                    if match:
                        repository = match.group(1)
                        number = number or int(match.group(2))
                        break
        if not repository or number <= 0:
            malformed += 1
            continue
        identity = (repository.lower(), number)
        if identity not in seen:
            seen.add(identity)
            found.append(PullRequestIdentity(repository=repository, number=number))
    return tuple(found), malformed


class CandidateObservationStore:
    """Small durable store for current evidence and retained membership."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS clocks (
                    scope TEXT PRIMARY KEY, next_read INTEGER NOT NULL,
                    accepted_read INTEGER NOT NULL, invalidated_read INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS observations (
                    scope TEXT PRIMARY KEY, read_id INTEGER NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS memberships (
                    repository TEXT NOT NULL, issue_number INTEGER NOT NULL,
                    generation_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
                    pr_repository TEXT NOT NULL, pr_number INTEGER NOT NULL,
                    head_repository TEXT NOT NULL, head_ref TEXT NOT NULL, head_sha TEXT NOT NULL,
                    base_repository TEXT NOT NULL, base_ref TEXT NOT NULL, base_sha TEXT NOT NULL,
                    PRIMARY KEY (repository, issue_number, generation_id, candidate_id, pr_repository, pr_number));
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @staticmethod
    def scope(repository: str, issue: int, generation: str, candidate: str) -> str:
        return f"{repository.lower()}::{issue}::{generation}::{candidate}"

    def begin_read(self, scope: str) -> int:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT next_read FROM clocks WHERE scope = ?", (scope,)).fetchone()
            read_id = (int(row[0]) if row else 0) + 1
            connection.execute(
                "INSERT INTO clocks VALUES (?, ?, 0, 0) ON CONFLICT(scope) DO UPDATE SET next_read = excluded.next_read",
                (scope, read_id),
            )
            connection.execute("COMMIT")
            return read_id

    def invalidate(self, scope: str) -> int:
        read_id = self.begin_read(scope)
        with self._lock, self._connect() as connection:
            connection.execute("UPDATE clocks SET invalidated_read = ? WHERE scope = ?", (read_id, scope))
        return read_id

    def publish(self, scope: str, observation: CandidateObservation) -> bool:
        payload = json.dumps(asdict(observation), sort_keys=True)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT accepted_read, invalidated_read FROM clocks WHERE scope = ?", (scope,)).fetchone()
            if row is None or observation.read_id <= max(int(row[0]), int(row[1])):
                connection.execute("ROLLBACK")
                return False
            connection.execute(
                "INSERT INTO observations VALUES (?, ?, ?) ON CONFLICT(scope) DO UPDATE SET read_id=excluded.read_id, payload=excluded.payload",
                (scope, observation.read_id, payload),
            )
            connection.execute("UPDATE clocks SET accepted_read = ? WHERE scope = ?", (observation.read_id, scope))
            for artifact in observation.artifacts:
                connection.execute(
                    "INSERT INTO memberships VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(repository, issue_number, generation_id, candidate_id, pr_repository, pr_number) "
                    "DO UPDATE SET head_repository=excluded.head_repository, head_ref=excluded.head_ref, "
                    "head_sha=excluded.head_sha, base_repository=excluded.base_repository, "
                    "base_ref=excluded.base_ref, base_sha=excluded.base_sha",
                    (
                        observation.repository.lower(),
                        observation.issue_number,
                        observation.generation_id,
                        observation.candidate_id,
                        artifact.identity.repository.lower(),
                        artifact.identity.number,
                        artifact.head_repository.lower(),
                        artifact.head_ref,
                        artifact.head_sha,
                        artifact.base_repository.lower(),
                        artifact.base_ref,
                        artifact.base_sha,
                    ),
                )
            connection.execute("COMMIT")
            return True

    def memberships(self, repository: str, issue_number: int) -> tuple[tuple[object, ...], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT generation_id,candidate_id,pr_repository,pr_number,head_repository,head_ref,head_sha,base_repository,base_ref,base_sha " "FROM memberships WHERE repository=? AND issue_number=?",
                (repository.lower(), issue_number),
            ).fetchall()
        return tuple(tuple(row) for row in rows)

    def current_evidence_status(self, repository: str, issue_number: int, generation_id: str, candidate_id: str) -> Optional[EvidenceStatus]:
        scope = self.scope(repository, issue_number, generation_id, candidate_id)
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM observations WHERE scope = ?", (scope,)).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(str(row[0])).get("evidence_status")
            return EvidenceStatus(value)
        except (ValueError, TypeError, json.JSONDecodeError):
            return EvidenceStatus.CONFLICTING


class JulesCandidateObservationAdapter:
    """Collect and classify candidate evidence without lifecycle side effects."""

    def __init__(
        self,
        ledger: JulesCompetitionLedger,
        store: CandidateObservationStore,
        jules_client: JulesSessionReader,
        github_client: StrictPullRequestReader,
    ):
        self.ledger = ledger
        self.store = store
        self.jules = jules_client
        self.github = github_client

    def observe_candidate(self, repository: str, issue_number: int, generation_id: str, candidate_id: str) -> CandidateObservation:
        scope = self.store.scope(repository, issue_number, generation_id, candidate_id)
        read_id = self.store.begin_read(scope)
        diagnostics: list[str] = []
        snapshot = self.ledger.get_namespace_snapshot(repository, issue_number)
        generation = snapshot.get_generation(generation_id)
        candidate = generation.get_candidate(candidate_id) if generation else None
        if (
            candidate is None
            or candidate.authority_state
            not in (
                CandidateAuthorityState.ACCEPTED,
                CandidateAuthorityState.RETIRED,
            )
            or not candidate.session_id
        ):
            result = CandidateObservation(
                repository,
                issue_number,
                generation_id,
                candidate_id,
                read_id=read_id,
                evidence_status=EvidenceStatus.UNAVAILABLE,
                diagnostics=("candidate has no authoritative provider/session binding",),
            )
            return self._publish(scope, result)

        if candidate.provider_id.lower() != "jules":
            result = CandidateObservation(
                repository,
                issue_number,
                generation_id,
                candidate_id,
                candidate.provider_id,
                candidate.session_id,
                read_id,
                evidence_status=EvidenceStatus.CONFLICTING,
                diagnostics=("candidate provider binding is not Jules",),
            )
            return self._publish(scope, result)

        try:
            session = self.jules.get_session(candidate.session_id)
        except Exception as exc:
            result = CandidateObservation(
                repository,
                issue_number,
                generation_id,
                candidate_id,
                candidate.provider_id,
                candidate.session_id,
                read_id,
                evidence_status=EvidenceStatus.UNAVAILABLE,
                diagnostics=(f"authenticated session read unavailable: {type(exc).__name__}",),
            )
            return self._publish(scope, result)

        if not isinstance(session, Mapping):
            diagnostics.append("session response is not a mapping")
        else:
            returned_name = session.get("name")
            returned_id = returned_name.rsplit("/", 1)[-1] if isinstance(returned_name, str) else ""
            source = _mapping(session.get("sourceContext"))
            source_name = source.get("source") if source else None
            if returned_id != candidate.session_id.rsplit("/", 1)[-1]:
                diagnostics.append("canonical session identity mismatch")
            if source_name != f"sources/github/{repository}":
                diagnostics.append("session source repository mismatch")
        if diagnostics:
            result = CandidateObservation(
                repository,
                issue_number,
                generation_id,
                candidate_id,
                candidate.provider_id,
                candidate.session_id,
                read_id,
                evidence_status=EvidenceStatus.CONFLICTING,
                diagnostics=tuple(diagnostics),
            )
            return self._publish(scope, result)

        assert isinstance(session, Mapping)
        raw_state = session.get("state")
        provider_state = _KNOWN_STATES.get(raw_state, ProviderState.UNKNOWN) if isinstance(raw_state, str) else ProviderState.UNKNOWN
        if provider_state is ProviderState.UNKNOWN:
            diagnostics.append("unsupported or malformed provider state")
        identities, malformed = normalize_pull_request_outputs(session.get("outputs", {}))
        verified: list[VerifiedPullRequest] = []
        for identity in identities:
            if identity.repository.lower() != repository.lower():
                diagnostics.append(f"foreign output {identity.repository}#{identity.number}")
                continue
            artifact = self._verify_pr(identity, repository, generation.source_branch if generation else "")
            if artifact is None:
                diagnostics.append(f"PR #{identity.number} failed authoritative GitHub verification")
            else:
                verified.append(artifact)
        if malformed:
            diagnostics.append(f"{malformed} malformed output entr{'y' if malformed == 1 else 'ies'}")
        status = EvidenceStatus.INCOMPLETE if malformed or diagnostics else (EvidenceStatus.KNOWN if verified else EvidenceStatus.KNOWN_EMPTY)
        result = CandidateObservation(
            repository,
            issue_number,
            generation_id,
            candidate_id,
            candidate.provider_id,
            candidate.session_id,
            read_id,
            provider_state,
            status,
            tuple(verified),
            malformed,
            tuple(diagnostics),
        )
        published = self._publish(scope, result)
        if published.published:
            for artifact in verified:
                latest = self.ledger.get_namespace_snapshot(repository, issue_number)
                operation = f"observe:{generation_id}:{candidate_id}:{read_id}:{artifact.identity.number}"
                self.ledger.record_candidate_binding(
                    repository,
                    issue_number,
                    generation_id,
                    candidate_id,
                    operation,
                    latest.epoch,
                    CandidateBindingObservation(
                        artifact.identity.repository,
                        artifact.identity.number,
                        artifact.head_sha,
                        artifact.base_sha,
                    ),
                )
        return published

    def _publish(self, scope: str, observation: CandidateObservation) -> CandidateObservation:
        published = self.store.publish(scope, observation)
        return CandidateObservation(**{**asdict(observation), "artifacts": observation.artifacts, "diagnostics": observation.diagnostics, "published": published})

    def _verify_pr(self, identity: PullRequestIdentity, repository: str, requested_base: str) -> Optional[VerifiedPullRequest]:
        try:
            raw = self.github.get_pull_request_metadata_strict(repository, identity.number)
        except Exception:
            return None
        if not isinstance(raw, Mapping) or raw.get("number") != identity.number:
            return None
        head, base = _mapping(raw.get("head")), _mapping(raw.get("base"))
        if head is None or base is None:
            return None
        head_repo, base_repo = _repo_name(head.get("repo")), _repo_name(base.get("repo"))
        head_ref, base_ref = head.get("ref"), base.get("ref")
        head_sha, base_sha = head.get("sha"), base.get("sha")
        values = (head_repo, base_repo, head_ref, base_ref, head_sha, base_sha)
        if not all(isinstance(value, str) and value for value in values):
            return None
        if base_repo.lower() != repository.lower() or base_ref != requested_base:
            return None
        return VerifiedPullRequest(
            identity,
            head_repo,
            cast(str, head_ref),
            cast(str, head_sha),
            base_repo,
            cast(str, base_ref),
            cast(str, base_sha),
        )

    def invalidate(self, repository: str, issue_number: int, generation_id: str, candidate_id: str) -> int:
        return self.store.invalidate(self.store.scope(repository, issue_number, generation_id, candidate_id))

    def classify(self, repository: str, issue_number: int, pr_repository: str, pr_number: int) -> ClassificationResult:
        """Classify only from retained verified membership and durable selection."""
        snapshot = self.ledger.get_namespace_snapshot(repository, issue_number)
        rows = [row for row in self.store.memberships(repository, issue_number) if str(row[2]).lower() == pr_repository.lower() and row[3] == pr_number]
        if not rows:
            if snapshot.generations:
                return ClassificationResult(diagnostics=("unresolved Jules origin while speculative history exists",))
            return ClassificationResult(ArtifactClassification.LEGACY, mutation_allowed=True)
        owners = {(str(row[0]), str(row[1])) for row in rows}
        writable_heads = {(str(row[4]).lower(), str(row[5])) for row in rows}
        all_rows = self.store.memberships(repository, issue_number)
        head_owners = {(str(row[0]), str(row[1])) for row in all_rows if (str(row[4]).lower(), str(row[5])) in writable_heads}
        if len(owners | head_owners) > 1:
            return ClassificationResult(ArtifactClassification.BLOCKED, diagnostics=("conflicting candidate provenance",))
        generation_id, candidate_id = next(iter(owners))
        generation = snapshot.get_generation(generation_id)
        candidate = generation.get_candidate(candidate_id) if generation else None
        if generation is None or candidate is None:
            return ClassificationResult(ArtifactClassification.BLOCKED, candidate_id, generation_id, diagnostics=("durable membership has no readable authority record",))
        if generation.lifecycle_state is GenerationLifecycleState.RETIRED or candidate.authority_state is CandidateAuthorityState.RETIRED:
            return ClassificationResult(ArtifactClassification.RETIRED, candidate_id, generation_id, False, True)
        evidence_status = self.store.current_evidence_status(repository, issue_number, generation_id, candidate_id)
        if evidence_status not in (EvidenceStatus.KNOWN, EvidenceStatus.KNOWN_EMPTY):
            status_name = evidence_status.value if evidence_status else "MISSING"
            return ClassificationResult(
                ArtifactClassification.BLOCKED,
                candidate_id,
                generation_id,
                diagnostics=(f"current authoritative evidence is {status_name}",),
            )
        if generation.winner_candidate_id == candidate_id and generation.winner_pr_repository and generation.winner_pr_repository.lower() == pr_repository.lower() and generation.winner_pr_number == pr_number:
            return ClassificationResult(ArtifactClassification.SELECTED, candidate_id, generation_id, True, False)
        return ClassificationResult(ArtifactClassification.ACTIVE_UNSELECTED, candidate_id, generation_id)
