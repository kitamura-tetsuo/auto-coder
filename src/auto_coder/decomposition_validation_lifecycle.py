"""Durable authorization for implementation-ready parent Issue sets."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence

from .decomposition_analyzer import (
    DECOMPOSITION_FINDING_CATEGORIES,
    AffectedIssue,
    DecompositionAnalysisResult,
    DecompositionFinding,
    DecompositionIssue,
    DecompositionReviewEvidence,
    analyze_issue_decomposition,
    decomposition_review_evidence,
    objective_integrity_result,
)
from .github_pending_work import WorkIdentity, get_pending_work_store
from .issue_review_publication import find_confirmed_publication
from .objective_evidence import ObjectiveAnchorStore
from .prompt_loader import load_prompts
from .reissue_required_store import ReissueRequiredStore
from .role_structural_assessment import ROLE_IMPLEMENTATION_CHILD, ROLE_TRACKING_PARENT, assess_role_structure
from .runtime_locks import ensure_lock_directory, lock_path
from .specification_repair_rounds import RepairRoundApplication, SpecificationRepairRoundStore
from .specification_validation_lifecycle import (
    DIAGNOSTIC_EFFECT,
    READINESS_WITHDRAWAL_EFFECT,
    publication_trusted_complete,
    specification_digest,
)
from .util.gh_cache import IMPLEMENTATION_READY_LABEL, is_implementation_ready
from .util.github_request_outcome import GitHubRequestError

DECOMPOSITION_SCHEMA_VERSION = "issue-decomposition-validation-v5-distinct-objectives"
DECOMPOSITION_FINDINGS_MARKER = "auto-coder-decomposition-validation"

# Pending-work stage for the decomposition-route publication effects (Issue
# #2026, REQ-007): mirrors ``VALIDATION_PUBLICATION_STAGE`` in
# ``specification_validation_lifecycle`` but reconstructs a
# ``DecompositionIdentity`` on recovery instead of an individual
# ``ValidationIdentity``, so the two routes cannot collide on the same
# pending-work key even for the same parent Issue number.
DECOMPOSITION_PUBLICATION_STAGE = "decomposition-validation-publication"


def decomposition_publication_identity(repository: str, parent_issue_number: int, decision_identity_key: str) -> WorkIdentity:
    """Durable obligation identity for one decomposition BLOCKED decision's publication effects."""
    return WorkIdentity(repository, f"issue:{parent_issue_number}", DECOMPOSITION_PUBLICATION_STAGE, decision_identity_key)


@dataclass(frozen=True)
class SetMemberIdentity:
    issue_number: int
    issue_id: int
    specification_digest: str


@dataclass(frozen=True)
class DecompositionIdentity:
    repository: str
    parent: SetMemberIdentity
    children: tuple[SetMemberIdentity, ...]
    policy_identity: str

    @property
    def key(self) -> str:
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DecompositionDecision:
    identity: DecompositionIdentity
    verdict: str
    findings: tuple[DecompositionFinding, ...] = ()
    findings_published: bool = False
    readiness_removed: bool = False
    remediation: str = "NONE"
    remediation_reason: Optional[str] = None
    # Publication provenance (Issue #2026, REQ-008); see ValidationDecision
    # for the exact legacy/new-format completion semantics this mirrors.
    publication_schema_version: int = 0
    publication_receipt: Optional[dict] = None


