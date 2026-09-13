"""Pure, provider-independent review adjudication protocol.

The module intentionally performs no GitHub or repair side effects.  Callers
must supply authoritative GitHub identities, relationships, and revisions.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Sequence

MARKER = "<!-- auto-coder-review-adjudication:v1 -->"
SERIALIZATION_VERSION = "review-adjudication-state:v1"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ENVELOPE = re.compile(r"^<!-- auto-coder-review-adjudication:v1 -->\s*\n```json\n([\s\S]*?)\n```\s*$")
_KEYS = {"decision_id", "context_id", "head_sha", "contract_digest", "verdict", "directive", "supersedes", "rationale", "source"}
_PAIRS = {("UPHOLD", "FIX"), ("OVERRULE", "NO_CHANGE"), ("UNDECIDED", "NONE")}


class AdjudicationStatus(str, Enum):
    NONE = "NONE"
    APPLICABLE = "APPLICABLE"
    UNDECIDED = "UNDECIDED"
    CONFLICT = "CONFLICT"
    STALE = "STALE"
    REVOKED = "REVOKED"
    INVALID = "INVALID"
    UNAUTHORIZED = "UNAUTHORIZED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"


@dataclass(frozen=True)
class Requirement:
    id: str
    text: str


@dataclass(frozen=True)
class IssueContract:
    issue_id: int
    issue_number: int
    requirements: tuple[Requirement, ...]
    objective_text: str


@dataclass(frozen=True)
class Decision:
    decision_id: str
    context_id: str
    head_sha: str
    contract_digest: str
    verdict: str
    directive: str
    supersedes: tuple[str, ...]
    rationale: str
    source: str


@dataclass(frozen=True)
class SourceComment:
    repository_id: int
    repository: str
    pr_number: int
    thread_id: str
    root_comment_id: int
    comment_id: int
    author_id: int
    created_at: str
    update_revision: str
    raw_body: str
    is_reply: bool = True


@dataclass(frozen=True)
class DecisionRecord:
    decision: Decision
    source: SourceComment
    body_hash: str


@dataclass
class ReviewContext:
    context_id: str
    repository_id: int
    repository: str
    pr_number: int
    thread_id: str
    root_comment_id: int
    root_author_id: int
    root_actor_type: str
    root_body_hash: str
    root_update_revision: str
    head_sha: str
    base_sha: str
    base_ref: str
    parser_version: str
    contracts: tuple[IssueContract, ...]
    contract_digest: str
    objective_fingerprints: tuple[str, ...]
    retired_reason: Optional[str] = None
    decisions: dict[str, DecisionRecord] = field(default_factory=dict)
    tombstones: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AdjudicationResult:
    status: AdjudicationStatus
    context_id: Optional[str]
    repository: Optional[str]
    pr_number: Optional[int]
    actual_actor_id: Optional[int]
    source_comment_id: Optional[int]
    decision_id: Optional[str]
    tips: tuple[str, ...]
    reason: str
    verdict: Optional[str] = None
    directive: Optional[str] = None


def _pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def parse_decision(body: str) -> Decision:
    """Parse the literal v1 envelope without interpreting embedded examples."""
    match = _ENVELOPE.fullmatch(body)
    if not match:
        raise ValueError("body is not an exact v1 adjudication envelope")
    try:
        value = json.loads(match.group(1), object_pairs_hook=_pairs_hook)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid adjudication JSON") from exc
    if not isinstance(value, dict) or set(value) != _KEYS:
        raise ValueError("adjudication fields must exactly match the v1 schema")
    if not isinstance(value["decision_id"], str) or not _UUID.fullmatch(value["decision_id"]):
        raise ValueError("decision_id must be a canonical lowercase UUID")
    if not isinstance(value["context_id"], str) or not _UUID.fullmatch(value["context_id"]):
        raise ValueError("context_id must be a canonical lowercase UUID")
    if not isinstance(value["head_sha"], str) or not _SHA40.fullmatch(value["head_sha"]):
        raise ValueError("head_sha must be a full lowercase commit SHA")
    if not isinstance(value["contract_digest"], str) or not _SHA256.fullmatch(value["contract_digest"]):
        raise ValueError("contract_digest must be lowercase SHA-256")
    pair = (value["verdict"], value["directive"])
    if pair not in _PAIRS:
        raise ValueError("unsupported verdict/directive pair")
    supersedes = value["supersedes"]
    if not isinstance(supersedes, list) or any(not isinstance(item, str) or not _UUID.fullmatch(item) for item in supersedes) or len(set(supersedes)) != len(supersedes):
        raise ValueError("supersedes must contain distinct canonical UUIDs")
    if not isinstance(value["rationale"], str) or not value["rationale"].strip():
        raise ValueError("rationale must be nonblank")
    if value["source"] not in {"chatgpt-assisted", "dashboard"}:
        raise ValueError("unsupported adjudication source")
    return Decision(
        decision_id=value["decision_id"], context_id=value["context_id"], head_sha=value["head_sha"], contract_digest=value["contract_digest"], verdict=str(value["verdict"]), directive=str(value["directive"]), supersedes=tuple(supersedes), rationale=value["rationale"], source=value["source"]
    )


def render_decision(decision: Decision) -> str:
    payload = asdict(decision)
    payload["supersedes"] = list(decision.supersedes)
    return f"{MARKER}\n```json\n{json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}\n```"


def contract_identity(contracts: Sequence[IssueContract], parser_version: str) -> tuple[str, str, tuple[str, ...]]:
    """Return canonical JSON, its digest, and byte-exact Objective fingerprints."""
    if not parser_version or not contracts:
        raise ValueError("complete contracts and parser_version are required")
    ids: set[int] = set()
    issues = []
    objectives = []
    for contract in sorted(contracts, key=lambda item: item.issue_id):
        if contract.issue_id <= 0 or contract.issue_number <= 0 or contract.issue_id in ids or not isinstance(contract.objective_text, str):
            raise ValueError("contracts must have unique positive identities and complete Objectives")
        ids.add(contract.issue_id)
        req_ids = [req.id for req in contract.requirements]
        if not req_ids or len(req_ids) != len(set(req_ids)) or any(not req.id or not req.text for req in contract.requirements):
            raise ValueError("contracts must have complete, unique requirements")
        issues.append({"issue_id": contract.issue_id, "issue_number": contract.issue_number, "requirements": [{"id": req.id, "text": req.text} for req in contract.requirements]})
        objectives.append(hashlib.sha256(contract.objective_text.encode("utf-8")).hexdigest())
    canonical = json.dumps({"issues": issues, "parser_version": parser_version}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest(), tuple(objectives)


class AdjudicationLedger:
    """Deterministic decision graph with permanent fail-closed retirement."""

    def __init__(self, context: ReviewContext) -> None:
        self.context = context

    @staticmethod
    def _order(source: SourceComment) -> tuple[datetime, int]:
        try:
            instant = datetime.fromisoformat(source.created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("created_at must be an ISO-8601 instant") from exc
        return instant, source.comment_id

    def retire(self, reason: str) -> None:
        if self.context.retired_reason is None:
            self.context.retired_reason = reason
            self.context.tombstones.append(reason)

    def ingest(self, source: SourceComment, adjudicator_ids: Sequence[int], root_reviewer_ids: Sequence[int]) -> AdjudicationResult:
        body_hash = hashlib.sha256(source.raw_body.encode("utf-8")).hexdigest()
        physical = next((record for record in self.context.decisions.values() if record.source.comment_id == source.comment_id), None)
        if physical is not None:
            if physical.source.update_revision == source.update_revision and physical.body_hash == body_hash:
                return self.current(source, physical.decision, "same immutable activity")
            self.retire(f"accepted source {source.comment_id} was edited")
            return self._result(AdjudicationStatus.INVALID, source, physical.decision, self.context.retired_reason or "invalidated")
        try:
            decision = parse_decision(source.raw_body)
        except ValueError as exc:
            return AdjudicationResult(
                AdjudicationStatus.INVALID,
                self.context.context_id,
                self.context.repository,
                self.context.pr_number,
                source.author_id,
                source.comment_id,
                None,
                self.tips(),
                str(exc),
            )
        if self.context.retired_reason:
            return self._result(AdjudicationStatus.INVALID, source, decision, self.context.retired_reason)
        exact_source = (source.repository_id, source.repository, source.pr_number, source.thread_id, source.root_comment_id)
        exact_context = (self.context.repository_id, self.context.repository, self.context.pr_number, self.context.thread_id, self.context.root_comment_id)
        if not source.is_reply or exact_source != exact_context:
            return self._result(AdjudicationStatus.UNAUTHORIZED, source, decision, "source is not a reply in the registered root thread")
        if self.context.root_actor_type != "Bot" or source.author_id not in adjudicator_ids or self.context.root_author_id not in root_reviewer_ids:
            return self._result(AdjudicationStatus.UNAUTHORIZED, source, decision, "author or automated root is not authorized")
        if decision.context_id != self.context.context_id or decision.head_sha != self.context.head_sha or decision.contract_digest != self.context.contract_digest:
            return self._result(AdjudicationStatus.STALE, source, decision, "decision does not match the registered context revision")
        existing = self.context.decisions.get(decision.decision_id)
        if existing:
            self.retire(f"decision identity collision for {decision.decision_id}")
            return self._result(AdjudicationStatus.INVALID, source, decision, self.context.retired_reason or "invalidated")
        for predecessor_id in decision.supersedes:
            predecessor = self.context.decisions.get(predecessor_id)
            if predecessor is None or self._order(predecessor.source) >= self._order(source):
                return self._result(AdjudicationStatus.INVALID, source, decision, "predecessor is missing or not earlier in authoritative source order")
        self.context.decisions[decision.decision_id] = DecisionRecord(decision, source, body_hash)
        return self.current(source, decision, "decision accepted")

    def observe_deletion(self, comment_id: int) -> None:
        if any(record.source.comment_id == comment_id for record in self.context.decisions.values()):
            self.retire(f"accepted source {comment_id} was confirmed deleted")

    def reconcile(
        self, *, available: bool, root_body_hash: str, root_update_revision: str, root_author_id: int, root_actor_type: str, head_sha: str, base_sha: str, base_ref: str, contract_digest: str, objective_fingerprints: Sequence[str], root_reviewer_ids: Sequence[int], adjudicator_ids: Sequence[int]
    ) -> AdjudicationResult:
        if not available:
            return self._result(AdjudicationStatus.SOURCE_UNAVAILABLE, None, None, "required authoritative evidence is unavailable")
        if self.context.retired_reason:
            return self._result(AdjudicationStatus.INVALID, None, None, self.context.retired_reason)
        current = (root_body_hash, root_update_revision, root_author_id, root_actor_type, head_sha, base_sha, base_ref, contract_digest, tuple(objective_fingerprints))
        bound = (self.context.root_body_hash, self.context.root_update_revision, self.context.root_author_id, self.context.root_actor_type, self.context.head_sha, self.context.base_sha, self.context.base_ref, self.context.contract_digest, self.context.objective_fingerprints)
        if current != bound:
            self.retire("a bound context revision changed")
            return self._result(AdjudicationStatus.STALE, None, None, self.context.retired_reason or "stale")
        tips = self.tips()
        tip_authors = {self.context.decisions[item].source.author_id for item in tips}
        if self.context.root_author_id not in root_reviewer_ids or not tip_authors.issubset(set(adjudicator_ids)):
            self.retire("authorization of the root or a current tip author was revoked")
            return self._result(AdjudicationStatus.REVOKED, None, None, self.context.retired_reason or "revoked")
        return self.current(None, None, "authoritative context reconciled")

    def tips(self) -> tuple[str, ...]:
        superseded = {item for record in self.context.decisions.values() for item in record.decision.supersedes}
        return tuple(sorted((item for item in self.context.decisions if item not in superseded), key=lambda item: self._order(self.context.decisions[item].source)))

    def current(self, source: Optional[SourceComment], decision: Optional[Decision], reason: str) -> AdjudicationResult:
        tips = self.tips()
        if not tips:
            return self._result(AdjudicationStatus.NONE, source, decision, reason)
        if len(tips) > 1:
            return self._result(AdjudicationStatus.CONFLICT, source, decision, reason)
        tip = self.context.decisions[tips[0]].decision
        status = AdjudicationStatus.UNDECIDED if tip.verdict == "UNDECIDED" else AdjudicationStatus.APPLICABLE
        return self._result(status, source, tip, reason, tip.verdict, tip.directive)

    def _result(self, status: AdjudicationStatus, source: Optional[SourceComment], decision: Optional[Decision], reason: str, verdict: Optional[str] = None, directive: Optional[str] = None) -> AdjudicationResult:
        return AdjudicationResult(status, self.context.context_id, self.context.repository, self.context.pr_number, source.author_id if source else None, source.comment_id if source else None, decision.decision_id if decision else None, self.tips(), reason, verdict, directive)

    def dumps(self) -> str:
        return json.dumps({"version": SERIALIZATION_VERSION, "context": asdict(self.context)}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def loads(cls, raw: str) -> "AdjudicationLedger":
        try:
            value = json.loads(raw, object_pairs_hook=_pairs_hook)
            if not isinstance(value, dict) or set(value) != {"version", "context"} or value["version"] != SERIALIZATION_VERSION or not isinstance(value["context"], dict):
                raise ValueError
            data = value["context"]
            contracts = tuple(IssueContract(item["issue_id"], item["issue_number"], tuple(Requirement(**req) for req in item["requirements"]), item["objective_text"]) for item in data.pop("contracts"))
            decisions = {key: DecisionRecord(Decision(**{**item["decision"], "supersedes": tuple(item["decision"]["supersedes"])}), SourceComment(**item["source"]), item["body_hash"]) for key, item in data.pop("decisions").items()}
            data["objective_fingerprints"] = tuple(data["objective_fingerprints"])
            context = ReviewContext(contracts=contracts, decisions=decisions, **data)
            # Recompute durable identities so corrupt history cannot authorize.
            _, digest, objectives = contract_identity(context.contracts, context.parser_version)
            if digest != context.contract_digest or objectives != context.objective_fingerprints or any(key != record.decision.decision_id for key, record in decisions.items()):
                raise ValueError
            return cls(context)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("corrupt review adjudication history") from exc
