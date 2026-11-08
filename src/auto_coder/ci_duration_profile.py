"""Immutable, diagnostic-only duration profiles from native pytest timing JSONL."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

PROFILE_SCHEMA = "ci-duration-profile-v1"
BATCH_SCHEMA = "ci-duration-batch-v1"
CONTEXT_SCHEMA = "ci-duration-context-v1"
RETENTION_DAYS = 14
MAX_SAMPLES = 20
MIN_ESTIMATE_SECONDS = 0.001
PHASES = ("setup", "call", "teardown")
OUTCOMES = ("passed", "failed", "skipped")


class ProfileInputError(ValueError):
    """An input is unusable, without authorizing a change to pytest results."""


def _pairs_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProfileInputError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ProfileInputError(f"non-finite JSON constant: {value}")


def _decode(content: str, source: str) -> dict[str, object]:
    try:
        value: object = json.loads(content, object_pairs_hook=_pairs_no_duplicates, parse_constant=_reject_constant)
    except (ValueError, TypeError) as error:
        raise ProfileInputError(f"{source}: invalid JSON: {error}") from error
    return _object(value, source)


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ProfileInputError(f"{field} must be a JSON object")
    return cast(dict[str, object], value)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileInputError(f"{field} must be a nonempty string")
    return value


def _optional_text(value: object, field: str) -> str | None:
    return None if value is None else _text(value, field)


def _optional_string(value: object, field: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ProfileInputError(f"{field} must be a string or null")


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ProfileInputError(f"{field} must be a finite nonnegative number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as error:
        raise ProfileInputError(f"{field} must be a finite nonnegative number") from error
    if not math.isfinite(number) or number < 0:
        raise ProfileInputError(f"{field} must be a finite nonnegative number")
    return number


def _timestamp(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProfileInputError(f"{field} must be an ISO-8601 timestamp") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise ProfileInputError(f"{field} must include a UTC offset")
    return result.astimezone(timezone.utc)


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _digest(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


@dataclass(frozen=True)
class ProfileIdentity:
    repository: str
    profile_id: str
    configuration_digest: str
    python_major_minor: str

    @classmethod
    def parse(cls, value: object) -> ProfileIdentity:
        data = _object(value, "profile")
        repository = _text(data.get("repository"), "profile.repository")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ProfileInputError("profile.repository must use owner/repository syntax")
        configuration_digest = _text(data.get("configuration_digest"), "profile.configuration_digest")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", configuration_digest):
            raise ProfileInputError("profile.configuration_digest must be sha256:<64 lowercase hex characters>")
        python = _text(data.get("python_major_minor"), "profile.python_major_minor")
        if not re.fullmatch(r"[0-9]+\.[0-9]+", python):
            raise ProfileInputError("profile.python_major_minor must be major.minor")
        return cls(repository, _text(data.get("profile_id"), "profile.profile_id"), configuration_digest, python)

    def json(self) -> dict[str, object]:
        return {"repository": self.repository, "profile_id": self.profile_id, "configuration_digest": self.configuration_digest, "python_major_minor": self.python_major_minor}


@dataclass(frozen=True)
class ProfileContext:
    profile: ProfileIdentity
    evaluation_time: datetime

    @classmethod
    def read(cls, path: Path) -> ProfileContext:
        data = _read_json(path)
        if data.get("schema_version") != CONTEXT_SCHEMA:
            raise ProfileInputError(f"{path}: expected {CONTEXT_SCHEMA}")
        return cls(ProfileIdentity.parse(data.get("profile")), _timestamp(data.get("evaluation_time_utc"), "evaluation_time_utc"))


@dataclass(frozen=True)
class BatchOrigin:
    kind: str
    checkout_sha: str
    event_sha: str | None
    workflow: str | None
    run_id: str | None
    run_attempt: str | None
    job_key: str | None
    invocation_id: str

    @classmethod
    def parse(cls, value: object) -> BatchOrigin:
        data = _object(value, "origin")
        kind = _text(data.get("kind"), "origin.kind")
        if kind not in ("local", "github-actions"):
            raise ProfileInputError("origin.kind must be local or github-actions")
        checkout = _text(data.get("checkout_sha"), "origin.checkout_sha")
        if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", checkout):
            raise ProfileInputError("origin.checkout_sha must be the actual Git checkout hash")
        fields = {key: _optional_text(data.get(key), f"origin.{key}") for key in ("event_sha", "workflow", "run_id", "run_attempt", "job_key")}
        action_fields = (fields["workflow"], fields["run_id"], fields["run_attempt"], fields["job_key"])
        if kind == "local" and (any(value is not None for value in action_fields) or fields["event_sha"] is not None):
            raise ProfileInputError("local origin must not invent GitHub Actions provenance")
        if kind == "github-actions" and any(value is None for value in action_fields):
            raise ProfileInputError("github-actions origin needs workflow, run_id, run_attempt and job_key")
        return cls(kind, checkout, fields["event_sha"], fields["workflow"], fields["run_id"], fields["run_attempt"], fields["job_key"], _text(data.get("invocation_id"), "origin.invocation_id"))

    def json(self) -> dict[str, object]:
        return {"kind": self.kind, "checkout_sha": self.checkout_sha, "event_sha": self.event_sha, "workflow": self.workflow, "run_id": self.run_id, "run_attempt": self.run_attempt, "job_key": self.job_key, "invocation_id": self.invocation_id}


@dataclass(frozen=True)
class ExecutionOrigin:
    batch: BatchOrigin
    process_id: str
    pid: int
    python_version: str

    def json(self) -> dict[str, object]:
        return {**self.batch.json(), "process_id": self.process_id, "pid": self.pid, "python_version": self.python_version}


@dataclass(frozen=True)
class PhaseEvent:
    kind: str
    origin: ExecutionOrigin
    nodeid: str
    phase: str
    span_id: str
    started_at: datetime | None
    native_ms: float | None
    outcome: str | None
    wasxfail: str | None


@dataclass(frozen=True)
class PhasePair:
    start: PhaseEvent
    report: PhaseEvent


@dataclass(frozen=True)
class DurationSample:
    sample_id: str
    nodeid: str
    observed_at: datetime
    origin: ExecutionOrigin
    phase_span_ids: dict[str, str]
    phase_started_at: dict[str, str]
    native_phase_ms: dict[str, float]
    phase_outcomes: dict[str, dict[str, object]]

    @classmethod
    def from_pairs(cls, profile: ProfileIdentity, pairs: list[PhasePair]) -> DurationSample:
        first = pairs[0].start
        assert first.started_at is not None
        spans = {pair.start.phase: pair.start.span_id for pair in pairs}
        started = {pair.start.phase: _utc(cast(datetime, pair.start.started_at)) for pair in pairs}
        durations = {pair.start.phase: cast(float, pair.report.native_ms) for pair in pairs}
        outcomes: dict[str, dict[str, object]] = {pair.start.phase: {"outcome": pair.report.outcome, "wasxfail": pair.report.wasxfail} for pair in pairs}
        sample_id = _digest({"profile": profile.json(), "origin": first.origin.json(), "nodeid": first.nodeid, "phase_span_ids": spans})
        return cls(sample_id, first.nodeid, first.started_at, first.origin, spans, started, durations, outcomes)

    @property
    def raw_seconds(self) -> float:
        return math.fsum(self.native_phase_ms[phase] / 1000 for phase in PHASES)

    @property
    def duration_seconds(self) -> float:
        return max(MIN_ESTIMATE_SECONDS, self.raw_seconds)

    def json(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "nodeid": self.nodeid,
            "observed_at": _utc(self.observed_at),
            "origin": self.origin.json(),
            "phase_span_ids": self.phase_span_ids,
            "phase_started_at": self.phase_started_at,
            "native_phase_ms": self.native_phase_ms,
            "phase_outcomes": self.phase_outcomes,
            "raw_duration_seconds": self.raw_seconds,
            "duration_seconds": self.duration_seconds,
        }


@dataclass(frozen=True)
class InputDiagnostic:
    kind: str
    source: str
    status: str
    samples: int
    reasons: tuple[str, ...] = ()

    def json(self) -> dict[str, object]:
        return {"kind": self.kind, "source": self.source, "status": self.status, "samples": self.samples, "reasons": list(self.reasons)}


@dataclass(frozen=True)
class ProfileResult:
    payload: dict[str, object]
    diagnostics: tuple[InputDiagnostic, ...]
    native_input_paths: tuple[Path, ...] = ()


def _read_json(path: Path) -> dict[str, object]:
    try:
        return _decode(path.read_text(encoding="utf-8"), str(path))
    except (OSError, UnicodeError) as error:
        raise ProfileInputError(f"{path}: {type(error).__name__}: {error}") from error


def _native_event(data: dict[str, object], origin: BatchOrigin, profile: ProfileIdentity) -> PhaseEvent:
    if data.get("schema_version") != 1:
        raise ProfileInputError("unsupported native timing schema_version")
    context = _object(data.get("context"), "native context")
    for key in ("checkout_sha", "event_sha", "workflow", "run_id", "run_attempt", "job_key", "invocation_id"):
        if context.get(key) != getattr(origin, key):
            raise ProfileInputError(f"native {key} does not match declared batch origin")
    process_id = _text(context.get("process_id"), "context.process_id")
    pid = context.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        raise ProfileInputError("native context.pid must be a positive integer")
    version = _text(context.get("python_version"), "context.python_version")
    if not re.fullmatch(re.escape(profile.python_major_minor) + r"\.[0-9]+(?:[a-zA-Z0-9.-]*)?", version):
        raise ProfileInputError("native Python major/minor differs from the profile")
    kind = _text(data.get("event_kind"), "event_kind")
    native_origin = ExecutionOrigin(origin, process_id, pid, version)
    if kind not in ("pytest_phase_start", "pytest_phase_report"):
        return PhaseEvent(kind, native_origin, "", "", "", None, None, None, None)
    phase = _text(data.get("pytest_phase"), "pytest_phase")
    if phase not in PHASES:
        raise ProfileInputError(f"unknown pytest phase: {phase}")
    span = _text(data.get("span_id"), "span_id")
    nodeid = _text(data.get("nodeid"), "nodeid")
    if kind == "pytest_phase_start":
        return PhaseEvent(kind, native_origin, nodeid, phase, span, _timestamp(data.get("started_at_utc"), "started_at_utc"), None, None, None)
    outcome = _text(data.get("outcome"), "outcome")
    if outcome not in OUTCOMES:
        raise ProfileInputError(f"unknown pytest outcome: {outcome}")
    return PhaseEvent(kind, native_origin, nodeid, phase, span, None, _number(data.get("native_duration_ms"), "native_duration_ms"), outcome, _optional_string(data.get("wasxfail"), "wasxfail"))


def _pairs_to_samples(events: dict[tuple[str, str, str], tuple[str, PhaseEvent]], conflicts: set[tuple[str, str]], profile: ProfileIdentity, reasons: list[str]) -> list[DurationSample]:
    spans: dict[tuple[str, str], dict[str, PhaseEvent]] = {}
    for (process_id, span_id, kind), (_raw, event) in events.items():
        spans.setdefault((process_id, span_id), {})[kind] = event

    # A missing or conflicting report must not erase the phase-start boundary:
    # a later execution of the same node may otherwise complete the earlier one.
    by_node: dict[tuple[str, str], list[tuple[PhaseEvent, PhasePair | None]]] = {}
    unanchored_nodes: set[tuple[str, str]] = set()
    for (process_id, span_id), parts in sorted(spans.items()):
        start, report = parts.get("pytest_phase_start"), parts.get("pytest_phase_report")
        if start is None:
            reasons.append(f"incomplete phase record for process {process_id} span {span_id}")
            if report is not None:
                unanchored_nodes.add((process_id, report.nodeid))
            continue
        pair: PhasePair | None = None
        if (process_id, span_id) in conflicts:
            reasons.append(f"conflicting native phase records for process {process_id} span {span_id}")
        elif report is None:
            reasons.append(f"incomplete phase record for process {process_id} span {span_id}")
        elif (start.nodeid, start.phase, start.origin) != (report.nodeid, report.phase, report.origin):
            reasons.append(f"conflicting phase-start/report identity for process {process_id} span {span_id}")
            unanchored_nodes.add((process_id, report.nodeid))
        else:
            pair = PhasePair(start, report)
        by_node.setdefault((process_id, start.nodeid), []).append((start, pair))

    samples: list[DurationSample] = []
    for (process_id, nodeid), observations in sorted(by_node.items()):
        observations.sort(key=lambda item: (cast(datetime, item[0].started_at), PHASES.index(item[0].phase), item[0].span_id))
        current: list[PhasePair] = []
        active = False
        invalid = False
        for start, pair in observations:
            phase = start.phase
            if phase == "setup":
                if active:
                    reasons.append(f"incomplete pytest execution: {nodeid} in process {process_id}")
                current = [pair] if pair is not None else []
                active = True
                invalid = pair is None
                continue
            if not active:
                reasons.append(f"unreached setup/call: {nodeid} {phase} in process {process_id}")
                continue
            if invalid:
                continue
            if pair is None or phase != PHASES[len(current)] or start.origin != current[0].start.origin:
                reasons.append(f"nonconsecutive or conflicting pytest phases: {nodeid} in process {process_id}")
                invalid = True
                current = []
                continue
            current.append(pair)
            if len(current) == len(PHASES):
                if (process_id, nodeid) not in unanchored_nodes:
                    samples.append(DurationSample.from_pairs(profile, current))
                else:
                    reasons.append(f"unanchored phase report makes execution ambiguous: {nodeid} in process {process_id}")
                current = []
                active = False
        if active:
            reasons.append(f"incomplete pytest execution: {nodeid} in process {process_id}")
    return samples

def _batch(path: Path, profile: ProfileIdentity) -> tuple[list[DurationSample], InputDiagnostic, tuple[Path, ...]]:
    data = _read_json(path)
    if data.get("schema_version") != BATCH_SCHEMA:
        raise ProfileInputError(f"{path}: expected {BATCH_SCHEMA}")
    if ProfileIdentity.parse(data.get("profile")) != profile:
        raise ProfileInputError(f"{path}: foreign profile identity")
    origin = BatchOrigin.parse(data.get("origin"))
    files = data.get("files")
    if not isinstance(files, list) or not files or any(not isinstance(file, str) or not file.strip() for file in files):
        raise ProfileInputError(f"{path}: files must be a nonempty array of JSONL paths")
    paths = tuple(sorted(set((path.parent / cast(str, file)).resolve() for file in files)))
    events: dict[tuple[str, str, str], tuple[str, PhaseEvent]] = {}
    conflicts: set[tuple[str, str]] = set()
    reasons: list[str] = []
    for native_path in paths:
        try:
            lines = native_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as error:
            reasons.append(f"{native_path}: {type(error).__name__}: {error}")
            continue
        for line_number, line in enumerate(lines, start=1):
            try:
                native = _native_event(_decode(line, f"{native_path}:{line_number}"), origin, profile)
            except ProfileInputError as error:
                reasons.append(f"{native_path.name}:{line_number}: {error}")
                continue
            if native.kind not in ("pytest_phase_start", "pytest_phase_report"):
                continue
            key = (native.origin.process_id, native.span_id, native.kind)
            existing = events.get(key)
            if existing is not None and existing[0] != line:
                conflicts.add(key[:2])
                reasons.append(f"conflicting native phase records: process {key[0]} span {key[1]}")
            elif existing is None:
                events[key] = (line, native)
    samples = _pairs_to_samples(events, conflicts, profile, reasons)
    status = "partial" if samples and reasons else "available" if samples else "unavailable"
    if not samples and not reasons:
        reasons.append("no complete native setup/call/teardown samples")
    return samples, InputDiagnostic("batch", str(path), status, len(samples), tuple(sorted(set(reasons)))), paths


def _previous_sample(value: object, profile: ProfileIdentity) -> DurationSample:
    data = _object(value, "historical sample")
    origin_data = _object(data.get("origin"), "historical origin")
    batch = BatchOrigin.parse(origin_data)
    process_id = _text(origin_data.get("process_id"), "origin.process_id")
    pid = origin_data.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        raise ProfileInputError("historical origin.pid must be positive")
    version = _text(origin_data.get("python_version"), "origin.python_version")
    if not re.fullmatch(re.escape(profile.python_major_minor) + r"\.[0-9]+(?:[a-zA-Z0-9.-]*)?", version):
        raise ProfileInputError("historical Python version does not match profile")
    origin = ExecutionOrigin(batch, process_id, pid, version)
    spans_data = _object(data.get("phase_span_ids"), "phase_span_ids")
    started_data = _object(data.get("phase_started_at"), "phase_started_at")
    durations_data = _object(data.get("native_phase_ms"), "native_phase_ms")
    outcomes_data = _object(data.get("phase_outcomes"), "phase_outcomes")
    if any(set(section) != set(PHASES) for section in (spans_data, started_data, durations_data, outcomes_data)):
        raise ProfileInputError("historical sample requires exactly three native phases")
    spans = {phase: _text(spans_data[phase], f"phase_span_ids.{phase}") for phase in PHASES}
    started = {phase: _utc(_timestamp(started_data[phase], f"phase_started_at.{phase}")) for phase in PHASES}
    if [started[phase] for phase in PHASES] != sorted(started.values()):
        raise ProfileInputError("historical native phases are out of order")
    if len(set(spans.values())) != len(PHASES):
        raise ProfileInputError("historical phase span IDs must be distinct")
    durations = {phase: _number(durations_data[phase], f"native_phase_ms.{phase}") for phase in PHASES}
    outcomes: dict[str, dict[str, object]] = {}
    for phase in PHASES:
        outcome = _object(outcomes_data[phase], f"phase_outcomes.{phase}")
        name = _text(outcome.get("outcome"), f"phase_outcomes.{phase}.outcome")
        if name not in OUTCOMES:
            raise ProfileInputError("unknown historical pytest outcome")
        outcomes[phase] = {"outcome": name, "wasxfail": _optional_string(outcome.get("wasxfail"), "wasxfail")}
    nodeid = _text(data.get("nodeid"), "sample.nodeid")
    sample = DurationSample(_text(data.get("sample_id"), "sample_id"), nodeid, _timestamp(data.get("observed_at"), "observed_at"), origin, spans, started, durations, outcomes)
    if _utc(sample.observed_at) != started["setup"]:
        raise ProfileInputError("historical sample observed time differs from native setup")
    expected_id = _digest({"profile": profile.json(), "origin": origin.json(), "nodeid": nodeid, "phase_span_ids": spans})
    if sample.sample_id != expected_id:
        raise ProfileInputError("historical sample identity digest mismatch")
    if _number(data.get("raw_duration_seconds"), "raw_duration_seconds") != sample.raw_seconds or _number(data.get("duration_seconds"), "duration_seconds") != sample.duration_seconds:
        raise ProfileInputError("historical sample does not match native phase durations")
    return sample


def _ranked(samples: list[DurationSample]) -> float:
    durations = sorted(sample.duration_seconds for sample in samples)
    return durations[math.ceil(0.95 * len(durations)) - 1]


def _previous(path: Path, profile: ProfileIdentity) -> tuple[list[DurationSample], InputDiagnostic]:
    data = _read_json(path)
    if data.get("schema_version") != PROFILE_SCHEMA or ProfileIdentity.parse(data.get("profile")) != profile:
        raise ProfileInputError(f"{path}: incompatible historical profile")
    digest = data.pop("digest_sha256", None)
    if not isinstance(digest, str) or digest != _digest(data):
        raise ProfileInputError(f"{path}: historical snapshot digest mismatch")
    generated_at = _timestamp(data.get("generated_at"), "historical generated_at")
    if (data.get("retention_days"), data.get("max_samples_per_node"), data.get("min_estimate_seconds")) != (RETENTION_DAYS, MAX_SAMPLES, MIN_ESTIMATE_SECONDS):
        raise ProfileInputError(f"{path}: historical retention/estimate parameters differ")
    source_status = _text(data.get("status"), "historical status")
    if source_status not in ("available", "partial", "unavailable"):
        raise ProfileInputError(f"{path}: unknown historical status")
    nodes = _object(data.get("nodes"), "historical nodes")
    samples: list[DurationSample] = []
    for nodeid, entry in sorted(nodes.items()):
        node = _object(entry, f"nodes.{nodeid}")
        raw_samples = node.get("samples")
        if not isinstance(raw_samples, list) or not raw_samples:
            raise ProfileInputError(f"nodes.{nodeid} requires nonempty samples")
        parsed = [_previous_sample(raw, profile) for raw in raw_samples]
        if len(parsed) > MAX_SAMPLES or parsed != sorted(parsed, key=lambda sample: (sample.observed_at, sample.sample_id)):
            raise ProfileInputError(f"nodes.{nodeid} violates historical sample ordering/limit")
        if any(not generated_at - timedelta(days=RETENTION_DAYS) <= sample.observed_at <= generated_at for sample in parsed):
            raise ProfileInputError(f"nodes.{nodeid} contains a sample outside its original retention window")
        if any(sample.nodeid != nodeid for sample in parsed) or len({sample.sample_id for sample in parsed}) != len(parsed):
            raise ProfileInputError(f"nodes.{nodeid} contains mismatched or duplicate samples")
        if node.get("sample_count") != len(parsed) or node.get("p95_seconds") != _ranked(parsed):
            raise ProfileInputError(f"nodes.{nodeid} has inconsistent estimate/count")
        if node.get("oldest_at") != _utc(min(sample.observed_at for sample in parsed)) or node.get("newest_at") != _utc(max(sample.observed_at for sample in parsed)):
            raise ProfileInputError(f"nodes.{nodeid} has inconsistent timestamps")
        samples.extend(parsed)
    if bool(samples) != (source_status != "unavailable"):
        raise ProfileInputError(f"{path}: historical availability does not match the retained samples")
    reasons = (f"source snapshot has {source_status} diagnostics",) if source_status != "available" else ()
    return samples, InputDiagnostic("previous", str(path), source_status, len(samples), reasons)


def build_profile(context: ProfileContext, batches: list[Path], previous: list[Path]) -> ProfileResult:
    """Merge only valid native samples, preserving all inputs and their provenance."""

    diagnostics: list[InputDiagnostic] = []
    native_input_paths: set[Path] = set()
    samples_by_id: dict[str, DurationSample] = {}
    historical_ids: set[str] = set()
    conflicting_new_ids: set[str] = set()
    for kind, paths in (("previous", previous), ("batch", batches)):
        for path in sorted(set(p.resolve() for p in paths)):
            try:
                if kind == "batch":
                    candidates, diagnostic, native_paths = _batch(path, context.profile)
                    native_input_paths.update(native_paths)
                else:
                    candidates, diagnostic = _previous(path, context.profile)
            except ProfileInputError as error:
                diagnostics.append(InputDiagnostic(kind, str(path), "unavailable", 0, (str(error),)))
                continue
            reasons = list(diagnostic.reasons)
            for candidate in candidates:
                if candidate.sample_id in conflicting_new_ids:
                    reasons.append(f"conflicting batch deliveries for sample ID {candidate.sample_id}; sample omitted")
                    continue
                existing = samples_by_id.get(candidate.sample_id)
                if existing is None:
                    samples_by_id[candidate.sample_id] = candidate
                    if kind == "previous":
                        historical_ids.add(candidate.sample_id)
                elif existing.json() != candidate.json():
                    if kind == "batch" and candidate.sample_id not in historical_ids:
                        samples_by_id.pop(candidate.sample_id)
                        conflicting_new_ids.add(candidate.sample_id)
                        reasons.append(f"conflicting batch deliveries for sample ID {candidate.sample_id}; sample omitted")
                    else:
                        reasons.append(f"conflicting sample ID {candidate.sample_id}; earlier validated history preserved")
            status = "partial" if diagnostic.samples and reasons else "unavailable" if reasons else diagnostic.status
            diagnostics.append(InputDiagnostic(kind, str(path), status, diagnostic.samples, tuple(sorted(set(reasons)))))

    cutoff = context.evaluation_time - timedelta(days=RETENTION_DAYS)
    nodes: dict[str, list[DurationSample]] = {}
    for sample in samples_by_id.values():
        if cutoff <= sample.observed_at <= context.evaluation_time:
            nodes.setdefault(sample.nodeid, []).append(sample)
    output_nodes: dict[str, object] = {}
    for nodeid, node_samples in sorted(nodes.items()):
        retained = sorted(node_samples, key=lambda sample: (sample.observed_at, sample.sample_id), reverse=True)[:MAX_SAMPLES]
        retained.sort(key=lambda sample: (sample.observed_at, sample.sample_id))
        output_nodes[nodeid] = {
            "p95_seconds": _ranked(retained),
            "sample_count": len(retained),
            "oldest_at": _utc(retained[0].observed_at),
            "newest_at": _utc(retained[-1].observed_at),
            "samples": [sample.json() for sample in retained],
        }
    reasons = sorted({f"{diagnostic.kind} {diagnostic.source}: {reason}" for diagnostic in diagnostics for reason in diagnostic.reasons})
    if not output_nodes:
        reasons.append("no completed samples within the retention window; unknown nodes remain unknown")
    status = "partial" if reasons and output_nodes else "available" if output_nodes else "unavailable"
    payload: dict[str, object] = {
        "schema_version": PROFILE_SCHEMA,
        "profile": context.profile.json(),
        "generated_at": _utc(context.evaluation_time),
        "retention_days": RETENTION_DAYS,
        "max_samples_per_node": MAX_SAMPLES,
        "min_estimate_seconds": MIN_ESTIMATE_SECONDS,
        "status": status,
        "reasons": reasons,
        "inputs": [item.json() for item in sorted(diagnostics, key=lambda item: (item.kind, item.source))],
        "nodes": output_nodes,
    }
    payload["digest_sha256"] = _digest(payload)
    return ProfileResult(payload, tuple(diagnostics), tuple(sorted(native_input_paths)))


def write_profile(path: Path, result: ProfileResult, inputs: list[Path]) -> None:
    """Publish a complete immutable snapshot with exclusive final-name creation."""

    destination = path.resolve()
    if any(destination == source.resolve() for source in (*inputs, *result.native_input_paths)):
        raise ProfileInputError("output must not overwrite a context, manifest, native JSONL input or previous snapshot")
    if path.exists() or path.is_symlink():
        raise ProfileInputError(f"output already exists: {path}; snapshots are immutable")
    temporary: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(_canonical(result.payload))
            stream.flush()
            os.fsync(stream.fileno())
        # A preflight existence check is not a lock: link() creates the final
        # name exclusively without ever replacing a concurrent publisher.
        os.link(temporary, path)
    except OSError as error:
        raise ProfileInputError(f"atomic output failed for {path}: {type(error).__name__}: {error}") from error
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass
