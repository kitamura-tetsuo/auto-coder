"""Attempt-bound, bounded diagnostic evidence for ordinary PR adversarial validation.

This module extends the existing non-authorizing ``ReviewAuditStore`` with one
typed diagnostic record per native adversarial-validation attempt. It retains
what the controller actually consumed and checked (input fingerprints, the
supplied/checked requirement manifests, response fingerprints, the parsed
returned coverage entries, the deterministic coverage interpretation and the
final result) so a mismatch can be diagnosed without rerunning the validation.

Everything here is observation only: hooks never raise, never change a prompt,
backend call, verdict or exception, never perform GitHub/LLM requests, and a
failed write is only logged. Digests are fingerprints of the exact UTF-8 input,
not retrievable content or attestation.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from loguru import logger

from ..build_provenance import ControllerArtifactObservation, observe_controller_artifact
from ..review_audit import (
    VALIDATION_EVIDENCE_SCHEMA_VERSION,
    ValidationEvidenceRow,
    redact_sensitive_data,
)
from .recorder import get_review_audit_store

DIGEST_ENCODING = "sha256-of-exact-utf8-bytes"
MANIFEST_IDENTITY_VERSION = "requirement-manifest-v1"

MAX_RECORD_BYTES = 256 * 1024
MAX_ENTRIES = 500
MAX_TEXT_CHARS = 2000

ROLE_PRODUCER = "producer"
ROLE_REUSE = "reuse"

_REDACTION_MARKER = "[REDACTED]"
# Progressive (entry cap, text cap) steps used to fit the size budget.
_BUDGET_STEPS: Tuple[Tuple[int, int], ...] = (
    (MAX_ENTRIES, MAX_TEXT_CHARS),
    (250, 1000),
    (100, 500),
    (40, 200),
    (10, 80),
    (0, 0),
)
_QUALIFIED_ID = re.compile(r"^(?:[\w.-]+/[\w.-]+)?#\d+/[\w.-]+$")
_PATH_LIKE = re.compile(r"(?:\\|://|^(?:/|~|\.{1,2}/|[A-Za-z]:)|/[^/]*\.[A-Za-z][A-Za-z0-9]{0,4}$)")
# Absolute/home/drive paths and URLs embedded in free text are never retained.
_FREE_TEXT_LOCATION = re.compile(r"(?:\b[A-Za-z][A-Za-z0-9+.\-]*://\S+|(?<![\w#:/.\-])(?:~|\.{1,2})?/(?:[\w.\-]+/)*[\w.\-]+|\b[A-Za-z]:\\\S+)")
_LABEL_CHARS = 200
_CREDENTIAL_ENV_NAME = re.compile(r"(?:TOKEN|SECRET|PASSWORD|API_?KEY|CREDENTIAL)", re.IGNORECASE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utf8(text: str) -> bytes:
    return text.encode("utf-8", errors="surrogatepass")


def sha256_text(text: str) -> str:
    return hashlib.sha256(_utf8(text)).hexdigest()


def configured_credentials() -> List[str]:
    """Configured secret values (from the environment) that must never be retained."""
    return [value for name, value in os.environ.items() if len(value) >= 8 and _CREDENTIAL_ENV_NAME.search(name)]


@dataclass(frozen=True)
class Fingerprint:
    """SHA-256 and UTF-8 byte length of an exact text; never the text itself."""

    state: str = "unavailable"  # present | empty | unavailable
    sha256: Optional[str] = None
    byte_length: Optional[int] = None


def fingerprint_text(text: Optional[str]) -> Fingerprint:
    if text is None:
        return Fingerprint()
    raw = _utf8(text)
    return Fingerprint(state="empty" if not text.strip() else "present", sha256=hashlib.sha256(raw).hexdigest(), byte_length=len(raw))


@dataclass
class ManifestInput:
    """The ordered ``(requirement_id, text)`` entries and mode of one manifest."""

    mode: str = ""
    entries: Sequence[Tuple[str, str]] = ()
    validation_snapshot: str = ""  # the controller's snapshot digest this manifest belongs to


def manifest_identity(entries: Sequence[Tuple[str, str]]) -> str:
    """Versioned SHA-256 over the exact ordered ID/text representation."""
    document = json.dumps({"version": MANIFEST_IDENTITY_VERSION, "entries": [[rid, text] for rid, text in entries]}, ensure_ascii=False, separators=(",", ":"))
    return sha256_text(document)


@dataclass
class IssueInputObservation:
    repository: str = ""
    number: int = 0
    body: Fingerprint = field(default_factory=Fingerprint)
    source_updated_at: Optional[str] = None
    retrieval_mode: Optional[str] = None


@dataclass
class InputObservation:
    captured_at: str = ""
    pr_body: Fingerprint = field(default_factory=Fingerprint)
    pr_source_updated_at: Optional[str] = None  # observed in the supplied PR metadata; None means unknown
    resolved_issues: List[IssueInputObservation] = field(default_factory=list)
    linked_issue_context: Fingerprint = field(default_factory=Fingerprint)
    rendered_pr_body: Optional[Fingerprint] = None  # only when the supplied form differs from the consumed body


@dataclass
class ManifestObservation:
    manifest_id: str = ""
    role: str = ""  # supplied | checked
    mode: str = ""
    entries: List[Tuple[str, Fingerprint]] = field(default_factory=list)
    identity_sha256: str = ""
    validation_snapshot: str = ""


@dataclass
class ReturnedEntry:
    requirement_id: str = ""
    status: str = ""


@dataclass
class ResponseObservation:
    response_id: str = ""
    stage: str = ""
    started_at: str = ""
    prompt: Fingerprint = field(default_factory=Fingerprint)
    manifest_transmitted: bool = False
    supplied_manifest_id: Optional[str] = None
    continues_manifest_id: Optional[str] = None
    continues_unavailable_reason: Optional[str] = None
    interaction_ids: List[str] = field(default_factory=list)
    interaction_association: str = "unavailable"
    interaction_unavailable_reason: Optional[str] = "not_yet_associated"
    backend_alias: Optional[str] = None
    backend_type: Optional[str] = None
    provider_alias: Optional[str] = None
    requested_model: Optional[str] = None
    reported_model: Optional[str] = None
    response: Fingerprint = field(default_factory=Fingerprint)
    response_state: str = "pending"  # pending | nonempty | empty | unavailable
    semantic_payload: Optional[Fingerprint] = None  # only when transport normalization changed the payload
    parse_state: str = "pending"  # pending | parsed | failed
    parse_failure_category: Optional[str] = None
    parsed_verdict: Optional[str] = None  # the verdict label the model itself returned
    post_parse_verdict: Optional[str] = None  # after the parser's finding precedence, before coverage checking
    returned_entries: List[ReturnedEntry] = field(default_factory=list)
    returned_entries_observed: bool = False


@dataclass
class CoverageCheckObservation:
    check_id: str = ""
    response_id: Optional[str] = None
    performed: bool = False
    not_performed_reason: Optional[str] = None
    supplied_manifest_id: Optional[str] = None
    checked_manifest_id: Optional[str] = None
    expected_ids: List[str] = field(default_factory=list)
    returned_ids: List[str] = field(default_factory=list)
    missing_ids: List[str] = field(default_factory=list)
    duplicate_ids: List[str] = field(default_factory=list)
    unknown_ids: List[str] = field(default_factory=list)
    verdict_before: Optional[str] = None
    verdict_after: Optional[str] = None
    diagnostic_category: Optional[str] = None
    diagnostic_reason: Optional[str] = None


@dataclass
class FinalObservation:
    recorded_at: str = ""
    kind: str = ""  # semantic_response | local_without_semantic_response | exception
    verdict: Optional[str] = None
    summary: Optional[str] = None
    diagnostic_category: Optional[str] = None
    diagnostic_reason: Optional[str] = None
    source_response_id: Optional[str] = None


@dataclass
class AttemptIdentity:
    repository: str = ""
    pr_number: int = 0
    head_sha: Optional[str] = None
    base_sha: Optional[str] = None
    review_id: str = ""
    attempt_id: str = ""
    attempt_sequence: int = 0
    process_run_id: Optional[str] = None
    execution_id: Optional[str] = None


class _Limits:
    """Per-render caps plus the omission/clipping ledger for one rendering."""

    def __init__(self, entry_cap: int, text_cap: int, credentials: Sequence[str]):
        self.entry_cap = entry_cap
        self.text_cap = text_cap
        self.credentials = list(credentials)
        self.omissions: List[Dict[str, Any]] = []
        self.clipped_fields: List[str] = []

    def cap(self, section: str, items: Sequence[Any]) -> List[Any]:
        total = len(items)
        kept = list(items[: self.entry_cap])
        if total > len(kept):
            self.omissions.append({"section": section, "source_count": total, "retained": len(kept), "omitted": total - len(kept)})
        return kept

    def text(self, section: str, value: Optional[str]) -> Optional[str]:
        """Redact first, then clip; clipping is recorded explicitly."""
        if value is None:
            return None
        redacted = _FREE_TEXT_LOCATION.sub("[LOCATION]", str(redact_sensitive_data(str(value), self.credentials)))
        if len(redacted) > self.text_cap:
            self.clipped_fields.append(section)
            return redacted[: self.text_cap]
        return redacted

    def label(self, section: str, value: Optional[str]) -> Optional[str]:
        """Short categorical value (verdict, category, backend name): kept even at the smallest budget."""
        if value is None:
            return None
        redacted = _FREE_TEXT_LOCATION.sub("[LOCATION]", str(redact_sensitive_data(str(value), self.credentials)))
        if len(redacted) > _LABEL_CHARS:
            self.clipped_fields.append(section)
            return redacted[:_LABEL_CHARS]
        return redacted

    def identity(self, value: Optional[str]) -> Any:
        """Return the exact identity, or explicit omission metadata (never a changed ID)."""
        if value is None:
            return None
        raw = str(value)
        reason = None
        if str(redact_sensitive_data(raw, self.credentials)) != raw:
            reason = "redacted"
        elif len(raw) > self.text_cap:
            reason = "too_long"
        elif _PATH_LIKE.search(raw) and not _QUALIFIED_ID.match(raw):
            reason = "path_or_url_like"
        if reason is None:
            return raw
        return {"omitted": True, "reason": reason, "sha256": sha256_text(raw), "byte_length": len(_utf8(raw))}

    def identities(self, section: str, values: Sequence[str]) -> List[Any]:
        return [self.identity(value) for value in self.cap(section, values)]


def _fp(fingerprint: Optional[Fingerprint]) -> Optional[Dict[str, Any]]:
    return None if fingerprint is None else asdict(fingerprint)


class AttemptEvidenceRecorder:
    """Accumulates and persists one attempt's evidence; every method is best-effort."""

    def __init__(self, identity: AttemptIdentity, build: ControllerArtifactObservation, role: str = ROLE_PRODUCER):
        self._identity = identity
        self._build = build
        self._role = role
        self._lock = threading.RLock()
        self._input: Optional[InputObservation] = None
        self._manifests: List[ManifestObservation] = []
        self._responses: List[ResponseObservation] = []
        self._checks: List[CoverageCheckObservation] = []
        self._final: Optional[FinalObservation] = None
        self._interaction_baselines: Dict[str, Optional[set[str]]] = {}
        self._flush_failures = 0

    # -- identity ----------------------------------------------------------
    @property
    def review_id(self) -> str:
        return self._identity.review_id

    # -- observations ------------------------------------------------------
    def observe_inputs(self, observation: InputObservation) -> None:
        with self._lock:
            if self._input is None:  # the first (consumed) construction is the only authoritative one
                self._input = observation
        self.flush()

    def _manifest(self, role: str, manifest: ManifestInput) -> ManifestObservation:
        identity = manifest_identity(manifest.entries)
        for existing in self._manifests:
            if existing.role == role and existing.mode == manifest.mode and existing.identity_sha256 == identity and existing.validation_snapshot == manifest.validation_snapshot:
                return existing
        observation = ManifestObservation(
            manifest_id=f"{role}-{sum(1 for m in self._manifests if m.role == role) + 1}",
            role=role,
            mode=manifest.mode,
            entries=[(requirement_id, fingerprint_text(text)) for requirement_id, text in manifest.entries],
            identity_sha256=identity,
            validation_snapshot=manifest.validation_snapshot,
        )
        self._manifests.append(observation)
        return observation

    def begin_phase(self, stage: str, prompt: str, manifest: Optional[ManifestInput], rendered_pr_body: Optional[str] = None) -> None:
        """Record one prompt/follow-up before its reviewer invocation."""
        with self._lock:
            if rendered_pr_body is not None and self._input is not None and self._input.rendered_pr_body is None:
                rendered = fingerprint_text(rendered_pr_body)
                if rendered != self._input.pr_body:
                    self._input.rendered_pr_body = rendered
            phase = ResponseObservation(response_id=f"r{len(self._responses) + 1}", stage=stage, started_at=_now_iso(), prompt=fingerprint_text(prompt))
            if manifest is not None:
                phase.manifest_transmitted = True
                phase.supplied_manifest_id = self._manifest("supplied", manifest).manifest_id
            else:
                earlier = [r.supplied_manifest_id for r in self._responses if r.supplied_manifest_id]
                if earlier:
                    phase.continues_manifest_id = earlier[-1]
                else:
                    phase.continues_unavailable_reason = "no_earlier_supplied_manifest_recorded"
            self._interaction_baselines[phase.response_id] = self._read_interaction_ids()
            self._responses.append(phase)
        self.flush()

    def _pending_phase(self, stage: str) -> ResponseObservation:
        for phase in reversed(self._responses):
            if phase.response_state == "pending" and phase.stage == stage:
                return phase
        # A response consumed without a recorded prompt keeps its own identity; its prompt is unavailable.
        phase = ResponseObservation(response_id=f"r{len(self._responses) + 1}", stage=stage, started_at=_now_iso())
        phase.continues_unavailable_reason = "prompt_not_recorded"
        self._responses.append(phase)
        return phase

    def _read_interactions(self) -> Optional[List[Any]]:
        try:
            read = get_review_audit_store().get_evaluation(self._identity.repository, self._identity.review_id)
        except Exception:
            return None
        return list(read.record.interactions) if read.record is not None else None

    def _read_interaction_ids(self) -> Optional[set[str]]:
        interactions = self._read_interactions()
        return None if interactions is None else {i.interaction_id for i in interactions}

    def _associate_interactions(self, phase: ResponseObservation) -> None:
        """Verified only when the interaction set was readable both before and after the call.

        A response is never matched to an interaction that merely remains
        unassigned: unreadable evidence leaves the association unavailable.
        """
        if phase.response_id not in self._interaction_baselines or self._interaction_baselines[phase.response_id] is None:
            phase.interaction_unavailable_reason = "pre_invocation_interaction_snapshot_unavailable"
            return
        baseline = self._interaction_baselines[phase.response_id] or set()
        interactions = self._read_interactions()
        if interactions is None:
            phase.interaction_unavailable_reason = "interaction_records_unavailable"
            return
        fresh = [i for i in interactions if i.interaction_id not in baseline]
        if not fresh:
            phase.interaction_unavailable_reason = "no_new_interaction_recorded"
            return
        producer = next((i for i in reversed(fresh) if i.completion_status == "RETURNED"), fresh[-1])
        phase.interaction_ids = [i.interaction_id for i in fresh]
        phase.interaction_association = "verified_review_scoped_interaction_records"
        phase.interaction_unavailable_reason = None
        phase.backend_alias, phase.backend_type, phase.provider_alias = producer.backend_alias, producer.backend_type, producer.provider_alias
        phase.requested_model, phase.reported_model = producer.requested_model, producer.reported_model

    def complete_response(self, stage: str, response: Optional[str], semantic_payload: Optional[str], returned: Optional[List[ReturnedEntry]], parsed_verdict: Optional[str], post_parse_verdict: Optional[str], failure_category: Optional[str]) -> str:
        """Record the response exactly as passed to the parser; returns its response_id."""
        with self._lock:
            phase = self._pending_phase(stage)
            phase.response = fingerprint_text(response)
            phase.response_state = "unavailable" if response is None else ("empty" if not response.strip() else "nonempty")
            if response is not None and semantic_payload is not None and semantic_payload != response:
                phase.semantic_payload = fingerprint_text(semantic_payload)
            if returned is not None:
                phase.returned_entries, phase.returned_entries_observed = returned, True
            phase.parse_state = "failed" if failure_category else "parsed"
            phase.parse_failure_category = failure_category
            phase.parsed_verdict = parsed_verdict
            phase.post_parse_verdict = post_parse_verdict
            self._associate_interactions(phase)
            response_id = phase.response_id
        self.flush()
        return response_id

    def observe_check(self, response_id: Optional[str], manifest: ManifestInput, returned_ids: Sequence[str], verdict_before: Optional[str], not_performed_reason: Optional[str] = None) -> CoverageCheckObservation:
        with self._lock:
            checked = self._manifest("checked", manifest)
            response = next((r for r in self._responses if r.response_id == response_id), None)
            expected = [rid for rid, _ in manifest.entries]
            observation = CoverageCheckObservation(
                check_id=f"c{len(self._checks) + 1}",
                response_id=response_id,
                performed=not_performed_reason is None,
                not_performed_reason=not_performed_reason,
                supplied_manifest_id=(response.supplied_manifest_id or response.continues_manifest_id) if response else None,
                checked_manifest_id=checked.manifest_id,
                verdict_before=verdict_before,
            )
            if not_performed_reason is None:
                expected_set, returned_set = set(expected), set(returned_ids)
                observation.expected_ids = expected
                observation.returned_ids = list(returned_ids)
                observation.missing_ids = [rid for rid in expected if rid not in returned_set]
                observation.unknown_ids = [rid for rid in returned_ids if rid not in expected_set]
                parsed_ids = [entry.requirement_id for entry in response.returned_entries] if response else []
                observation.duplicate_ids = sorted({rid for rid in parsed_ids if parsed_ids.count(rid) > 1})
            self._checks.append(observation)
            return observation

    def finish_check(self, observation: CoverageCheckObservation, verdict_after: Optional[str], category: Optional[str], reason: Optional[str]) -> None:
        with self._lock:
            observation.verdict_after, observation.diagnostic_category, observation.diagnostic_reason = verdict_after, category, reason
        self.flush()

    def observe_final(self, final: FinalObservation) -> None:
        with self._lock:
            for phase in self._responses:
                if phase.response_state == "pending":
                    self._associate_interactions(phase)  # invocation may have raised before any response
                    phase.response_state = "unavailable"
            self._final = final
        self.flush()

    def response_manifest_ids(self, response_id: Optional[str]) -> Optional[str]:
        with self._lock:
            response = next((r for r in self._responses if r.response_id == response_id), None)
            return (response.supplied_manifest_id or response.continues_manifest_id) if response else None

    # -- rendering and persistence ----------------------------------------
    def _unrecorded(self) -> List[str]:
        missing: List[str] = []
        if self._input is None:
            missing.append("input")
        if self._final is None:
            if not self._responses or any(r.response_state == "pending" for r in self._responses):
                missing.append("response")
            if not self._checks:
                missing.append("coverage_check")
            missing.append("final_result")
        return missing

    def _render(self, limits: _Limits) -> Dict[str, Any]:
        ident = self._identity
        unavailable: List[str] = []
        for name in ("process_run_id", "execution_id", "head_sha", "base_sha"):
            if getattr(ident, name) is None:
                unavailable.append(name)
        payload: Dict[str, Any] = {
            "schema_version": VALIDATION_EVIDENCE_SCHEMA_VERSION,
            "digest_encoding": DIGEST_ENCODING,
            "manifest_identity_version": MANIFEST_IDENTITY_VERSION,
            "role": self._role,
            "completeness": "complete" if not self._unrecorded() else "partial",
            "unrecorded": self._unrecorded(),
            "identity": {
                "repository": limits.identity(ident.repository),
                "pr_number": ident.pr_number,
                "head_sha": limits.identity(ident.head_sha),
                "base_sha": limits.identity(ident.base_sha),
                "review_id": limits.identity(ident.review_id),
                "attempt_id": limits.identity(ident.attempt_id),
                "attempt_sequence": ident.attempt_sequence,
                "process_run_id": limits.identity(ident.process_run_id),
                "execution_id": limits.identity(ident.execution_id),
                "unavailable": unavailable,
            },
            "build": self._render_build(limits),
        }
        if self._input is not None:
            observed = self._input
            payload["input"] = {
                "captured_at": observed.captured_at,
                "capture_time_is_not_freshness": True,
                "pr_body": _fp(observed.pr_body),
                "pr_source_updated_at": limits.label("input.pr_source_updated_at", observed.pr_source_updated_at),
                "rendered_pr_body": _fp(observed.rendered_pr_body),
                "linked_issue_context": _fp(observed.linked_issue_context),
                "resolved_issues": [
                    {
                        "repository": limits.identity(issue.repository),
                        "number": issue.number,
                        "body": _fp(issue.body),
                        "source_updated_at": limits.label("input.source_updated_at", issue.source_updated_at),
                        "retrieval_mode": limits.label("input.retrieval_mode", issue.retrieval_mode),
                    }
                    for issue in limits.cap("input.resolved_issues", observed.resolved_issues)
                ],
                "resolved_issue_count": len(observed.resolved_issues),
            }
        payload["manifests"] = [self._render_manifest(m, limits) for m in limits.cap("manifests", self._manifests)]
        payload["responses"] = [self._render_response(r, limits) for r in limits.cap("responses", self._responses)]
        payload["coverage_checks"] = [self._render_check(c, limits) for c in limits.cap("coverage_checks", self._checks)]
        if self._final is not None:
            final = self._final
            payload["final"] = {
                "recorded_at": final.recorded_at,
                "kind": final.kind,
                "verdict": limits.label("final.verdict", final.verdict),
                "summary": limits.text("final.summary", final.summary),
                "diagnostic_category": limits.label("final.diagnostic_category", final.diagnostic_category),
                "diagnostic_reason": limits.text("final.diagnostic_reason", final.diagnostic_reason),
                "source_response_id": final.source_response_id,
            }
        payload["limits"] = {"max_record_bytes": MAX_RECORD_BYTES, "max_entries": MAX_ENTRIES, "max_text_chars": MAX_TEXT_CHARS, "applied_entry_cap": limits.entry_cap, "applied_text_cap": limits.text_cap, "omissions": limits.omissions, "clipped_fields": limits.clipped_fields}
        return payload

    def _render_build(self, limits: _Limits) -> Dict[str, Any]:
        build = self._build
        rendered: Dict[str, Any] = {"schema_version": build.schema_version, "process_run_id": limits.identity(build.process_run_id)}
        for name in ("process_run", "distribution_version", "source_revision"):
            entry = getattr(build, name)
            rendered[name] = {"value": limits.label(f"build.{name}", entry.value), "origin": entry.origin, "available": entry.available, "reason": entry.reason}
        return rendered

    def _render_manifest(self, manifest: ManifestObservation, limits: _Limits) -> Dict[str, Any]:
        entries = limits.cap(f"manifests.{manifest.manifest_id}.entries", manifest.entries)
        return {
            "manifest_id": manifest.manifest_id,
            "role": manifest.role,
            "mode": limits.label("manifest.mode", manifest.mode),
            "count": len(manifest.entries),
            "identity_sha256": manifest.identity_sha256,
            "validation_snapshot": limits.identity(manifest.validation_snapshot or None),
            "entries": [{"id": limits.identity(requirement_id), "text": _fp(fingerprint)} for requirement_id, fingerprint in entries],
        }

    def _render_response(self, response: ResponseObservation, limits: _Limits) -> Dict[str, Any]:
        returned = limits.cap(f"responses.{response.response_id}.returned_entries", response.returned_entries)
        return {
            "response_id": response.response_id,
            "stage": response.stage,
            "started_at": response.started_at,
            "prompt": _fp(response.prompt),
            "manifest_transmitted": response.manifest_transmitted,
            "supplied_manifest_id": response.supplied_manifest_id,
            "continues_manifest_id": response.continues_manifest_id,
            "continues_unavailable_reason": response.continues_unavailable_reason,
            "interaction": {
                "association": response.interaction_association,
                "unavailable_reason": response.interaction_unavailable_reason,
                "interaction_ids": limits.identities(f"responses.{response.response_id}.interaction_ids", response.interaction_ids),
                "backend_alias": limits.label("response.backend_alias", response.backend_alias),
                "backend_type": limits.label("response.backend_type", response.backend_type),
                "provider_alias": limits.label("response.provider_alias", response.provider_alias),
                "requested_model": limits.label("response.requested_model", response.requested_model),
                "reported_model": limits.label("response.reported_model", response.reported_model),
            },
            "response": _fp(response.response),
            "response_state": response.response_state,
            "semantic_payload": _fp(response.semantic_payload),
            "parse": {
                "state": response.parse_state,
                "failure_category": limits.label("response.parse_failure_category", response.parse_failure_category),
                "parsed_verdict": limits.label("response.parsed_verdict", response.parsed_verdict),
                "post_parse_verdict": limits.label("response.post_parse_verdict", response.post_parse_verdict),
                "returned_entries_observed": response.returned_entries_observed,
                "returned_entry_count": len(response.returned_entries),
                "returned_entries": [{"id": limits.identity(entry.requirement_id), "status": limits.label("response.returned_status", entry.status)} for entry in returned],
            },
        }

    def _render_check(self, check: CoverageCheckObservation, limits: _Limits) -> Dict[str, Any]:
        base = f"coverage_checks.{check.check_id}"
        return {
            "check_id": check.check_id,
            "response_id": check.response_id,
            "performed": check.performed,
            "not_performed_reason": check.not_performed_reason,
            "supplied_manifest_id": check.supplied_manifest_id,
            "checked_manifest_id": check.checked_manifest_id,
            "counts_are_returned_evidence_not_acceptance": True,
            "expected_count": len(check.expected_ids),
            "returned_count": len(check.returned_ids),
            "missing_count": len(check.missing_ids),
            "duplicate_count": len(check.duplicate_ids),
            "unknown_count": len(check.unknown_ids),
            "expected_ids": limits.identities(f"{base}.expected_ids", check.expected_ids),
            "returned_ids": limits.identities(f"{base}.returned_ids", check.returned_ids),
            "missing_ids": limits.identities(f"{base}.missing_ids", check.missing_ids),
            "duplicate_ids": limits.identities(f"{base}.duplicate_ids", check.duplicate_ids),
            "unknown_ids": limits.identities(f"{base}.unknown_ids", check.unknown_ids),
            "verdict_before": limits.label("check.verdict_before", check.verdict_before),
            "verdict_after": limits.label("check.verdict_after", check.verdict_after),
            "diagnostic_category": limits.label("check.diagnostic_category", check.diagnostic_category),
            "diagnostic_reason": limits.text("check.diagnostic_reason", check.diagnostic_reason),
        }

    def render_bounded(self) -> Dict[str, Any]:
        """Render the redacted payload within the size/entry/text budget."""
        credentials = configured_credentials()
        with self._lock:
            for entry_cap, text_cap in _BUDGET_STEPS:
                payload = self._render(_Limits(entry_cap, text_cap, credentials))
                if len(_utf8(json.dumps(payload, sort_keys=True, ensure_ascii=False))) <= MAX_RECORD_BYTES:
                    return payload
            minimal = self._render(_Limits(0, 0, credentials))
            for section in ("input", "manifests", "responses", "coverage_checks"):
                minimal.pop(section, None)
            minimal["limits"]["incomplete_reason"] = "record_exceeds_size_budget"
            return minimal

    def flush(self) -> bool:
        """Persist the current state; failures are logged only and never raised."""
        try:
            payload = self.render_bounded()
            row = ValidationEvidenceRow(
                review_id=self._identity.review_id,
                repository=self._identity.repository,
                pr_number=str(self._identity.pr_number),
                attempt_id=self._identity.attempt_id,
                attempt_sequence=self._identity.attempt_sequence,
                role=self._role,
                schema_version=VALIDATION_EVIDENCE_SCHEMA_VERSION,
                completeness=payload["completeness"],
                updated_at=_now_iso(),
                payload=payload,
            )
            ok = get_review_audit_store().record_validation_evidence(row, configured_credentials())
        except Exception:
            ok = False
            logger.opt(exception=True).warning(f"Validation evidence: failed to render/record evidence for {self._identity.review_id}")
        if not ok:
            self._flush_failures += 1
        return ok


