"""Durable, generation-bound authorization for Issue implementation."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

from .prompt_loader import load_prompts
from .reissue_required_store import ReissueRequiredStore
from .requirement_contract import NormativeIssueManifest
from .specification_analyzer import (
    SPECIFICATION_FINDING_CATEGORIES,
    IndividualReviewEvidence,
    SpecificationAnalysisResult,
    SpecificationFinding,
    analyze_issue_specification,
    individual_review_evidence,
)
from .util.gh_cache import IMPLEMENTATION_READY_LABEL, is_implementation_ready

VALIDATION_SCHEMA_VERSION = "issue-specification-validation-v2-remediation"
FINDINGS_MARKER_PREFIX = "auto-coder-specification-validation"


def configured_provider_identity() -> str:
    """Return the effective validator route and models (never credentials)."""
    override = os.environ.get("AUTO_CODER_SPECIFICATION_VALIDATOR_IDENTITY")
    if override:
        return override
    from .llm_backend_config import get_llm_config

    config = get_llm_config()
    if config is None:
        return "unconfigured"
    order = config.get_adversarial_validation_backend_order()
    if not order:
        default = config.get_adversarial_validation_default_backend()
        order = [default] if default else []
    if not order:
        getter = getattr(config, "get_high_score_backend_order", None)
        order = getter() if callable(getter) else list(getattr(config, "backend_with_high_score_order", []) or [])
    route = []
    for name in order:
        backend = config.get_backend_config(name)
        route.append(
            {
                "alias": name,
                "provider": (backend.backend_type or backend.name) if backend is not None else name,
                "model": config.get_model_for_backend(name),
            }
        )
    return json.dumps(route, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ValidationIdentity:
    repository: str
    issue_number: int
    specification_digest: str
    policy_identity: str

    @property
    def key(self) -> str:
        value = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ValidationDecision:
    identity: ValidationIdentity
    verdict: str
    findings: tuple[SpecificationFinding, ...] = ()
    findings_published: bool = False
    readiness_removed: bool = False
    remediation: str = "NONE"


def _contract_evidence(manifest: NormativeIssueManifest, title: str, body: str) -> str:
    value = {
        "issue_number": manifest.issue_number,
        "title": title,
        "body": body,
        "requirements": [{"requirement_id": item.requirement_id, "text": item.text} for item in manifest.requirements],
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


class IndividualReviewHistoryStore:
    """Atomic per-Issue baseline and applied BLOCKED-review history."""

    def __init__(self, repository: str, path: Optional[Path] = None) -> None:
        state_root = Path(os.environ.get("AUTO_CODER_SPECIFICATION_VALIDATION_ROOT", Path.home() / ".auto-coder"))
        self.path = path or state_root / repository / "individual_review_history.json"

    def _read(self) -> dict[str, object]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except FileNotFoundError:
            return {}

    def evidence(self, issue_number: int, contract: str) -> IndividualReviewEvidence:
        """Create the immutable first valid baseline and return prior outcomes."""
        key = str(issue_number)
        with self._locked():
            state = self._read()
            raw = state.get(key)
            if raw is None:
                raw = {"baseline": contract, "applied_outcomes": []}
                state[key] = raw
                self._write(state)
            if not isinstance(raw, dict) or not isinstance(raw.get("baseline"), str):
                raise ValueError(f"Invalid individual-review history for Issue #{issue_number}")
            outcomes = raw.get("applied_outcomes", [])
            if not isinstance(outcomes, list) or any(not isinstance(item, str) for item in outcomes):
                raise ValueError(f"Invalid applied individual-review outcomes for Issue #{issue_number}")
            valid = tuple(outcomes)
            return IndividualReviewEvidence(str(raw["baseline"]), valid)

    def record_applied(self, issue_number: int, identity_key: str, outcome: str) -> None:
        key = str(issue_number)
        with self._locked():
            state = self._read()
            raw = state.get(key)
            if not isinstance(raw, dict):
                return
            applied = raw.setdefault("applied_outcomes", [])
            applied_keys = raw.setdefault("applied_identity_keys", [])
            if not isinstance(applied, list) or not isinstance(applied_keys, list) or identity_key in applied_keys:
                return
            applied.append(outcome)
            applied_keys.append(identity_key)
            self._write(state)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        import fcntl

        lock_path = self.path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _write(self, state: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
        temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)


def specification_digest(title: str, body: str) -> str:
    """Digest exact authoritative fields without ambiguous concatenation."""
    encoded = json.dumps([title, body], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validation_policy_identity(provider_identity: str) -> str:
    """Identify every configured input which can alter the semantic decision."""
    issue_prompts = load_prompts().get("issue")
    prompt = issue_prompts.get("adversarial_specification_analysis") if isinstance(issue_prompts, dict) else None
    contract = {
        "version": VALIDATION_SCHEMA_VERSION,
        "prompt": prompt,
        "categories": sorted(SPECIFICATION_FINDING_CATEGORIES),
        "result_fields": ["verdict", "remediation", "findings"],
        "provider": provider_identity,
    }
    return hashlib.sha256(json.dumps(contract, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


_IDENTITY_LOCKS: dict[str, threading.Lock] = {}
_IDENTITY_LOCKS_GUARD = threading.Lock()


class SpecificationValidationStore:
    """Atomic JSON store for completed READY/BLOCKED decisions."""

    def __init__(self, repository: str, path: Optional[Path] = None) -> None:
        state_root = Path(os.environ.get("AUTO_CODER_SPECIFICATION_VALIDATION_ROOT", Path.home() / ".auto-coder"))
        self.path = path or state_root / repository / "specification_validations.json"

    def _read(self) -> dict[str, object]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except FileNotFoundError:
            return {}

    @contextmanager
    def locked(self, key: str) -> Iterator[None]:
        """Serialize one identity in this process and across daemon processes."""
        import fcntl

        with _IDENTITY_LOCKS_GUARD:
            lock = _IDENTITY_LOCKS.setdefault(f"{self.path}:{key}", threading.Lock())
        with lock:
            lock_path = self.path.with_suffix(f".{key}.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a", encoding="utf-8") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def get(self, identity: ValidationIdentity) -> Optional[ValidationDecision]:
        raw = self._read().get(identity.key)
        if not isinstance(raw, dict) or raw.get("verdict") not in {"READY", "BLOCKED"}:
            return None
        if raw.get("identity") != asdict(identity):
            return None
        findings = tuple(SpecificationFinding(**item) for item in raw.get("findings", []) if isinstance(item, dict))
        remediation = str(raw.get("remediation", "NONE"))
        return ValidationDecision(identity, str(raw["verdict"]), findings, bool(raw.get("findings_published")), bool(raw.get("readiness_removed")), remediation)

    def save(self, decision: ValidationDecision) -> None:
        if decision.verdict not in {"READY", "BLOCKED"}:
            raise ValueError("ERROR decisions must not be persisted")
        with self.locked("repository-state"):
            state = self._read()
            state[decision.identity.key] = {
                "identity": asdict(decision.identity),
                "verdict": decision.verdict,
                "findings": [asdict(item) for item in decision.findings],
                "findings_published": decision.findings_published,
                "readiness_removed": decision.readiness_removed,
                "remediation": decision.remediation,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
            temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(temporary, self.path)


Analyzer = Callable[[NormativeIssueManifest, str], SpecificationAnalysisResult]


class SpecificationValidationLifecycle:
    """Coalesce validation and apply generation-checked BLOCKED effects."""

    def __init__(self, repository: str, provider_identity: str, path: Optional[Path] = None, analyzer: Optional[Analyzer] = None) -> None:
        self.repository = repository
        self.policy_identity = validation_policy_identity(provider_identity)
        self.store = SpecificationValidationStore(repository, path)
        terminal_path = path.with_name("reissue_required.json") if path is not None else None
        history_path = path.with_name("individual_review_history.json") if path is not None else None
        self.reissue_store = ReissueRequiredStore(repository, terminal_path)
        self.history_store = IndividualReviewHistoryStore(repository, history_path)
        self.analyzer = analyzer

    def identity(self, issue_number: int, title: str, body: str) -> ValidationIdentity:
        return ValidationIdentity(self.repository, issue_number, specification_digest(title, body), self.policy_identity)

    def decide(self, manifest: NormativeIssueManifest, title: str, body: str) -> ValidationDecision:
        identity = self.identity(manifest.issue_number, title, body)
        with self.store.locked(identity.key):
            existing = self.store.get(identity)
            if existing is not None:
                return existing
            if not manifest.explicit_contract_present or not manifest.explicit_contract_valid:
                analyzed = self.analyzer(manifest, body) if self.analyzer is not None else analyze_issue_specification(manifest, body)
                decision = ValidationDecision(identity, analyzed.verdict, analyzed.findings, remediation=analyzed.remediation)
                if analyzed.verdict in {"READY", "BLOCKED"}:
                    self.store.save(decision)
                return decision
            contract = _contract_evidence(manifest, title, body)
            evidence = self.history_store.evidence(manifest.issue_number, contract)
            if self.analyzer is None:
                analyzed = self._default_analyzer(manifest, body, evidence)
            else:
                analyzed = self.analyzer(manifest, body)
            decision = ValidationDecision(identity, analyzed.verdict, analyzed.findings, remediation=analyzed.remediation)
            if analyzed.verdict in {"READY", "BLOCKED"}:
                self.store.save(decision)
            return decision

    def _default_analyzer(self, manifest: NormativeIssueManifest, body: str, evidence: IndividualReviewEvidence) -> SpecificationAnalysisResult:
        with individual_review_evidence(evidence):
            return analyze_issue_specification(manifest, body)

    def is_reissue_required(self, issue_number: int) -> bool:
        """Return the durable authorization stop for this stable Issue number."""
        return self.reissue_store.contains(issue_number)

    def apply_blocked(
        self,
        github: object,
        decision: ValidationDecision,
        submission_is_current: Optional[Callable[[], bool]] = None,
    ) -> Optional[str]:
        """Apply idempotent effects only while BLOCKED evidence is authoritative."""
        issue_number = decision.identity.issue_number
        expected_identity = decision.identity
        with self.store.locked(decision.identity.key):
            current_decision = self.store.get(decision.identity)
            if current_decision is None or current_decision.verdict != "BLOCKED":
                return "durable BLOCKED decision is unavailable"

            def matching_snapshot() -> Optional[dict[str, object]]:
                snapshot = github.get_issue_dispatch_snapshot_strict(self.repository, issue_number)  # type: ignore[attr-defined]
                if not isinstance(snapshot, dict) or not is_implementation_ready(snapshot):
                    return None
                identity = self.identity(issue_number, str(snapshot.get("title") or ""), str(snapshot.get("body") or ""))
                if identity != expected_identity or (submission_is_current is not None and not submission_is_current()):
                    return None
                return snapshot

            # A changed/withdrawn submission must not receive stale effects. It is
            # not an operational failure: the old generation simply remains blocked.
            if matching_snapshot() is None:
                return None
            self._record_applied_outcome(current_decision)
            if current_decision.remediation == "REISSUE_REQUIRED":
                try:
                    self.reissue_store.mark(issue_number)
                except OSError as exc:
                    return f"durable reissue-required marker failed: {exc}"
            if not current_decision.findings_published:
                marker = f"{FINDINGS_MARKER_PREFIX}:{current_decision.identity.key}"
                comments = github.get_issue_comments_strict(self.repository, issue_number)  # type: ignore[attr-defined]
                # Comment enumeration is external I/O. An edit during that read
                # invalidates publication just as an edit before the read does.
                if matching_snapshot() is None:
                    return None
                if not any(marker in str(comment.get("body") or "") for comment in comments if isinstance(comment, dict)):
                    github.add_comment_to_issue(self.repository, issue_number, self.findings_comment(current_decision))  # type: ignore[attr-defined]
                current_decision = ValidationDecision(current_decision.identity, current_decision.verdict, current_decision.findings, True, current_decision.readiness_removed, current_decision.remediation)
                self.store.save(current_decision)
            if matching_snapshot() is None:
                return None
            # readiness_removed describes the previous submission, not all future
            # submissions. If the label is currently present it was explicitly
            # re-added and must be removed again, while the findings stay unique.
            github.remove_labels(self.repository, issue_number, [IMPLEMENTATION_READY_LABEL], item_type="issue")  # type: ignore[attr-defined]
            current_decision = ValidationDecision(
                current_decision.identity,
                current_decision.verdict,
                current_decision.findings,
                current_decision.findings_published,
                True,
                current_decision.remediation,
            )
            self.store.save(current_decision)
        return None

    def apply_inherited_blocked(
        self,
        github: object,
        decision: ValidationDecision,
        parent_number: int,
        set_is_current: Callable[[], bool],
    ) -> Optional[str]:
        """Withdraw a parent submission for one current blocked child."""
        issue_number = decision.identity.issue_number
        with self.store.locked(decision.identity.key):
            failures: list[str] = []
            current = self.store.get(decision.identity)
            if current is None or current.verdict != "BLOCKED":
                return "durable child BLOCKED decision is unavailable"

            def still_current() -> bool:
                snapshot = github.get_issue_dispatch_snapshot_strict(self.repository, issue_number)  # type: ignore[attr-defined]
                return isinstance(snapshot, dict) and self.identity(issue_number, str(snapshot.get("title") or ""), str(snapshot.get("body") or "")) == decision.identity and set_is_current()

            if not still_current():
                return None
            self._record_applied_outcome(current)
            if current.remediation == "REISSUE_REQUIRED":
                try:
                    self.reissue_store.mark(issue_number)
                except OSError as exc:
                    return f"durable reissue-required marker failed: {exc}"
            if not current.findings_published:
                marker = f"{FINDINGS_MARKER_PREFIX}:{current.identity.key}"
                try:
                    comments = github.get_issue_comments_strict(self.repository, issue_number)  # type: ignore[attr-defined]
                except Exception as exc:
                    failures.append(f"findings lookup failed: {exc}")
                    comments = None
                if not still_current():
                    return "; ".join(failures) or None
                if comments is not None:
                    published = any(marker in str(comment.get("body") or "") for comment in comments if isinstance(comment, dict))
                    if not published:
                        try:
                            github.add_comment_to_issue(self.repository, issue_number, self.findings_comment(current))  # type: ignore[attr-defined]
                            published = True
                        except Exception as exc:
                            failures.append(f"findings publication failed: {exc}")
                    if published:
                        current = ValidationDecision(current.identity, current.verdict, current.findings, True, current.readiness_removed, current.remediation)
                        self.store.save(current)
            if not still_current():
                return "; ".join(failures) or None
            try:
                github.remove_labels(self.repository, parent_number, [IMPLEMENTATION_READY_LABEL], item_type="issue")  # type: ignore[attr-defined]
                self.store.save(ValidationDecision(current.identity, current.verdict, current.findings, current.findings_published, True, current.remediation))
            except Exception as exc:
                failures.append(f"readiness withdrawal failed: {exc}")
        return "; ".join(failures) or None

    def _record_applied_outcome(self, decision: ValidationDecision) -> None:
        outcome = json.dumps(
            {
                "specification_digest": decision.identity.specification_digest,
                "verdict": decision.verdict,
                "remediation": decision.remediation,
                "findings": [asdict(item) for item in decision.findings],
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        self.history_store.record_applied(decision.identity.issue_number, decision.identity.key, outcome)

    @staticmethod
    def findings_comment(decision: ValidationDecision) -> str:
        remedy = "Replace this Issue with a new Issue number; editing this Issue cannot restore implementation eligibility." if decision.remediation == "REISSUE_REQUIRED" else "Edit this Issue in place and resubmit it for validation."
        lines = [f"<!-- {FINDINGS_MARKER_PREFIX}:{decision.identity.key} -->", "## Auto-Coder specification validation", "", "Implementation is blocked by material specification defects:", "", f"**Remediation:** {remedy}"]
        for finding in decision.findings:
            ids = ", ".join(finding.requirement_ids) or "contract-wide"
            lines.extend(["", f"- **{finding.category}** ({ids}): {finding.explanation}", f"  Clarification required: {finding.clarification}"])
        return "\n".join(lines)
