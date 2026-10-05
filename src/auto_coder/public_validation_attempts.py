"""Bounded, public-safe projection of one validation attempt's diagnostic evidence.

Backs ``GET /api/validation-attempts``. The projection reads the durable
attempt-bound record through ``ReviewAuditStore.get_validation_evidence`` (an
exact, read-only, indexed lookup) and copies only allowlisted typed fields into
the dataclasses below; the stored record, native reports and interaction
objects are never serialized wholesale. Every exported text is passed through
the public API's redaction *before* clipping, identities are exact or
explicitly omitted (never rewritten), and the serialized body is fitted to the
public byte bound by shrinking per-collection entry caps while recording the
HTTP clipping separately from capture-time omissions.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple, Union

from .build_provenance import ArtifactField, observe_controller_artifact
from .execution_trace import get_trace_collector
from .public_api import (
    _IDENTIFIER,
    MAX_LIMIT,
    MAX_RESPONSE_BYTES,
    MAX_TEXT_CHARS,
    _dump,
    sanitize_text,
)
from .review_audit import (
    ReviewAuditStore,
    ReviewEffectRecord,
    ValidationEvidenceReadStatus,
    ValidationEvidenceRow,
)

SCHEMA_VERSION = 1
ATTEMPT_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
PR_NUMBER_PATTERN = re.compile(r"[0-9]{1,9}")
SEQUENCE_PATTERN = re.compile(r"[0-9]{1,15}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
# Supported explicit (``REQ-001``), qualified (``#99/REQ-001``, ``owner/repo#99/REQ-001``) and legacy numeric forms.
_REQUIREMENT_ID = re.compile(r"(?:(?:[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100})?#[0-9]{1,9}/)?REQ-[0-9]{1,6}")
_LABEL_CHARS = 200
_ENTRY_CAP_STEPS = (MAX_LIMIT, 250, 100, 40, 10, 3, 0)
_MAX_LISTED_FIELDS = 50

NOTES = (
    "Returned VERIFIED counts, a recorded PASS or a confirmed publication are recorded observations, not accepted requirement coverage, provider liveness, merge permission or current GitHub state.",
    "Fingerprints are SHA-256 comparison values of the exact UTF-8 inputs captured at attempt time, not links to or copies of raw content.",
    "no_retained_match does not establish that the attempt never occurred.",
)


# -- public schema ---------------------------------------------------------------


@dataclass
class OmittedIdentity:
    """An identity that cannot be exported exactly: bounded digest and length only."""

    reason: str
    sha256: Optional[str] = None
    byte_length: Optional[int] = None
    omitted: bool = True


PublicIdentity = Union[str, OmittedIdentity, None]


@dataclass
class Fingerprint:
    state: str = "unavailable"
    sha256: Optional[str] = None
    byte_length: Optional[int] = None


@dataclass
class CountInfo:
    """Original/returned counts that keep capture-time loss apart from HTTP clipping."""

    source_count: Optional[int] = None
    retained_in_source: int = 0
    returned: int = 0
    source_omitted: Optional[int] = None
    http_clipped: int = 0
    incomplete: bool = False


@dataclass
class SectionState:
    availability: str = "available"  # available | not_recorded | omitted | unavailable
    reason: Optional[str] = None
    incomplete: bool = False


@dataclass
class ArtifactView:
    value: Optional[str] = None
    origin: str = "none"
    available: bool = False
    reason: Optional[str] = None


@dataclass
class ProducingArtifact:
    """The controller artifact that produced the record (installed version / embedded revision), not the reviewed head or latest main."""

    availability: str = "unavailable"
    process_run_id: PublicIdentity = None
    distribution_version: ArtifactView = field(default_factory=ArtifactView)
    source_revision: ArtifactView = field(default_factory=ArtifactView)
    observation: str = "recorded_by_producing_process_at_capture"


@dataclass
class ServingMetadata:
    """The process serving this response; never relabels the producing process."""

    sampled_at: float = 0.0
    process_run_id: Optional[str] = None
    distribution_version: ArtifactView = field(default_factory=ArtifactView)
    source_revision: ArtifactView = field(default_factory=ArtifactView)


@dataclass
class AttemptIdentity:
    attempt_id: PublicIdentity = None
    attempt_sequence: Optional[int] = None
    review_id: PublicIdentity = None
    repository: PublicIdentity = None
    pr_number: Optional[int] = None
    head_sha: PublicIdentity = None
    base_sha: PublicIdentity = None
    process_run_id: PublicIdentity = None
    execution_id: PublicIdentity = None
    unavailable: List[str] = field(default_factory=list)
    github_pull_request: Optional[str] = None


@dataclass
class RecordMeta:
    role: Optional[str] = None
    completeness: Optional[str] = None
    unrecorded: List[str] = field(default_factory=list)
    schema_version: Optional[int] = None
    digest_encoding: Optional[str] = None
    manifest_identity_version: Optional[str] = None
    source_updated_at: Optional[str] = None


@dataclass
class IssueInput:
    repository: PublicIdentity = None
    number: Optional[int] = None
    body: Fingerprint = field(default_factory=Fingerprint)
    source_updated_at: Optional[str] = None
    retrieval_mode: Optional[str] = None
    github_issue: Optional[str] = None


@dataclass
class InputSection:
    state: SectionState = field(default_factory=SectionState)
    captured_at: Optional[str] = None
    capture_time_is_not_freshness: bool = True
    pr_body: Optional[Fingerprint] = None
    pr_source_updated_at: Optional[str] = None
    rendered_pr_body: Optional[Fingerprint] = None
    linked_issue_context: Optional[Fingerprint] = None
    resolved_issues: List[IssueInput] = field(default_factory=list)
    resolved_issues_count: CountInfo = field(default_factory=CountInfo)


@dataclass
class ManifestEntry:
    id: PublicIdentity = None
    text: Fingerprint = field(default_factory=Fingerprint)


@dataclass
class ManifestView:
    manifest_id: Optional[str] = None
    role: Optional[str] = None
    mode: Optional[str] = None
    identity_sha256: Optional[str] = None
    validation_snapshot: PublicIdentity = None
    entries: List[ManifestEntry] = field(default_factory=list)
    entries_count: CountInfo = field(default_factory=CountInfo)


@dataclass
class ReturnedEntryView:
    id: PublicIdentity = None
    status: Optional[str] = None


@dataclass
class InteractionView:
    association: Optional[str] = None
    unavailable_reason: Optional[str] = None
    interaction_ids: List[PublicIdentity] = field(default_factory=list)
    interaction_ids_count: CountInfo = field(default_factory=CountInfo)
    backend_alias: Optional[str] = None
    backend_type: Optional[str] = None
    provider_alias: Optional[str] = None
    requested_model: Optional[str] = None
    reported_model: Optional[str] = None


@dataclass
class ParseView:
    state: Optional[str] = None
    failure_category: Optional[str] = None
    parsed_verdict: Optional[str] = None
    post_parse_verdict: Optional[str] = None
    returned_entries_observed: bool = False
    returned_entries: List[ReturnedEntryView] = field(default_factory=list)
    returned_entries_count: CountInfo = field(default_factory=CountInfo)


@dataclass
class ResponseView:
    response_id: Optional[str] = None
    stage: Optional[str] = None
    started_at: Optional[str] = None
    prompt: Fingerprint = field(default_factory=Fingerprint)
    manifest_transmitted: bool = False
    supplied_manifest_id: Optional[str] = None
    continues_manifest_id: Optional[str] = None
    continues_unavailable_reason: Optional[str] = None
    interaction: InteractionView = field(default_factory=InteractionView)
    response: Fingerprint = field(default_factory=Fingerprint)
    response_state: Optional[str] = None
    semantic_payload: Optional[Fingerprint] = None
    parse: ParseView = field(default_factory=ParseView)


@dataclass
class IdSet:
    ids: List[PublicIdentity] = field(default_factory=list)
    count: CountInfo = field(default_factory=CountInfo)


@dataclass
class CoverageCheckView:
    check_id: Optional[str] = None
    response_id: Optional[str] = None
    performed: bool = False
    not_performed_reason: Optional[str] = None
    supplied_manifest_id: Optional[str] = None
    checked_manifest_id: Optional[str] = None
    counts_are_returned_evidence_not_acceptance: bool = True
    expected: IdSet = field(default_factory=IdSet)
    returned: IdSet = field(default_factory=IdSet)
    missing: IdSet = field(default_factory=IdSet)
    duplicate: IdSet = field(default_factory=IdSet)
    unknown: IdSet = field(default_factory=IdSet)
    verdict_before: Optional[str] = None
    verdict_after: Optional[str] = None
    diagnostic_category: Optional[str] = None
    diagnostic_reason: Optional[str] = None
    diagnostic_reason_clipped: bool = False


@dataclass
class FinalView:
    state: SectionState = field(default_factory=SectionState)
    recorded_at: Optional[str] = None
    kind: Optional[str] = None
    verdict: Optional[str] = None
    diagnostic_category: Optional[str] = None
    diagnostic_reason: Optional[str] = None
    diagnostic_reason_clipped: bool = False
    source_response_id: Optional[str] = None


@dataclass
class ManifestsSection:
    state: SectionState = field(default_factory=SectionState)
    items: List[ManifestView] = field(default_factory=list)
    count: CountInfo = field(default_factory=CountInfo)


@dataclass
class ResponsesSection:
    state: SectionState = field(default_factory=SectionState)
    items: List[ResponseView] = field(default_factory=list)
    count: CountInfo = field(default_factory=CountInfo)


@dataclass
class ChecksSection:
    state: SectionState = field(default_factory=SectionState)
    items: List[CoverageCheckView] = field(default_factory=list)
    count: CountInfo = field(default_factory=CountInfo)


@dataclass
class EffectView:
    effect_id: PublicIdentity = None
    review_id: PublicIdentity = None
    observation_time: Optional[str] = None
    disposition: Optional[str] = None


@dataclass
class ReuseView:
    review_id: PublicIdentity = None
    completeness: Optional[str] = None
    source_observed_at: Optional[str] = None
    source_review_id: PublicIdentity = None
    source_known: Optional[bool] = None
    fresh_invocation: Optional[bool] = None


@dataclass
class SourceOmission:
    section: str
    source_count: Optional[int] = None
    retained: Optional[int] = None
    omitted: Optional[int] = None


@dataclass
class SourceLimits:
    """Capture-time loss recorded in the stored attempt (distinct from HTTP clipping)."""

    applied_entry_cap: Optional[int] = None
    applied_text_cap: Optional[int] = None
    omissions: List[SourceOmission] = field(default_factory=list)
    clipped_fields: List[str] = field(default_factory=list)
    incomplete_reason: Optional[str] = None


@dataclass
class HttpLimits:
    """Additional clipping applied only by this response."""

    max_response_bytes: int = MAX_RESPONSE_BYTES
    max_entries_per_collection: int = MAX_LIMIT
    max_text_chars: int = MAX_TEXT_CHARS
    applied_entry_cap: int = MAX_LIMIT
    clipped: bool = False
    clipped_text_fields: List[str] = field(default_factory=list)
    redacted_field_count: int = 0


@dataclass
class Evidence:
    identity: AttemptIdentity = field(default_factory=AttemptIdentity)
    record: RecordMeta = field(default_factory=RecordMeta)
    producing_artifact: ProducingArtifact = field(default_factory=ProducingArtifact)
    input: InputSection = field(default_factory=InputSection)
    manifests: ManifestsSection = field(default_factory=ManifestsSection)
    responses: ResponsesSection = field(default_factory=ResponsesSection)
    coverage_checks: ChecksSection = field(default_factory=ChecksSection)
    final: FinalView = field(default_factory=FinalView)
    effects: List[EffectView] = field(default_factory=list)
    effects_count: CountInfo = field(default_factory=CountInfo)
    reuse_observations: List[ReuseView] = field(default_factory=list)
    reuse_observations_count: CountInfo = field(default_factory=CountInfo)
    source_limits: SourceLimits = field(default_factory=SourceLimits)


@dataclass
class Selection:
    pr_number: int
    attempt_id: Optional[str] = None
    attempt_sequence: Optional[int] = None


@dataclass
class ValidationAttemptResponse:
    repository: str
    selection: Selection
    result: str  # attempt | no_retained_match | evidence_unavailable | audit_not_initialized
    serving: ServingMetadata
    http_limits: HttpLimits
    reason: Optional[str] = None
    evidence: Optional[Evidence] = None
    notes: Tuple[str, ...] = NOTES
    availability: str = "available"
    schema_version: int = SCHEMA_VERSION


@dataclass
class ValidationAttemptUnavailable:
    repository: str
    error_code: str
    message: str
    data: None = None
    schema_version: int = SCHEMA_VERSION


# -- request parsing ---------------------------------------------------------------


class InvalidSelection(Exception):
    """Raised for an invalid selector; carries no caller-supplied text."""


def parse_selection(params: "dict[str, str]") -> Selection:
    """Strictly validate ``pr_number`` and exactly one of ``attempt_id`` / ``attempt_sequence``."""
    pr_raw, id_raw, seq_raw = params.get("pr_number"), params.get("attempt_id"), params.get("attempt_sequence")
    if pr_raw is None or not PR_NUMBER_PATTERN.fullmatch(pr_raw) or int(pr_raw) < 1:
        raise InvalidSelection
    if (id_raw is None) == (seq_raw is None):
        raise InvalidSelection
    if id_raw is not None:
        if not ATTEMPT_ID_PATTERN.fullmatch(id_raw):
            raise InvalidSelection
        return Selection(int(pr_raw), attempt_id=id_raw)
    if seq_raw is None or not SEQUENCE_PATTERN.fullmatch(seq_raw) or int(seq_raw) < 1:
        raise InvalidSelection
    return Selection(int(pr_raw), attempt_sequence=int(seq_raw))


# -- tolerant, typed extraction ------------------------------------------------------


def _dict(value: object) -> "dict[str, Any]":
    return value if isinstance(value, dict) else {}


def _list(value: object) -> List[Any]:
    return value if isinstance(value, list) else []


def _int(value: object) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _bool(value: object) -> Optional[bool]:
    return value if isinstance(value, bool) else None


class _Projector:
    """Projects one stored record under a per-collection entry cap and records HTTP clipping."""

    def __init__(self, repository: str, entry_cap: int):
        self.repository = repository
        self.cap = entry_cap
        self.clipped_text: List[str] = []
        self.redacted = 0
        self.clipped = False

    # -- text and identities ---------------------------------------------------
    def text(self, section: str, value: object, limit: int = MAX_TEXT_CHARS) -> Tuple[Optional[str], bool]:
        if not isinstance(value, str):
            return None, False
        text, filtered, clipped = sanitize_text(value, paths=True)
        if filtered:
            self.redacted += 1
        if len(text) > limit:
            text, clipped = text[:limit], True
        if clipped:
            self.clipped = True
            if len(self.clipped_text) < _MAX_LISTED_FIELDS and section not in self.clipped_text:
                self.clipped_text.append(section)
        return text, clipped

    def label(self, section: str, value: object) -> Optional[str]:
        return self.text(section, value, _LABEL_CHARS)[0]

    def identity(self, value: object, requirement: bool = False) -> PublicIdentity:
        """Exact, or explicitly omitted with digest/length; a changed value is never emitted."""
        if value is None:
            return None
        if isinstance(value, dict):  # an identity the capture already omitted
            sha, length = value.get("sha256"), _int(value.get("byte_length"))
            reason = self.label("identity.reason", value.get("reason")) or "omitted_at_capture"
            return OmittedIdentity(f"source_omitted:{reason}", sha if isinstance(sha, str) and _SHA256.fullmatch(sha) else None, length)
        if not isinstance(value, str):
            return OmittedIdentity("identity_not_text")
        raw = value.encode("utf-8", errors="surrogatepass")
        digest = hashlib.sha256(raw).hexdigest()
        if requirement:
            if len(value) <= _LABEL_CHARS and _REQUIREMENT_ID.fullmatch(value):
                return value
            return OmittedIdentity("not_a_supported_requirement_id", digest, len(raw))
        redacted = sanitize_text(value, paths=True)[0] != value
        if redacted:
            self.redacted += 1
            return OmittedIdentity("redacted", digest, len(raw))
        if len(value) > _LABEL_CHARS:
            return OmittedIdentity("too_long", digest, len(raw))
        if not _IDENTIFIER.fullmatch(value) or value.startswith(("/", "~", ".")) or "://" in value:
            return OmittedIdentity("not_a_safe_identity", digest, len(raw))
        return value

    # -- collections --------------------------------------------------------------
    def capped(self, items: List[Any], source_count: Optional[int]) -> Tuple[List[Any], CountInfo]:
        kept = items[: self.cap]
        retained = len(items)
        original = source_count if source_count is not None and source_count >= retained else retained
        clipped = retained - len(kept)
        if clipped:
            self.clipped = True
        return kept, CountInfo(
            source_count=original,
            retained_in_source=retained,
            returned=len(kept),
            source_omitted=original - retained,
            http_clipped=clipped,
            incomplete=original > len(kept),
        )

    def fingerprint(self, raw: object) -> Optional[Fingerprint]:
        if not isinstance(raw, dict):
            return None
        sha = raw.get("sha256")
        state: str = raw["state"] if raw.get("state") in ("present", "empty", "unavailable") else "unavailable"
        return Fingerprint(state, sha if isinstance(sha, str) and _SHA256.fullmatch(sha) else None, _int(raw.get("byte_length")))

    def ids(self, raw: object, source_count: Optional[int]) -> IdSet:
        kept, count = self.capped(_list(raw), source_count)
        return IdSet([self.identity(item, requirement=True) for item in kept], count)

    def artifact(self, raw: object) -> ArtifactView:
        data = _dict(raw)
        if not data:
            return ArtifactView(reason="not_recorded")
        return ArtifactView(self.label("artifact.value", data.get("value")), self.label("artifact.origin", data.get("origin")) or "none", data.get("available") is True, self.label("artifact.reason", data.get("reason")))

    # -- sections -----------------------------------------------------------------
    def build(self, payload: "dict[str, Any]") -> ProducingArtifact:
        build = _dict(payload.get("build"))
        if not build:
            return ProducingArtifact(availability="unavailable", distribution_version=ArtifactView(reason="build_not_recorded"), source_revision=ArtifactView(reason="build_not_recorded"))
        return ProducingArtifact("recorded", self.identity(build.get("process_run_id")), self.artifact(build.get("distribution_version")), self.artifact(build.get("source_revision")))

    def input(self, payload: "dict[str, Any]", source_reason: Optional[str]) -> InputSection:
        raw = payload.get("input")
        if not isinstance(raw, dict):
            reason = source_reason or ("input_not_yet_recorded" if "input" in _list(payload.get("unrecorded")) else "input_unavailable")
            return InputSection(SectionState("omitted" if source_reason else "not_recorded", reason, True), pr_body=None)
        issues, count = self.capped([i for i in _list(raw.get("resolved_issues")) if isinstance(i, dict)], _int(raw.get("resolved_issue_count")))
        views = []
        for issue in issues:
            repository = self.identity(issue.get("repository"))
            number = _int(issue.get("number"))
            link = f"https://github.com/{repository}/issues/{number}" if isinstance(repository, str) and repository.casefold() == self.repository.casefold() and number else None
            views.append(
                IssueInput(
                    repository,
                    number,
                    self.fingerprint(issue.get("body")) or Fingerprint(),
                    self.label("input.source_updated_at", issue.get("source_updated_at")),
                    self.label("input.retrieval_mode", issue.get("retrieval_mode")),
                    link,
                )
            )
        return InputSection(
            SectionState("available", None, count.incomplete),
            self.label("input.captured_at", raw.get("captured_at")),
            True,
            self.fingerprint(raw.get("pr_body")),
            self.label("input.pr_source_updated_at", raw.get("pr_source_updated_at")),
            self.fingerprint(raw.get("rendered_pr_body")),
            self.fingerprint(raw.get("linked_issue_context")),
            views,
            count,
        )

    def manifest(self, raw: "dict[str, Any]") -> ManifestView:
        entries, count = self.capped([e for e in _list(raw.get("entries")) if isinstance(e, dict)], _int(raw.get("count")))
        snapshot = raw.get("validation_snapshot")
        sha = raw.get("identity_sha256")
        return ManifestView(
            self.label("manifest.id", raw.get("manifest_id")),
            self.label("manifest.role", raw.get("role")),
            self.label("manifest.mode", raw.get("mode")),
            sha if isinstance(sha, str) and _SHA256.fullmatch(sha) else None,
            self.identity(snapshot),
            [ManifestEntry(self.identity(e.get("id"), requirement=True), self.fingerprint(e.get("text")) or Fingerprint()) for e in entries],
            count,
        )

    def response(self, raw: "dict[str, Any]") -> ResponseView:
        interaction, parse = _dict(raw.get("interaction")), _dict(raw.get("parse"))
        interaction_ids, interaction_count = self.capped(_list(interaction.get("interaction_ids")), None)
        returned, returned_count = self.capped([e for e in _list(parse.get("returned_entries")) if isinstance(e, dict)], _int(parse.get("returned_entry_count")))
        return ResponseView(
            self.label("response.id", raw.get("response_id")),
            self.label("response.stage", raw.get("stage")),
            self.label("response.started_at", raw.get("started_at")),
            self.fingerprint(raw.get("prompt")) or Fingerprint(),
            raw.get("manifest_transmitted") is True,
            self.label("response.supplied_manifest_id", raw.get("supplied_manifest_id")),
            self.label("response.continues_manifest_id", raw.get("continues_manifest_id")),
            self.label("response.continues_unavailable_reason", raw.get("continues_unavailable_reason")),
            InteractionView(
                self.label("interaction.association", interaction.get("association")),
                self.label("interaction.unavailable_reason", interaction.get("unavailable_reason")),
                [self.identity(i) for i in interaction_ids],
                interaction_count,
                self.label("interaction.backend_alias", interaction.get("backend_alias")),
                self.label("interaction.backend_type", interaction.get("backend_type")),
                self.label("interaction.provider_alias", interaction.get("provider_alias")),
                self.label("interaction.requested_model", interaction.get("requested_model")),
                self.label("interaction.reported_model", interaction.get("reported_model")),
            ),
            self.fingerprint(raw.get("response")) or Fingerprint(),
            self.label("response.state", raw.get("response_state")),
            self.fingerprint(raw.get("semantic_payload")),
            ParseView(
                self.label("parse.state", parse.get("state")),
                self.label("parse.failure_category", parse.get("failure_category")),
                self.label("parse.parsed_verdict", parse.get("parsed_verdict")),
                self.label("parse.post_parse_verdict", parse.get("post_parse_verdict")),
                parse.get("returned_entries_observed") is True,
                [ReturnedEntryView(self.identity(e.get("id"), requirement=True), self.label("parse.returned_status", e.get("status"))) for e in returned],
                returned_count,
            ),
        )

    def check(self, raw: "dict[str, Any]") -> CoverageCheckView:
        reason, reason_clipped = self.text("check.diagnostic_reason", raw.get("diagnostic_reason"))
        return CoverageCheckView(
            self.label("check.id", raw.get("check_id")),
            self.label("check.response_id", raw.get("response_id")),
            raw.get("performed") is True,
            self.label("check.not_performed_reason", raw.get("not_performed_reason")),
            self.label("check.supplied_manifest_id", raw.get("supplied_manifest_id")),
            self.label("check.checked_manifest_id", raw.get("checked_manifest_id")),
            True,
            self.ids(raw.get("expected_ids"), _int(raw.get("expected_count"))),
            self.ids(raw.get("returned_ids"), _int(raw.get("returned_count"))),
            self.ids(raw.get("missing_ids"), _int(raw.get("missing_count"))),
            self.ids(raw.get("duplicate_ids"), _int(raw.get("duplicate_count"))),
            self.ids(raw.get("unknown_ids"), _int(raw.get("unknown_count"))),
            self.label("check.verdict_before", raw.get("verdict_before")),
            self.label("check.verdict_after", raw.get("verdict_after")),
            self.label("check.diagnostic_category", raw.get("diagnostic_category")),
            reason,
            reason_clipped,
        )

    def collection(self, payload: "dict[str, Any]", key: str, build: Any, section: Any, absent_marker: str, source_reason: Optional[str]) -> Any:
        raw = payload.get(key)
        if not isinstance(raw, list):
            omitted = source_reason is not None
            return section(SectionState("omitted" if omitted else "unavailable", source_reason or f"{key}_unavailable", True))
        kept, count = self.capped([item for item in raw if isinstance(item, dict)], None)
        unrecorded = absent_marker in _list(payload.get("unrecorded")) and not raw
        state = SectionState("not_recorded" if unrecorded else "available", f"{key}_not_yet_recorded" if unrecorded else None, count.incomplete or unrecorded)
        return section(state, [build(item) for item in kept], count)

    def final(self, payload: "dict[str, Any]", source_reason: Optional[str]) -> FinalView:
        raw = payload.get("final")
        if not isinstance(raw, dict):
            if source_reason:
                return FinalView(SectionState("omitted", source_reason, True))
            pending = "final_result" in _list(payload.get("unrecorded"))
            return FinalView(SectionState("not_recorded" if pending else "unavailable", "final_result_not_yet_recorded" if pending else "final_unavailable", True))
        reason, reason_clipped = self.text("final.diagnostic_reason", raw.get("diagnostic_reason"))
        return FinalView(
            SectionState(),
            self.label("final.recorded_at", raw.get("recorded_at")),
            self.label("final.kind", raw.get("kind")),
            self.label("final.verdict", raw.get("verdict")),
            self.label("final.diagnostic_category", raw.get("diagnostic_category")),
            reason,
            reason_clipped,
            self.label("final.source_response_id", raw.get("source_response_id")),
        )

    def source_limits(self, payload: "dict[str, Any]") -> SourceLimits:
        limits = _dict(payload.get("limits"))
        omissions = [SourceOmission(self.label("source_omission.section", o.get("section")) or "", _int(o.get("source_count")), _int(o.get("retained")), _int(o.get("omitted"))) for o in _list(limits.get("omissions"))[:_MAX_LISTED_FIELDS] if isinstance(o, dict)]
        clipped = [text for text in (self.label("source_clipped_field", c) for c in _list(limits.get("clipped_fields"))[:_MAX_LISTED_FIELDS]) if text]
        return SourceLimits(_int(limits.get("applied_entry_cap")), _int(limits.get("applied_text_cap")), omissions, clipped, self.label("source_incomplete_reason", limits.get("incomplete_reason")))


def _attempt_identity(projector: _Projector, row: ValidationEvidenceRow, payload: "dict[str, Any]", pr_number: int) -> AttemptIdentity:
    recorded = _dict(payload.get("identity"))
    unavailable = [name for name in (projector.label("identity.unavailable", n) for n in _list(recorded.get("unavailable"))[:_MAX_LISTED_FIELDS]) if name]
    return AttemptIdentity(
        projector.identity(row.attempt_id),
        _int(row.attempt_sequence),
        projector.identity(row.review_id),
        projector.identity(row.repository),
        pr_number,
        projector.identity(recorded.get("head_sha")),
        projector.identity(recorded.get("base_sha")),
        projector.identity(recorded.get("process_run_id")),
        projector.identity(recorded.get("execution_id")),
        unavailable,
        f"https://github.com/{projector.repository}/pull/{pr_number}",
    )


def _evidence(projector: _Projector, producer: Optional[ValidationEvidenceRow], reuse: List[ValidationEvidenceRow], effects: List[ReviewEffectRecord], pr_number: int) -> Evidence:
    anchor = producer if producer is not None else reuse[0]
    payload = producer.payload if producer is not None else {}
    source = projector.source_limits(payload)
    source_reason = source.incomplete_reason
    producer_missing = None if producer is not None else "producer_record_not_retained"
    meta = RecordMeta(
        projector.label("record.role", payload.get("role") or anchor.role),
        projector.label("record.completeness", anchor.completeness),
        [item for item in (projector.label("record.unrecorded", u) for u in _list(payload.get("unrecorded"))[:_MAX_LISTED_FIELDS]) if item],
        _int(anchor.schema_version),
        projector.label("record.digest_encoding", payload.get("digest_encoding")),
        projector.label("record.manifest_identity_version", payload.get("manifest_identity_version")),
        projector.label("record.updated_at", anchor.updated_at),
    )
    evidence = Evidence(identity=_attempt_identity(projector, anchor, payload, pr_number), record=meta, source_limits=source)
    if producer is None:
        reason = SectionState("unavailable", producer_missing, True)
        evidence.input = InputSection(SectionState("unavailable", producer_missing, True))
        evidence.manifests, evidence.responses, evidence.coverage_checks = ManifestsSection(reason), ResponsesSection(reason), ChecksSection(reason)
        evidence.final = FinalView(reason)
    else:
        evidence.producing_artifact = projector.build(payload)
        evidence.input = projector.input(payload, source_reason)
        evidence.manifests = projector.collection(payload, "manifests", projector.manifest, ManifestsSection, "manifests", source_reason)
        evidence.responses = projector.collection(payload, "responses", projector.response, ResponsesSection, "response", source_reason)
        evidence.coverage_checks = projector.collection(payload, "coverage_checks", projector.check, ChecksSection, "coverage_check", source_reason)
        evidence.final = projector.final(payload, source_reason)
    effect_rows, evidence.effects_count = projector.capped(list(effects), None)
    evidence.effects = [EffectView(projector.identity(e.effect_id), projector.identity(e.review_id), projector.label("effect.observation_time", e.observation_time), projector.label("effect.disposition", e.disposition)) for e in effect_rows]
    reuse_rows, evidence.reuse_observations_count = projector.capped(list(reuse), None)
    evidence.reuse_observations = []
    for row in reuse_rows:
        info = _dict(row.payload.get("reuse"))
        evidence.reuse_observations.append(
            ReuseView(projector.identity(row.review_id), projector.label("reuse.completeness", row.completeness), projector.label("reuse.updated_at", row.updated_at), projector.identity(info.get("source_review_id")), _bool(info.get("source_known")), _bool(info.get("fresh_invocation")))
        )
    return evidence


# -- observation ----------------------------------------------------------------------


def _artifact_view(field_: ArtifactField) -> ArtifactView:
    value = field_.value if isinstance(field_.value, str) else None
    return ArtifactView(sanitize_text(value)[0] if value is not None else None, field_.origin, field_.available, field_.reason)


def _serving() -> ServingMetadata:
    build = observe_controller_artifact()
    return ServingMetadata(time.time(), get_trace_collector().process_run_id, _artifact_view(build.distribution_version), _artifact_view(build.source_revision))


class ObservationFailed(Exception):
    """Evidence could not be read or projected; carries no source detail."""


def build_validation_attempt(store: ReviewAuditStore, repo_name: str, selection: Selection) -> bytes:
    """Read the exact attempt and return the serialized, byte-bounded public body.

    Raises ``ObservationFailed`` for an unreadable, corrupt, unsupported or
    unprojectable source; never falls back to another attempt.
    """
    found = store.get_validation_evidence(repo_name, str(selection.pr_number), attempt_id=selection.attempt_id, attempt_sequence=selection.attempt_sequence)
    status = found.status
    serving = _serving()
    if status in (ValidationEvidenceReadStatus.UNAVAILABLE, ValidationEvidenceReadStatus.CORRUPT, ValidationEvidenceReadStatus.UNSUPPORTED_SCHEMA):
        raise ObservationFailed
    if status != ValidationEvidenceReadStatus.AVAILABLE:
        result, reason = {
            ValidationEvidenceReadStatus.NOT_FOUND: ("no_retained_match", None),
            ValidationEvidenceReadStatus.PRE_FEATURE: ("evidence_unavailable", "pre_feature_audit_record"),
            ValidationEvidenceReadStatus.UNINITIALIZED: ("audit_not_initialized", "audit_source_not_initialized"),
        }[status]
        return _dump(ValidationAttemptResponse(repo_name, selection, result, serving, HttpLimits(), reason))
    producer = found.producer
    reuse = list(found.reuse_observations)
    rows = ([producer] if producer is not None else []) + reuse
    if not rows or any(row.repository.casefold() != repo_name.casefold() or row.pr_number != str(selection.pr_number) for row in rows):
        raise ObservationFailed
    for cap in _ENTRY_CAP_STEPS:
        projector = _Projector(repo_name, cap)
        evidence = _evidence(projector, producer, reuse, list(found.effects), selection.pr_number)
        limits = HttpLimits(applied_entry_cap=cap, clipped=projector.clipped, clipped_text_fields=projector.clipped_text, redacted_field_count=projector.redacted)
        body = _dump(ValidationAttemptResponse(repo_name, selection, "attempt", serving, limits, evidence=evidence))
        if len(body) <= MAX_RESPONSE_BYTES:
            return body
    raise ObservationFailed