# ---------------------------------------------------------------------------
# Active-recorder binding and production hooks (all no-ops without a recorder)
# ---------------------------------------------------------------------------

_active_recorder: contextvars.ContextVar[Optional[AttemptEvidenceRecorder]] = contextvars.ContextVar("active_validation_evidence_recorder", default=None)
_recorders: Dict[str, AttemptEvidenceRecorder] = {}
_recorders_lock = threading.Lock()


def active_recorder() -> Optional[AttemptEvidenceRecorder]:
    return _active_recorder.get()


def start_attempt_capture(*, repository: str, pr_number: int, head_sha: Optional[str], base_sha: Optional[str], review_id: str, attempt_id: Optional[str], attempt_sequence: Optional[int]) -> Optional[AttemptEvidenceRecorder]:
    """Create and persist the identity/build record before any validation input is read.

    Returns ``None`` (capture simply absent) without a native attempt identity:
    a missing association is explicit and never guessed.
    """
    if not attempt_id or not attempt_sequence:
        return None
    try:
        build = observe_controller_artifact()
        process_run_id = build.process_run_id
        execution_id: Optional[str] = None
        try:
            from ..execution_trace import current_scope

            scope = current_scope()
            execution_id = scope.execution_id if scope is not None else None
        except Exception:
            execution_id = None
        recorder = AttemptEvidenceRecorder(
            AttemptIdentity(repository=repository, pr_number=pr_number, head_sha=head_sha or None, base_sha=base_sha or None, review_id=review_id, attempt_id=attempt_id, attempt_sequence=attempt_sequence, process_run_id=process_run_id, execution_id=execution_id),
            build,
        )
        with _recorders_lock:
            _recorders[review_id] = recorder
        recorder.flush()
        return recorder
    except Exception:
        logger.opt(exception=True).warning("Validation evidence: could not start attempt capture")
        return None