def decomposition_policy_identity(provider_identity: str) -> str:
    issue_prompts = load_prompts().get("issue")
    prompt = issue_prompts.get("adversarial_decomposition_analysis") if isinstance(issue_prompts, dict) else None
    contract = {
        "version": DECOMPOSITION_SCHEMA_VERSION,
        "prompt": prompt,
        "categories": sorted(DECOMPOSITION_FINDING_CATEGORIES),
        "result_fields": ["verdict", "remediation", "findings", "category", "affected_issues", "issue_number", "requirement_ids", "explanation", "clarification"],
        "provider": provider_identity,
    }
    return hashlib.sha256(json.dumps(contract, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class DecompositionValidationStore:
    def __init__(self, repository: str, path: Optional[Path] = None) -> None:
        root = Path(os.environ.get("AUTO_CODER_SPECIFICATION_VALIDATION_ROOT", Path.home() / ".auto-coder"))
        self.path = path or root / repository / "decomposition_validations.json"
        self.repository = repository

    def _read(self) -> dict[str, object]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    @contextmanager
    def locked(self, key: str) -> Iterator[None]:
        import fcntl

        runtime_path = lock_path(self.repository, self.path, "decomposition-validation", key)
        lock_name = str(runtime_path)
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(lock_name, threading.Lock())
        with lock:
            ensure_lock_directory(runtime_path)
            with runtime_path.open("a", encoding="utf-8") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def get(self, identity: DecompositionIdentity) -> Optional[DecompositionDecision]:
        raw = self._read().get(identity.key)
        serialized_identity = json.loads(json.dumps(asdict(identity)))
        if not isinstance(raw, dict) or raw.get("identity") != serialized_identity or raw.get("verdict") not in {"READY", "BLOCKED"}:
            return None
        findings = tuple(
            DecompositionFinding(
                category=str(item["category"]),
                affected_issues=tuple(AffectedIssue(int(affected["issue_number"]), tuple(affected["requirement_ids"])) for affected in item["affected_issues"]),
                explanation=str(item["explanation"]),
                clarification=str(item["clarification"]),
            )
            for item in raw.get("findings", [])
            if isinstance(item, dict)
        )
        raw_receipt = raw.get("publication_receipt")
        publication_receipt = raw_receipt if isinstance(raw_receipt, dict) else None
        return DecompositionDecision(
            identity,
            str(raw["verdict"]),
            findings,
            bool(raw.get("findings_published")),
            bool(raw.get("readiness_removed")),
            str(raw.get("remediation", "NONE")),
            raw.get("remediation_reason") if isinstance(raw.get("remediation_reason"), str) else None,
            int(raw.get("publication_schema_version") or 0),
            publication_receipt,
        )

    def save(self, decision: DecompositionDecision) -> None:
        if decision.verdict not in {"READY", "BLOCKED"}:
            raise ValueError("ERROR decomposition decisions must not be persisted")
        with self.locked("repository-state"):
            state = self._read()
            state[decision.identity.key] = {
                "identity": asdict(decision.identity),
                "verdict": decision.verdict,
                "findings": [asdict(finding) for finding in decision.findings],
                "findings_published": decision.findings_published,
                "readiness_removed": decision.readiness_removed,
                "remediation": decision.remediation,
                "remediation_reason": decision.remediation_reason,
                "publication_schema_version": decision.publication_schema_version,
                "publication_receipt": decision.publication_receipt,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
            temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(temporary, self.path)


class DecompositionReviewHistoryStore:
    """Atomic immutable baseline and applied BLOCKED history per parent number."""

    def __init__(self, repository: str, path: Optional[Path] = None) -> None:
        root = Path(os.environ.get("AUTO_CODER_SPECIFICATION_VALIDATION_ROOT", Path.home() / ".auto-coder"))
        self.path = path or root / repository / "decomposition_review_history.json"
        self.repository = repository

    def _read(self) -> dict[str, object]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except FileNotFoundError:
            return {}

    def _write(self, state: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
        temporary.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.path)

    def evidence(self, parent_number: int, contract: str) -> DecompositionReviewEvidence:
        key = str(parent_number)
        lock = DecompositionValidationStore(self.repository, self.path)
        with lock.locked("history"):
            state = self._read()
            raw = state.get(key)
            if raw is None:
                raw = {"baseline": contract, "applied_outcomes": [], "applied_identity_keys": []}
                state[key] = raw
                self._write(state)
            if not isinstance(raw, dict) or not isinstance(raw.get("baseline"), str):
                raise ValueError(f"Invalid decomposition-review history for parent Issue #{parent_number}")
            outcomes = raw.get("applied_outcomes", [])
            if not isinstance(outcomes, list) or any(not isinstance(item, str) for item in outcomes):
                raise ValueError(f"Invalid applied decomposition-review outcomes for parent Issue #{parent_number}")
            return DecompositionReviewEvidence(raw["baseline"], tuple(outcomes))

    def record_applied(self, parent_number: int, identity_key: str, outcome: str) -> None:
        lock = DecompositionValidationStore(self.repository, self.path)
        with lock.locked("history"):
            state = self._read()
            raw = state.get(str(parent_number))
            if not isinstance(raw, dict):
                return
            outcomes = raw.setdefault("applied_outcomes", [])
            keys = raw.setdefault("applied_identity_keys", [])
            if not isinstance(outcomes, list) or not isinstance(keys, list) or identity_key in keys:
                return
            outcomes.append(outcome)
            keys.append(identity_key)
            self._write(state)


def _set_contract_evidence(parent: DecompositionIssue, children: Sequence[DecompositionIssue]) -> str:
    """Serialize the exact first semantically analyzed authoritative set."""

    def member(issue: DecompositionIssue) -> dict[str, object]:
        return {
            "issue_number": issue.manifest.issue_number,
            "title": issue.manifest.title,
            "body": issue.body,
            "requirements": [{"requirement_id": requirement.requirement_id, "text": requirement.text} for requirement in issue.manifest.requirements],
        }

    return json.dumps(
        {"parent": member(parent), "direct_children": [member(child) for child in children]},
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )


Analyzer = Callable[[DecompositionIssue, Sequence[DecompositionIssue]], DecompositionAnalysisResult]


class DecompositionValidationLifecycle:
    """Coalesce one set review and apply generation-checked parent effects."""

    def __init__(self, repository: str, provider_identity: str, path: Optional[Path] = None, analyzer: Optional[Analyzer] = None) -> None:
        self.repository = repository
        self.policy_identity = decomposition_policy_identity(provider_identity)
        self.store = DecompositionValidationStore(repository, path)
        terminal_path = path.with_name("reissue_required.json") if path is not None else None
        self.reissue_store = ReissueRequiredStore(repository, terminal_path)
        history_path = path.with_name("decomposition_review_history.json") if path is not None else None
        self.history_store = DecompositionReviewHistoryStore(repository, history_path)
        objective_path = path.with_name("individual_review_history.json") if path is not None else None
        self.objective_store = ObjectiveAnchorStore(repository, objective_path)
        rounds_path = path.with_name("specification_repair_rounds.json") if path is not None else None
        self.repair_rounds = SpecificationRepairRoundStore(repository, rounds_path)
        self.analyzer = analyzer or (lambda parent, children: analyze_issue_decomposition(parent, children))

    def identity(self, parent: dict[str, object], children: Sequence[dict[str, object]]) -> DecompositionIdentity:
        def member(snapshot: dict[str, object]) -> SetMemberIdentity:
            raw_number = snapshot.get("number")
            if not isinstance(raw_number, int) or isinstance(raw_number, bool):
                raise ValueError("Decomposition member is missing a valid Issue number")
            number = raw_number
            stable_id = snapshot.get("id")
            # GitHub's repository-qualified Issue number is itself stable. The
            # database id is preferred when present and the number is retained
            # to make diagnostics and membership explicit.
            issue_id = int(stable_id) if isinstance(stable_id, int) else number
            return SetMemberIdentity(number, issue_id, specification_digest(str(snapshot.get("title") or ""), str(snapshot.get("body") or "")))

        return DecompositionIdentity(self.repository, member(parent), tuple(sorted((member(child) for child in children), key=lambda item: (item.issue_id, item.issue_number))), self.policy_identity)

    def decide(self, identity: DecompositionIdentity, parent: DecompositionIssue, children: Sequence[DecompositionIssue]) -> DecompositionDecision:
        with self.store.locked(identity.key):
            members = (parent, *children)
            member_roles = ((parent, ROLE_TRACKING_PARENT), *((child, ROLE_IMPLEMENTATION_CHILD) for child in children))
            structural_errors: list[str] = []
            valid = True
            for item, role in member_roles:
                assessment = assess_role_structure(item.manifest, item.body, role)
                if assessment.status == "ERROR":
                    structural_errors.append(assessment.error or f"Issue #{item.manifest.issue_number} structural assessment failed")
                elif assessment.status != "VALID":
                    valid = False
            if structural_errors:
                return DecompositionDecision(identity, "ERROR", remediation_reason="; ".join(structural_errors))
            evidence: Optional[DecompositionReviewEvidence] = None
            if valid:
                try:
                    history = self.history_store.evidence(parent.manifest.issue_number, _set_contract_evidence(parent, children))
                    objectives = tuple(self.objective_store.capture(item.manifest.issue_number, item.body, "complete-direct-child-set-snapshot:v1") for item in members)
                    evidence = DecompositionReviewEvidence(history.baseline, history.prior_applied_outcomes, objectives)
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    return DecompositionDecision(identity, "ERROR", remediation_reason=f"Objective evidence unavailable: {exc}")
                integrity = objective_integrity_result(evidence, members)
                if integrity is not None:
                    decision = DecompositionDecision(identity, integrity.verdict, integrity.findings, remediation=integrity.remediation, remediation_reason=integrity.error)
                    if integrity.verdict == "BLOCKED":
                        self.store.save(decision)
                    return decision
            existing = self.store.get(identity)
            if existing is not None:
                return existing
            if valid:
                assert evidence is not None
                with decomposition_review_evidence(evidence):
                    analyzed = self.analyzer(parent, children)
            else:
                analyzed = self.analyzer(parent, children)
            decision = DecompositionDecision(
                identity,
                analyzed.verdict,
                analyzed.findings,
                remediation=analyzed.remediation,
                remediation_reason=analyzed.error,
            )
            if decision.verdict in {"READY", "BLOCKED"}:
                self.store.save(decision)
            return decision

    def is_reissue_required(self, parent_number: int) -> bool:
        return self.reissue_store.contains(parent_number)

    @staticmethod
    def _repair_generation(decision: DecompositionDecision) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "parent": [decision.identity.parent.issue_number, decision.identity.parent.specification_digest],
                    "children": [[item.issue_number, item.specification_digest] for item in decision.identity.children],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def authorize_automatic_repair(
        self,
        decision: DecompositionDecision,
        set_is_current: Callable[[], bool],
        initiate: Callable[[], None],
    ) -> RepairRoundApplication:
        """Persist authorization before initiating an exact-current set repair."""
        from .llm_backend_config import get_specification_repair_round_limit_from_config

        with self.store.locked(decision.identity.key):
            current = self.store.get(decision.identity)
            if current is None or current.verdict != "BLOCKED" or current.remediation != "EDIT_IN_PLACE" or not set_is_current():
                return RepairRoundApplication(decision.remediation, self.repair_rounds.count("decomposition", decision.identity.parent.issue_number))
            applied = self.repair_rounds.authorize(
                "decomposition",
                current.identity.parent.issue_number,
                self._repair_generation(current),
                current.remediation,
                get_specification_repair_round_limit_from_config(repo_name=self.repository),
            )
            if applied.automatic_repair_authorized:
                initiate()
            return applied

    def apply_blocked(self, github: object, decision: DecompositionDecision, fetch_set: Callable[[int], Optional[tuple[dict[str, object], list[dict[str, object]]]]]) -> Optional[str]:
        """Apply idempotent parent-scoped effects only while BLOCKED evidence is authoritative.

        The findings comment and the readiness withdrawal are independently
        completed against the durable pending-work store, mirroring
        ``SpecificationValidationLifecycle.apply_blocked`` (Issue #2026,
        REQ-001, REQ-002, REQ-007): each is confirmed via ``complete_effect``
        exactly when its own GitHub mutation is established, and a
        ``GitHubRequestError`` from either effect is durably deferred rather
        than only recorded in the returned failure string, so a controller
        restart resumes exactly the effect that did not yet succeed.
        """
        parent_number = decision.identity.parent.issue_number
        publication_identity = decomposition_publication_identity(self.repository, parent_number, decision.identity.key)
        pending_work_store = get_pending_work_store()
        with self.store.locked(decision.identity.key):
            failures: list[str] = []
            current = self.store.get(decision.identity)
            if current is None or current.verdict != "BLOCKED":
                return "durable decomposition BLOCKED decision is unavailable"

            diagnostic_trusted = publication_trusted_complete(current)
            if diagnostic_trusted:
                pending_work_store.complete_effect(publication_identity, DIAGNOSTIC_EFFECT)
            if current.readiness_removed:
                pending_work_store.complete_effect(publication_identity, READINESS_WITHDRAWAL_EFFECT)

            def identity_matches() -> bool:
                fetched = fetch_set(decision.identity.parent.issue_number)
                return fetched is not None and str(fetched[0].get("state") or "").lower() == "open" and self.identity(*fetched) == decision.identity

            def still_current() -> bool:
                fetched = fetch_set(decision.identity.parent.issue_number)
                return fetched is not None and str(fetched[0].get("state") or "").lower() == "open" and is_implementation_ready(fetched[0]) and self.identity(*fetched) == decision.identity

            def diagnostic_is_current() -> bool:
                """Currentness for the diagnostic comment.

                Same full currentness as the label withdrawal, except when
                that check is failing only because this exact decision's own
                readiness withdrawal already completed: that specific
                absence of the label must not hide a still-missing
                diagnostic comment (REQ-004, mirrors
                ``SpecificationValidationLifecycle.apply_blocked``).
                """
                if still_current():
                    return True
                if not identity_matches():
                    return False
                recorded = self.store.get(decision.identity)
                return recorded is not None and recorded.readiness_removed

            if not diagnostic_is_current():
                return None
            from .llm_backend_config import get_specification_repair_round_limit_from_config

            generation = self._repair_generation(current)
            applied = self.repair_rounds.apply(
                "decomposition",
                current.identity.parent.issue_number,
                generation,
                current.remediation,
                get_specification_repair_round_limit_from_config(repo_name=self.repository),
            )
            if (applied.remediation, applied.reason) != (current.remediation, current.remediation_reason):
                current = DecompositionDecision(
                    current.identity,
                    current.verdict,
                    current.findings,
                    current.findings_published,
                    current.readiness_removed,
                    applied.remediation,
                    applied.reason,
                )
                self.store.save(current)
            self.history_store.record_applied(
                current.identity.parent.issue_number,
                current.identity.key,
                json.dumps(
                    {
                        "reviewed_set": asdict(current.identity),
                        "remediation": current.remediation,
                        "findings": [asdict(finding) for finding in current.findings],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
            if current.remediation == "REISSUE_REQUIRED":
                try:
                    self.reissue_store.mark(current.identity.parent.issue_number)
                except OSError as exc:
                    return f"durable reissue-required marker failed: {exc}"
            if not diagnostic_trusted:
                marker = f"{DECOMPOSITION_FINDINGS_MARKER}:{current.identity.key}"
                expected_body = self.findings_comment(current)
                try:
                    comments = github.get_issue_comments_strict(self.repository, parent_number)  # type: ignore[attr-defined]
                except Exception as exc:
                    failures.append(f"findings lookup failed: {exc}")
                    if isinstance(exc, GitHubRequestError):
                        pending_work_store.defer(publication_identity, exc, (DIAGNOSTIC_EFFECT,))
                    comments = None
                if not diagnostic_is_current():
                    return "; ".join(failures) or None
                if comments is not None:
                    if current.publication_schema_version == 0:
                        current = replace(current, publication_schema_version=1)
                        self.store.save(current)
                    try:
                        reviewer_identity = github.reviewer_app_identity(self.repository)  # type: ignore[attr-defined]
                    except Exception as exc:
                        failures.append(f"reviewer identity unavailable: {exc}")
                        reviewer_identity = None
                    if reviewer_identity is not None:
                        receipt, conflicting = find_confirmed_publication(comments, marker, expected_body, reviewer_identity)
                        if receipt is None and not conflicting:
                            try:
                                receipt = github.publish_issue_review_comment(self.repository, parent_number, expected_body, diagnostic_is_current)  # type: ignore[attr-defined]
                            except Exception as exc:
                                failures.append(f"findings publication failed: {exc}")
                                if isinstance(exc, GitHubRequestError):
                                    pending_work_store.defer(publication_identity, exc, (DIAGNOSTIC_EFFECT,))
                                receipt = None
                        if receipt is not None:
                            current = replace(current, findings_published=True, publication_schema_version=1, publication_receipt=receipt.as_dict())
                            self.store.save(current)
                            pending_work_store.complete_effect(publication_identity, DIAGNOSTIC_EFFECT)
                        elif conflicting:
                            failures.append("findings publication conflict: existing marker comment is not confirmed reviewer-App authored")
            if not still_current():
                return "; ".join(failures) or None
            try:
                github.remove_labels(self.repository, parent_number, [IMPLEMENTATION_READY_LABEL], item_type="issue")  # type: ignore[attr-defined]
                current = replace(current, readiness_removed=True)
                self.store.save(current)
                pending_work_store.complete_effect(publication_identity, READINESS_WITHDRAWAL_EFFECT)
            except Exception as exc:
                failures.append(f"readiness withdrawal failed: {exc}")
                if isinstance(exc, GitHubRequestError):
                    pending_work_store.defer(publication_identity, exc, (READINESS_WITHDRAWAL_EFFECT,))
        return "; ".join(failures) or None

    @staticmethod
    def findings_comment(decision: DecompositionDecision) -> str:
        remedy = "Replace this parent Issue and submitted set with a new parent Issue number." if decision.remediation == "REISSUE_REQUIRED" else "Edit the submitted Issue set in place and resubmit it for validation."
        lines = [f"<!-- {DECOMPOSITION_FINDINGS_MARKER}:{decision.identity.key} -->", "## Auto-Coder decomposition validation", "", "Implementation is blocked by defects in the submitted parent/child specification set:", "", f"**Remediation:** {remedy}"]
        if decision.remediation_reason:
            lines.extend(["", f"**Reason:** `{decision.remediation_reason}`"])
        if decision.remediation_reason == "automatic_repair_paused(repair_round_limit_reached)":
            lines.extend(
                [
                    "",
                    "Automatic repair has paused because the repair-round limit was reached. " "The semantic remediation remains `EDIT_IN_PLACE`; replacement/reissue is not required by the circuit breaker itself.",
                ]
            )
        for finding in decision.findings:
            affected = ", ".join(f"#{item.issue_number} ({', '.join(item.requirement_ids) or 'contract-wide'})" for item in finding.affected_issues)
            lines.extend(["", f"- **{finding.category}** — {affected}: {finding.explanation}", f"  Clarification required: {finding.clarification}"])
        return "\n".join(lines)
