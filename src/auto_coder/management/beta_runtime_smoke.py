"""Durable local beta smoke evidence, owned by an existing acceptance operation.

This internal consumer reads the existing Git, deployment and worker producers.
It neither admits operations nor publishes, installs, or controls a worker.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Callable

from auto_coder.build_provenance import ORIGIN_BUILD_EMBEDDED, ORIGIN_INSTALLED_DISTRIBUTION
from auto_coder.logger_config import get_logger

from .operation_store import ManagementOperationStore
from .operations import ManagedTarget, OperationPhase, OperationRecord, QueryStatus, ReadStatus
from .source_evidence import BetaEvidenceReader, SourceEvidenceError, VerifiedBetaEvidence
from .worker_network_client import WorkerNetworkClient

logger = get_logger(__name__)
ACTION = "run_beta_acceptance"
PROFILE_ID = "beta-runtime-smoke-v1"
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
MAX_SAMPLES = 128
REQUIRED_PREDICATES = (
    "startup_reconciled",
    "no_startup_error",
    "running_lifecycle",
    "daemon_serving",
    "open_invocation_gate",
    "no_lifecycle_change",
    "producer_heartbeat",
)
_STORAGE_ERRORS = (OSError, sqlite3.Error, ValueError, TypeError, KeyError)
_EVIDENCE_ERRORS = (OSError, RuntimeError, ValueError, TypeError, KeyError)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _finite(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)  # type: ignore[arg-type]


def _number(value: object) -> float:
    if not isinstance(value, (int, float)) or not _finite(value):
        raise ValueError("finite numeric evidence required")
    return float(value)


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("integer evidence required")
    return value


class LocalResult(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    SUPERSEDED = "SUPERSEDED"


class SmokeState(str, Enum):
    NOT_FOUND = "NOT_FOUND"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"
    CONFLICT = "CONFLICT"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class SmokeProfile:
    total_seconds: float = 180.0
    interval_seconds: float = 2.0
    minimum_samples: int = 2
    profile_id: str = PROFILE_ID
    required_predicates: tuple[str, ...] = REQUIRED_PREDICATES

    def document(self) -> dict[str, object]:
        if (
            self.profile_id != PROFILE_ID
            or not _finite(self.total_seconds)
            or not _finite(self.interval_seconds)
            or not 0 < self.interval_seconds < self.total_seconds <= 3600
            or type(self.minimum_samples) is not int
            or not 2 <= self.minimum_samples <= MAX_SAMPLES
            or math.ceil(self.total_seconds / self.interval_seconds) + 1 > MAX_SAMPLES
            or (self.minimum_samples - 1) * self.interval_seconds >= self.total_seconds
            or tuple(self.required_predicates) != REQUIRED_PREDICATES
        ):
            raise ValueError("invalid bounded beta runtime smoke profile")
        value = asdict(self)
        return {**value, "content_revision": _digest(value)}

    @property
    def content_revision(self) -> str:
        return str(self.document()["content_revision"])

    @classmethod
    def from_document(cls, value: dict[str, object]) -> SmokeProfile:
        profile = cls(
            total_seconds=value["total_seconds"],  # type: ignore[arg-type]
            interval_seconds=value["interval_seconds"],  # type: ignore[arg-type]
            minimum_samples=value["minimum_samples"],  # type: ignore[arg-type]
            profile_id=str(value["profile_id"]),
            required_predicates=tuple(value["required_predicates"]),  # type: ignore[arg-type]
        )
        if _canonical(profile.document()) != _canonical(value):
            raise ValueError("profile content revision differs")
        return profile


@dataclass(frozen=True)
class LocalSmokeEvidence:
    local_evidence_id: str
    content_sha256: str
    content: str

    @property
    def local_result(self) -> LocalResult:
        return LocalResult(str(self.document()["local_result"]))

    def document(self) -> dict[str, object]:
        value = json.loads(self.content)
        if not isinstance(value, dict):
            raise ValueError("invalid local evidence")
        return value


@dataclass(frozen=True)
class SmokeObservation:
    state: SmokeState
    operation_id: str
    reason: str
    evidence: LocalSmokeEvidence | None = None
    next_sample_at: float | None = None


@dataclass(frozen=True)
class _WorkerRead:
    outcome: LocalResult | None
    reason: str
    identity: dict[str, object]
    sample: dict[str, object]


class BetaRuntimeSmokeProducer:
    """One bounded polling step; the authenticated caller owns dispatch and lane.

    The supplied operation must have action ``run_beta_acceptance`` and fixed
    ``source_sha``, ``deployment_operation_id``, ``supervisor_generation`` and
    ``deployment_generation`` parameters. Its expected incarnation is the
    process_run_id; its expected artifact is the installed_runtime_id.
    """

    def __init__(self, journal: ManagementOperationStore, client: WorkerNetworkClient, source: BetaEvidenceReader, profile: SmokeProfile | None, *, clock: Callable[[], float] = time.time) -> None:
        self.journal = journal
        self.client = client
        self.source = source
        self.profile = profile
        self.clock = clock

    def _now(self) -> float | None:
        try:
            value = self.clock()
            return float(value) if _finite(value) and value > 0 else None
        except (OSError, RuntimeError, ValueError, TypeError, OverflowError):
            return None

    def _operation(self, operation_id: str) -> OperationRecord | SmokeObservation:
        result = self.journal.get(operation_id)
        if result.status is ReadStatus.UNAVAILABLE:
            return SmokeObservation(SmokeState.UNAVAILABLE, operation_id, "journal_unavailable")
        if result.status is not ReadStatus.FOUND or result.record is None:
            return SmokeObservation(SmokeState.NOT_FOUND, operation_id, "operation_not_found")
        record = result.record
        config = self.client.config
        params = {item.name: item.value for item in record.intent.parameters}
        if (
            config.channel != "beta"
            or record.intent.target != ManagedTarget(config.instance_id, config.channel, config.repository)
            or record.intent.action != ACTION
            or record.intent.fingerprint() != record.intent_fingerprint
            or not isinstance(params.get("source_sha"), str)
            or SHA.fullmatch(str(params["source_sha"])) is None
            or any(not isinstance(params.get(key), str) or not params[key] for key in ("deployment_operation_id", "supervisor_generation", "deployment_generation"))
            or not record.intent.expected_incarnation
            or record.intent.expected_incarnation == "-"
            or not record.intent.expected_artifact
            or record.intent.expected_artifact in {"-", "bootstrap"}
            or set(params) - {"source_sha", "deployment_operation_id", "supervisor_generation", "deployment_generation", "target_id"}
        ):
            return SmokeObservation(SmokeState.CONFLICT, operation_id, "acceptance_binding_invalid")
        return record

    def _binding(self, record: OperationRecord) -> dict[str, object]:
        return {
            "operation_id": record.operation_id,
            "intent_fingerprint": record.intent_fingerprint,
            "intent": asdict(record.intent),
            "accepted_authority": asdict(record.authority),
            "source_repository": self.source.repository,
        }

    @staticmethod
    def _params(record: OperationRecord) -> dict[str, str]:
        return {item.name: str(item.value) for item in record.intent.parameters}

    def read(self, operation_id: str) -> SmokeObservation:
        """Read exact immutable evidence, without clocks, workers or Git reads."""
        try:
            record = self._operation(operation_id)
            if isinstance(record, SmokeObservation):
                return record
            stored = self.journal.smoke_snapshot(operation_id)
            if stored is None:
                return SmokeObservation(SmokeState.NOT_FOUND, operation_id, "capture_not_started")
            snapshot = stored[1]
            if _canonical(snapshot.get("binding")) != _canonical(self._binding(record)):
                return SmokeObservation(SmokeState.CONFLICT, operation_id, "original_operation_binding_changed")
            result = snapshot.get("result")
            if result is None:
                samples = snapshot.get("samples")
                profile = _mapping(snapshot.get("profile"))
                last = samples[-1] if isinstance(samples, list) and samples else None
                next_sample = _number(last["sampled_at"]) + _number(profile["interval_seconds"]) if isinstance(last, dict) else None
                return SmokeObservation(SmokeState.IN_PROGRESS, operation_id, "original_window_in_progress", next_sample_at=next_sample)
            if not isinstance(result, dict):
                raise ValueError("invalid local result")
            content = result.get("content")
            identity, digest = result.get("local_evidence_id"), result.get("content_sha256")
            if not isinstance(content, str) or not isinstance(identity, str) or not isinstance(digest, str) or hashlib.sha256(content.encode()).hexdigest() != digest:
                raise ValueError("local result content identity differs")
            document = json.loads(content)
            if (
                not isinstance(document, dict)
                or document.get("schema_version") != 1
                or document.get("record_type") != "beta-runtime-smoke"
                or document.get("operation_id") != operation_id
                or document.get("local_evidence_id") != identity
                or identity != f"{PROFILE_ID}:{operation_id}"
                or document.get("binding") != snapshot["binding"]
                or any(document.get(key) != snapshot.get(key) for key in ("capture", "profile", "window", "samples", "checks"))
                or _canonical(document) != content
            ):
                raise ValueError("local evidence does not match its persisted capture")
            evidence = LocalSmokeEvidence(identity, digest, content)
            evidence.local_result
            return SmokeObservation(SmokeState.COMPLETE, operation_id, str(document["reason"]), evidence)
        except _STORAGE_ERRORS:
            return SmokeObservation(SmokeState.UNAVAILABLE, operation_id, "local_evidence_unavailable")

    def _save(self, record: OperationRecord, revision: int, snapshot: dict[str, object]) -> SmokeObservation:
        try:
            saved = self.journal.smoke_save(record.operation_id, revision, snapshot)
            if not saved:
                # A concurrent poll may have won the same CAS. Return only the
                # durable winner; never expose the uncommitted candidate.
                return self.read(record.operation_id)
            return self.read(record.operation_id)
        except _STORAGE_ERRORS:
            return SmokeObservation(SmokeState.UNAVAILABLE, record.operation_id, "local_evidence_persistence_uncertain")

    def _finish(self, record: OperationRecord, revision: int, snapshot: dict[str, object], outcome: LocalResult, reason: str, now: float | None) -> SmokeObservation:
        checks = snapshot.setdefault("checks", [])
        assert isinstance(checks, list)
        checks.append({"check": "local_result", "disposition": outcome.value, "reason": reason})
        params = self._params(record)
        payload = {
            "schema_version": 1,
            "record_type": "beta-runtime-smoke",
            "local_evidence_id": f"{PROFILE_ID}:{record.operation_id}",
            "operation_id": record.operation_id,
            "repository": self.source.repository,
            "source_sha": params["source_sha"],
            "instance_id": record.intent.target.instance_id,
            "channel": record.intent.target.channel,
            "worker_repository": record.intent.target.repository,
            "binding": snapshot["binding"],
            "capture": snapshot["capture"],
            "profile": snapshot["profile"],
            "window": snapshot["window"],
            "samples": snapshot["samples"],
            "checks": checks,
            "local_result": outcome.value,
            "reason": reason,
            "completed_at": now,
        }
        content = _canonical(payload)
        snapshot["result"] = {"local_evidence_id": payload["local_evidence_id"], "content_sha256": hashlib.sha256(content.encode()).hexdigest(), "content": content}
        result = self._save(record, revision, snapshot)
        if result.state is SmokeState.COMPLETE and result.evidence is not None:
            logger.info("Local beta smoke operation={} result={} evidence={}", record.operation_id, result.evidence.local_result.value, result.evidence.local_evidence_id)
        return result

    def _unsettled(self, record: OperationRecord) -> str | None:
        cursor = 0
        while True:
            page = self.journal.list_operations(cursor=cursor, limit=100, outstanding_only=True, target=record.intent.target)
            if page.status is not QueryStatus.OK:
                return "lifecycle_journal_unavailable"
            if any(item.operation_id != record.operation_id for item in page.records):
                return "unresolved_target_operation"
            if page.next_cursor is None:
                return None
            cursor = page.next_cursor

    def _deployment(self, record: OperationRecord, evidence: VerifiedBetaEvidence) -> dict[str, object]:
        params = self._params(record)
        found = self.journal.get(params["deployment_operation_id"])
        deployed = found.record
        persisted = self.journal.lifecycle_snapshot(params["deployment_operation_id"])
        if found.status is not ReadStatus.FOUND or deployed is None or persisted is None:
            raise SourceEvidenceError("deployment_evidence_unavailable")
        snapshot = persisted[1]
        staged, worker = _mapping(snapshot.get("staged")), _mapping(snapshot.get("new_worker"))
        incumbent = _mapping(snapshot.get("incumbent"))
        if (
            deployed.phase is not OperationPhase.SUCCEEDED
            or deployed.intent.action != "deploy_beta_source"
            or deployed.intent.target != record.intent.target
            or snapshot.get("stage") != "SUCCEEDED"
            or snapshot.get("source_sha") != params["source_sha"]
            or self._params(deployed).get("source_sha") != params["source_sha"]
            or snapshot.get("accepted_evidence") != evidence.canonical()
            or any(staged.get(key) != getattr(evidence, key) for key in ("source_sha", "distribution_version", "lock_sha256"))
            or staged.get("runtime_profile") != "python-3.12"
            or staged.get("installed_runtime_id") != record.intent.expected_artifact
            or worker.get("runtime_id") != record.intent.expected_artifact
            or worker.get("process_run_id") != record.intent.expected_incarnation
            or worker.get("deployment_generation") != params["deployment_generation"]
            or incumbent.get("supervisor_generation") != params["supervisor_generation"]
            or not isinstance(staged.get("content_identity"), str)
            or DIGEST.fullmatch(str(staged["content_identity"])) is None
        ):
            raise SourceEvidenceError("deployment_selection_or_candidate_mismatch")
        return {
            "operation_id": deployed.operation_id,
            "intent_fingerprint": deployed.intent_fingerprint,
            "operation_revision": deployed.revision,
            "snapshot_revision": persisted[0],
            "snapshot_content_sha256": _digest(snapshot),
            "phase": deployed.phase.value,
            "stage": snapshot["stage"],
            "accepted_evidence": json.loads(str(snapshot["accepted_evidence"])),
            "staged": staged,
            "new_worker": worker,
            "supervisor_generation": incumbent["supervisor_generation"],
        }

    def _worker(self, record: OperationRecord, captured: dict[str, object] | None) -> _WorkerRead:
        params = self._params(record)
        response = self.client.inspect_supervisor()
        supervisor = response.data
        sample: dict[str, object] = {"supervisor_request_id": response.request_id, "checks": []}
        checks = sample["checks"]
        assert isinstance(checks, list)

        def stop(outcome: LocalResult, reason: str) -> _WorkerRead:
            checks.append({"check": reason, "disposition": outcome.value})
            return _WorkerRead(outcome, reason, {}, sample)

        if response.result_code != "OK" or not isinstance(supervisor, dict) or supervisor.get("supervisor_incarnation") != response.supervisor_generation:
            return stop(LocalResult.BLOCKED, "supervisor_observation_unavailable")
        sample["supervisor"] = {key: supervisor.get(key) for key in ("supervisor_incarnation", "status", "selected_runtime_id", "alive")}
        if supervisor.get("status") not in {"RUNNING", "DRAINING", "STOPPED", "UNKNOWN"}:
            # A Supervisor journal error projects alive=False alongside
            # NEEDS_ATTENTION; that is not an OS-confirmed worker failure.
            return stop(LocalResult.BLOCKED, "supervisor_lifecycle_unavailable")
        worker = _mapping(supervisor.get("worker"))
        expected = {
            "supervisor_generation": params["supervisor_generation"],
            "process_run_id": record.intent.expected_incarnation,
            "deployment_generation": params["deployment_generation"],
            "runtime_id": record.intent.expected_artifact,
        }
        actual = {"supervisor_generation": response.supervisor_generation, **{key: worker.get(key) for key in ("process_run_id", "deployment_generation", "runtime_id")}}
        sample["observed_identity"] = actual
        if any(actual[key] is not None and actual[key] != value for key, value in expected.items()) or (supervisor.get("selected_runtime_id") is not None and supervisor["selected_runtime_id"] != record.intent.expected_artifact):
            sample["observed_identity"] = actual
            return stop(LocalResult.SUPERSEDED if captured is not None else LocalResult.BLOCKED, "worker_or_runtime_replaced")
        if supervisor.get("alive") is False or supervisor.get("status") in {"STOPPED", "DRAINING"} or worker.get("stop_requested") is True:
            return stop(LocalResult.FAIL, "worker_lifecycle_not_running")
        if supervisor.get("alive") is not True or supervisor.get("status") != "RUNNING" or any(actual[key] is None for key in expected):
            return stop(LocalResult.BLOCKED, "worker_lifecycle_unavailable")
        nonce = uuid.uuid4().hex
        result = self.client.observe_worker(nonce)
        data = result.data
        observed = _mapping(data.get("observation")) if isinstance(data, dict) else {}
        if result.result_code != "OK" or not isinstance(data, dict) or result.supervisor_generation != response.supervisor_generation:
            return stop(LocalResult.BLOCKED, "worker_observation_unavailable")
        if observed.get("nonce") != nonce or observed.get("repository") != record.intent.target.repository or observed.get("channel") != "beta" or observed.get("process_run_id") != actual["process_run_id"] or observed.get("deployment_generation") != actual["deployment_generation"]:
            return stop(LocalResult.BLOCKED, "mixed_or_stale_worker_observation")
        # Reuse #2603's exact READY/launch/UDS join; do not infer a lock from SHA.
        from .mcp_runtime_tools import ManagementRuntime

        status, binding = ManagementRuntime._runtime_binding(data, observed, supervisor)
        artifact = _mapping(observed.get("artifact"))
        source, version = _mapping(artifact.get("source_revision")), _mapping(artifact.get("distribution_version"))
        sample.update(nonce=nonce, worker_request_id=result.request_id, observation_time=observed.get("observation_time"), heartbeat=observed.get("heartbeat"), artifact={"source_revision": source, "distribution_version": version}, runtime_binding=binding)
        if status != "VERIFIED" or binding is None or source.get("origin") != ORIGIN_BUILD_EMBEDDED or version.get("origin") != ORIGIN_INSTALLED_DISTRIBUTION:
            return stop(LocalResult.BLOCKED, "installed_runtime_provenance_unavailable_or_mismatched")
        identity = {**actual, "installed_runtime_id": binding["runtime_id"], "ready_content_identity": binding["content_identity"], "lock_sha256": binding["lock_sha256"], "source_revision": source, "distribution_version": version}
        if captured is not None and _canonical(captured) != _canonical(identity):
            sample["observed_identity"] = identity
            return stop(LocalResult.SUPERSEDED, "installed_runtime_binding_replaced")
        lifecycle, invocations, heartbeat = (_mapping(observed.get(key)) for key in ("lifecycle", "invocations", "heartbeat"))
        sample["lifecycle"] = {key: lifecycle.get(key) for key in ("state", "startup_reconciled", "startup_reconciliation_error", "daemon_serving")}
        sample["invocations"] = {key: invocations.get(key) for key in ("state", "unsettled_count")}
        predicates: tuple[tuple[str, object, object, bool], ...] = (
            ("startup_reconciled", lifecycle.get("startup_reconciled"), True, "startup_reconciled" in lifecycle),
            ("no_startup_error", lifecycle.get("startup_reconciliation_error"), None, "startup_reconciliation_error" in lifecycle),
            ("running_lifecycle", lifecycle.get("state"), "running", "state" in lifecycle),
            ("daemon_serving", lifecycle.get("daemon_serving"), True, "daemon_serving" in lifecycle),
            ("open_invocation_gate", invocations.get("state"), "running", "state" in invocations),
            ("no_lifecycle_change", worker.get("stop_requested", False), False, supervisor.get("status") == "RUNNING"),
        )
        failed = missing = False
        for name, value, expected_value, present in predicates:
            available = present and (type(value) is bool if isinstance(expected_value, bool) else isinstance(value, str) if isinstance(expected_value, str) else value is None or isinstance(value, str))
            disposition = LocalResult.BLOCKED if not available else LocalResult.PASS if value == expected_value else LocalResult.FAIL
            checks.append({"check": name, "disposition": disposition.value})
            failed |= disposition is LocalResult.FAIL
            missing |= disposition is LocalResult.BLOCKED
        if failed:
            return _WorkerRead(LocalResult.FAIL, "required_worker_predicate_violated", identity, sample)
        if missing:
            return _WorkerRead(LocalResult.BLOCKED, "required_worker_predicate_unavailable", identity, sample)
        sequence, heartbeat_stage = heartbeat.get("sequence"), heartbeat.get("stage")
        if heartbeat.get("available") is not True or type(sequence) is not int or sequence <= 0 or not isinstance(heartbeat_stage, str) or not heartbeat_stage.startswith("producer:"):
            return _WorkerRead(LocalResult.BLOCKED, "producer_heartbeat_unavailable", identity, sample)
        checks.append({"check": "producer_heartbeat", "disposition": "PASS"})
        return _WorkerRead(None, "qualifying_worker_observation", identity, sample)

    def advance(self, operation_id: str) -> SmokeObservation:
        """Persist at most one genuine sample in the original, unextended window."""
        try:
            record = self._operation(operation_id)
            if isinstance(record, SmokeObservation):
                return record
            prior = self.read(operation_id)
            if prior.state in {SmokeState.COMPLETE, SmokeState.CONFLICT, SmokeState.UNAVAILABLE}:
                return prior
            stored = self.journal.smoke_snapshot(operation_id)
            if stored is None:
                if record.phase not in {OperationPhase.ACCEPTED, OperationPhase.RUNNING, OperationPhase.RECONCILING}:
                    return SmokeObservation(SmokeState.CONFLICT, operation_id, "acceptance_operation_not_active")
                now = self._now()
                try:
                    profile = self.profile.document() if self.profile is not None else None
                except (ValueError, TypeError, OverflowError):
                    profile = None
                deadline = now + _number(profile["total_seconds"]) if now is not None and profile is not None else None
                snapshot: dict[str, object] = {"schema_version": 1, "binding": self._binding(record), "profile": profile, "window": {"opened_at": now, "deadline": deadline}, "capture": None, "samples": [], "checks": [], "result": None}
                if not self.journal.smoke_save(operation_id, -1, snapshot):
                    return self.read(operation_id)
                stored = self.journal.smoke_snapshot(operation_id)
                if stored is None:
                    return SmokeObservation(SmokeState.UNAVAILABLE, operation_id, "capture_persistence_uncertain")
            revision, snapshot = stored
            if snapshot.get("result") is not None:
                return self.read(operation_id)
            now = self._now()
            if record.phase not in {OperationPhase.ACCEPTED, OperationPhase.RUNNING, OperationPhase.RECONCILING}:
                return self._finish(record, revision, snapshot, LocalResult.BLOCKED, "acceptance_operation_not_active", now)
            profile_data, window = _mapping(snapshot.get("profile")), _mapping(snapshot.get("window"))
            if not profile_data:
                return self._finish(record, revision, snapshot, LocalResult.BLOCKED, "invalid_or_missing_profile", now)
            profile_value = SmokeProfile.from_document(profile_data)
            if now is None or not _finite(window.get("opened_at")) or not _finite(window.get("deadline")):
                return self._finish(record, revision, snapshot, LocalResult.BLOCKED, "clock_unavailable", now)
            samples = snapshot["samples"]
            assert isinstance(samples, list)
            latest = _number(samples[-1]["sampled_at"]) if samples else _number(window["opened_at"])
            if now < latest:
                return self._finish(record, revision, snapshot, LocalResult.BLOCKED, "clock_moved_backwards", now)
            if now >= _number(window["deadline"]):
                return self._finish(record, revision, snapshot, LocalResult.BLOCKED, "original_deadline_without_progress", now)
            if samples and now < latest + profile_value.interval_seconds:
                return self.read(operation_id)
            unsettled = self._unsettled(record)
            if unsettled is not None:
                return self._finish(record, revision, snapshot, LocalResult.BLOCKED, unsettled, now)
            capture = _mapping(snapshot.get("capture"))
            try:
                evidence = self.source.read_current()
                if capture and _canonical(capture["source"]) != evidence.canonical():
                    return self._finish(record, revision, snapshot, LocalResult.SUPERSEDED, "confirmed_beta_selection_replaced", now)
                if not capture:
                    snapshot["capture"] = {"source": json.loads(evidence.canonical()), "selection_record_ref": f"refs/tags/auto-coder-beta-selection-{evidence.selection_operation_id}", "deployment": None, "worker": None, "captured_at": now}
                if evidence.source_sha != self._params(record)["source_sha"]:
                    return self._finish(record, revision, snapshot, LocalResult.BLOCKED, "requested_beta_source_not_current", now)
                deployment = self._deployment(record, evidence)
                if not capture:
                    _mapping(snapshot["capture"])["deployment"] = deployment
            except _EVIDENCE_ERRORS as error:
                reason = str(error) if isinstance(error, SourceEvidenceError) and str(error) in {"deployment_evidence_unavailable", "deployment_selection_or_candidate_mismatch"} else "source_or_deployment_evidence_unavailable"
                return self._finish(record, revision, snapshot, LocalResult.BLOCKED, reason, self._now())
            if capture and _canonical(capture["deployment"]) != _canonical(deployment):
                return self._finish(record, revision, snapshot, LocalResult.BLOCKED, "original_deployment_evidence_changed", self._now())
            try:
                worker_read = self._worker(record, _mapping(capture["worker"]) if capture else None)
            except _EVIDENCE_ERRORS:
                return self._finish(record, revision, snapshot, LocalResult.BLOCKED, "worker_evidence_unavailable", self._now())
            sampled_at = self._now()
            sample = worker_read.sample
            sample["sampled_at"] = sampled_at
            sample["qualifying"] = False
            if sampled_at is None or sampled_at < now:
                return self._finish(record, revision, snapshot, LocalResult.BLOCKED, "clock_unavailable_or_regressed", sampled_at)
            # Keep the attempted read, including explicit failures, as evidence.
            samples.append(sample)
            if worker_read.outcome is not None:
                if not capture:
                    _mapping(snapshot["capture"]).update(worker=worker_read.identity or None, captured_at=sampled_at)
                return self._finish(record, revision, snapshot, worker_read.outcome, worker_read.reason, sampled_at)
            identity = worker_read.identity
            if not capture:
                _mapping(snapshot["capture"]).update(worker=identity, captured_at=sampled_at)
            staged = _mapping(deployment["staged"])
            if _mapping(identity["source_revision"]).get("value") != evidence.source_sha or _mapping(identity["distribution_version"]).get("value") != evidence.distribution_version or identity["lock_sha256"] != evidence.lock_sha256 or identity["ready_content_identity"] != staged["content_identity"]:
                return self._finish(record, revision, snapshot, LocalResult.BLOCKED, "installed_candidate_or_lock_mismatch", sampled_at)
            try:
                confirmed = self.source.read_current()
            except _EVIDENCE_ERRORS:
                return self._finish(record, revision, snapshot, LocalResult.BLOCKED, "selection_revalidation_unavailable", self._now())
            if confirmed.canonical() != evidence.canonical():
                return self._finish(record, revision, snapshot, LocalResult.SUPERSEDED, "confirmed_beta_selection_replaced", self._now())
            finished_at = self._now()
            if finished_at is None or finished_at < sampled_at:
                return self._finish(record, revision, snapshot, LocalResult.BLOCKED, "clock_unavailable_or_regressed", finished_at)
            if finished_at >= _number(window["deadline"]):
                return self._finish(record, revision, snapshot, LocalResult.BLOCKED, "original_deadline_without_progress", finished_at)
            heartbeat = _mapping(sample.get("heartbeat"))
            previous = [item for item in samples[:-1] if item.get("qualifying") is True]
            advancing = not previous or _integer(heartbeat["sequence"]) > _integer(_mapping(previous[-1]["heartbeat"])["sequence"])
            sample["qualifying"] = advancing
            sample_checks = sample["checks"]
            assert isinstance(sample_checks, list)
            sample_checks.append({"check": "heartbeat_progress", "disposition": "PASS" if advancing else "WAITING"})
            if len(previous) + int(advancing) >= profile_value.minimum_samples:
                return self._finish(record, revision, snapshot, LocalResult.PASS, "same_runtime_producer_heartbeats_advanced", finished_at)
            return self._save(record, revision, snapshot)
        except _STORAGE_ERRORS:
            return SmokeObservation(SmokeState.UNAVAILABLE, operation_id, "local_evidence_persistence_uncertain")