@contextlib.contextmanager
def bind_recorder(recorder: Optional[AttemptEvidenceRecorder]) -> Iterator[None]:
    token = _active_recorder.set(recorder)
    try:
        yield
    finally:
        _active_recorder.reset(token)


def pop_recorder(review_id: str) -> Optional[AttemptEvidenceRecorder]:
    with _recorders_lock:
        return _recorders.pop(review_id, None)


def record_reuse_observation(*, repository: str, pr_number: int, head_sha: Optional[str], review_id: str, attempt_id: str, attempt_sequence: int) -> None:
    """Record that ``review_id`` reused an existing attempt; never copies provenance.

    The producing evidence stays the producer row's. The source row is only
    referenced when an exact producer for this native attempt is recorded.
    """
    try:
        found = get_review_audit_store().get_validation_evidence(repository, str(pr_number), attempt_id=attempt_id)
        source = found.producer.review_id if found.producer is not None else None
        payload = {
            "schema_version": VALIDATION_EVIDENCE_SCHEMA_VERSION,
            "role": ROLE_REUSE,
            "completeness": "complete",
            "identity": {"repository": repository, "pr_number": pr_number, "head_sha": head_sha, "review_id": review_id, "attempt_id": attempt_id, "attempt_sequence": attempt_sequence},
            "reuse": {"source_review_id": source, "source_known": source is not None, "fresh_invocation": False},
        }
        get_review_audit_store().record_validation_evidence(
            ValidationEvidenceRow(review_id=review_id, repository=repository, pr_number=str(pr_number), attempt_id=attempt_id, attempt_sequence=attempt_sequence, role=ROLE_REUSE, schema_version=VALIDATION_EVIDENCE_SCHEMA_VERSION, completeness="complete", updated_at=_now_iso(), payload=payload),
            configured_credentials(),
        )
    except Exception:
        logger.opt(exception=True).warning("Validation evidence: failed to record reuse observation")


