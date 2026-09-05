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
from .requirement_contract import NormativeIssueManifest
from .specification_analyzer import (
    SPECIFICATION_FINDING_CATEGORIES,
    SpecificationAnalysisResult,
    SpecificationFinding,
    analyze_issue_specification,
)
from .util.gh_cache import IMPLEMENTATION_READY_LABEL, is_implementation_ready

VALIDATION_SCHEMA_VERSION = "issue-specification-validation-v1"
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
    route = [{"provider": name, "model": config.get_model_for_backend(name)} for name in order]
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
        return ValidationDecision(identity, str(raw["verdict"]), findings, bool(raw.get("findings_published")), bool(raw.get("readiness_removed")))

    def save(self, decision: ValidationDecision) -> None:
        if decision.verdict not in {"READY", "BLOCKED"}:
            raise ValueError("ERROR decisions must not be persisted")
        state = self._read()
        state[decision.identity.key] = {
            "identity": asdict(decision.identity),
            "verdict": decision.verdict,
            "findings": [asdict(item) for item in decision.findings],
            "findings_published": decision.findings_published,
            "readiness_removed": decision.readiness_removed,
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
        self.analyzer = analyzer or (lambda manifest, body: analyze_issue_specification(manifest, body))

    def identity(self, issue_number: int, title: str, body: str) -> ValidationIdentity:
        return ValidationIdentity(self.repository, issue_number, specification_digest(title, body), self.policy_identity)

    def decide(self, manifest: NormativeIssueManifest, title: str, body: str) -> ValidationDecision:
        identity = self.identity(manifest.issue_number, title, body)
        with self.store.locked(identity.key):
            existing = self.store.get(identity)
            if existing is not None:
                return existing
            analyzed = self.analyzer(manifest, body)
            decision = ValidationDecision(identity, analyzed.verdict, analyzed.findings)
            if analyzed.verdict in {"READY", "BLOCKED"}:
                self.store.save(decision)
            return decision

    def apply_blocked(self, github: object, decision: ValidationDecision) -> Optional[str]:
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
                return snapshot if identity == expected_identity else None

            # A changed/withdrawn submission must not receive stale effects. It is
            # not an operational failure: the old generation simply remains blocked.
            if matching_snapshot() is None:
                return None
            if not current_decision.findings_published:
                marker = f"{FINDINGS_MARKER_PREFIX}:{current_decision.identity.key}"
                comments = github.get_issue_comments_strict(self.repository, issue_number)  # type: ignore[attr-defined]
                if not any(marker in str(comment.get("body") or "") for comment in comments if isinstance(comment, dict)):
                    github.add_comment_to_issue(self.repository, issue_number, self.findings_comment(current_decision))  # type: ignore[attr-defined]
                current_decision = ValidationDecision(current_decision.identity, current_decision.verdict, current_decision.findings, True, current_decision.readiness_removed)
                self.store.save(current_decision)
            if matching_snapshot() is None:
                return None
            if not current_decision.readiness_removed:
                github.remove_labels(self.repository, issue_number, [IMPLEMENTATION_READY_LABEL], item_type="issue")  # type: ignore[attr-defined]
                current_decision = ValidationDecision(current_decision.identity, current_decision.verdict, current_decision.findings, current_decision.findings_published, True)
                self.store.save(current_decision)
        return None

    @staticmethod
    def findings_comment(decision: ValidationDecision) -> str:
        lines = [f"<!-- {FINDINGS_MARKER_PREFIX}:{decision.identity.key} -->", "## Auto-Coder specification validation", "", "Implementation is blocked by material specification defects:"]
        for finding in decision.findings:
            ids = ", ".join(finding.requirement_ids) or "contract-wide"
            lines.extend(["", f"- **{finding.category}** ({ids}): {finding.explanation}", f"  Clarification required: {finding.clarification}"])
        return "\n".join(lines)