@contextlib.contextmanager
def _safe(action: str) -> Iterator[None]:
    """Run a hook body so a diagnostic fault is logged but never escapes to validation."""
    try:
        yield
    except Exception:
        logger.opt(exception=True).warning(f"Validation evidence: {action} capture failed; validation is unaffected")


def observe_context_inputs(repository: str, pr_body: str, resolution: Any, issue_context: str, pr_updated_at: Optional[Any] = None) -> None:
    """Fingerprint the PR/Issue inputs consumed by context construction."""
    recorder = active_recorder()
    if recorder is None:
        return
    with _safe("inputs"):
        issues = [IssueInputObservation(repository=repository, number=int(issue.number), body=fingerprint_text(issue.body), source_updated_at=getattr(issue, "updated_at", None), retrieval_mode=getattr(issue, "retrieval_mode", None)) for issue in getattr(resolution, "issues", ())]
        source_updated_at = pr_updated_at if isinstance(pr_updated_at, str) and pr_updated_at else None
        recorder.observe_inputs(InputObservation(captured_at=_now_iso(), pr_body=fingerprint_text(pr_body), pr_source_updated_at=source_updated_at, resolved_issues=issues, linked_issue_context=fingerprint_text(issue_context)))


def observe_prompt(stage: str, prompt: str, manifest: Optional[ManifestInput], rendered_pr_body: Optional[str] = None) -> None:
    """Record a prompt before the reviewer invocation it is submitted with."""
    recorder = active_recorder()
    if recorder is None:
        return
    with _safe("prompt"):
        recorder.begin_phase(stage, prompt, manifest, rendered_pr_body)


@dataclass
class ParseScope:
    stage: str
    response: Optional[str]
    semantic_payload: Optional[str] = None
    returned: Optional[List[ReturnedEntry]] = None
    model_verdict: Optional[str] = None
    result: Any = None


_active_parse: contextvars.ContextVar[Optional[ParseScope]] = contextvars.ContextVar("active_validation_parse_scope", default=None)


@contextlib.contextmanager
def response_parse(stage: str, response: Optional[str]) -> Iterator[Optional[ParseScope]]:
    """Bind a parse scope; on exit the response and its parsed result get one identity."""
    recorder = active_recorder()
    if recorder is None:
        yield None
        return
    scope = ParseScope(stage=stage, response=response)
    token = _active_parse.set(scope)
    try:
        yield scope
    finally:
        _active_parse.reset(token)
        with _safe("response"):
            result = scope.result
            failed = result is not None and str(getattr(result, "result", "")).strip().upper() == "ERROR" and bool(getattr(result, "diagnostic_category", None))
            response_id = recorder.complete_response(
                stage,
                response,
                scope.semantic_payload,
                scope.returned,
                scope.model_verdict,
                str(getattr(result, "result", "")) if result is not None else None,
                (getattr(result, "diagnostic_category", None) or "parse_failed") if failed or result is None else None,
            )
            if result is not None:
                result.source_response_id = response_id


def observe_semantic_payload(raw_response: str, effective_response: str) -> None:
    scope = _active_parse.get()
    if scope is not None:
        scope.semantic_payload = effective_response


def observe_model_verdict(raw_result: str) -> None:
    scope = _active_parse.get()
    if scope is not None:
        scope.model_verdict = raw_result


def observe_returned_coverage(raw_entries: Sequence[Any]) -> None:
    """Capture returned requirement entries exactly as parsed, including duplicates/unknowns."""
    scope = _active_parse.get()
    if scope is None:
        return
    with _safe("returned"):
        scope.returned = [ReturnedEntry(requirement_id=str(item.get("requirement_id", "")).strip(), status=str(item.get("status", "")).strip().upper()) for item in raw_entries if isinstance(item, dict)]


def observe_coverage_check_begin(source_response_id: Optional[str], manifest: ManifestInput, returned_ids: Sequence[str], verdict_before: Optional[str], not_performed_reason: Optional[str] = None) -> Optional[Tuple[AttemptEvidenceRecorder, CoverageCheckObservation]]:
    recorder = active_recorder()
    if recorder is None:
        return None
    with _safe("check"):
        return recorder, recorder.observe_check(source_response_id or None, manifest, returned_ids, verdict_before, not_performed_reason)
    return None


def observe_coverage_check_end(handle: Optional[Tuple[AttemptEvidenceRecorder, CoverageCheckObservation]], verdict_after: Optional[str], category: Optional[str], reason: Optional[str]) -> None:
    if handle is None:
        return
    with _safe("check"):
        handle[0].finish_check(handle[1], verdict_after, category, reason)


def finish_attempt_capture(review_id: str, result: Optional[Any]) -> None:
    """Record the final ordinary-validation result returned to the caller."""
    recorder = pop_recorder(review_id)
    if recorder is None:
        return
    with _safe("final"):
        source = str(getattr(result, "source_response_id", "") or "") or None
        category = getattr(result, "diagnostic_category", None)
        if result is None or category == "validation_execution_error":
            kind = "exception"
        elif source:
            kind = "semantic_response"
        else:
            kind = "local_without_semantic_response"
        recorder.observe_final(
            FinalObservation(
                recorded_at=_now_iso(),
                kind=kind,
                verdict=str(getattr(result, "result", "") or "ERROR") if result is not None else "ERROR",
                summary=getattr(result, "summary", None) if result is not None else None,
                diagnostic_category=category if result is not None else "unrecovered_exception",
                diagnostic_reason=getattr(result, "diagnostic_reason", None) if result is not None else None,
                source_response_id=source,
            )
        )
