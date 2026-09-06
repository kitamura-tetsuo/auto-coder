"""Durable authorization for implementation-ready parent Issue sets."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence

from .decomposition_analyzer import (
    DECOMPOSITION_FINDING_CATEGORIES,
    AffectedIssue,
    DecompositionAnalysisResult,
    DecompositionFinding,
    DecompositionIssue,
    analyze_issue_decomposition,
)
from .prompt_loader import load_prompts
from .specification_validation_lifecycle import specification_digest
from .util.gh_cache import IMPLEMENTATION_READY_LABEL, is_implementation_ready

DECOMPOSITION_SCHEMA_VERSION = "issue-decomposition-validation-v1"
DECOMPOSITION_FINDINGS_MARKER = "auto-coder-decomposition-validation"


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


def decomposition_policy_identity(provider_identity: str) -> str:
    issue_prompts = load_prompts().get("issue")
    prompt = issue_prompts.get("adversarial_decomposition_analysis") if isinstance(issue_prompts, dict) else None
    contract = {
        "version": DECOMPOSITION_SCHEMA_VERSION,
        "prompt": prompt,
        "categories": sorted(DECOMPOSITION_FINDING_CATEGORIES),
        "result_fields": ["verdict", "findings", "category", "affected_issues", "issue_number", "requirement_ids", "explanation", "clarification"],
        "provider": provider_identity,
    }
    return hashlib.sha256(json.dumps(contract, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class DecompositionValidationStore:
    def __init__(self, repository: str, path: Optional[Path] = None) -> None:
        root = Path(os.environ.get("AUTO_CODER_SPECIFICATION_VALIDATION_ROOT", Path.home() / ".auto-coder"))
        self.path = path or root / repository / "decomposition_validations.json"

    def _read(self) -> dict[str, object]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    @contextmanager
    def locked(self, key: str) -> Iterator[None]:
        import fcntl

        lock_name = f"{self.path}:{key}"
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(lock_name, threading.Lock())
        with lock:
            lock_path = self.path.with_suffix(f".{key}.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a", encoding="utf-8") as stream:
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
        return DecompositionDecision(identity, str(raw["verdict"]), findings, bool(raw.get("findings_published")), bool(raw.get("readiness_removed")))

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
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
            temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(temporary, self.path)


Analyzer = Callable[[DecompositionIssue, Sequence[DecompositionIssue]], DecompositionAnalysisResult]


class DecompositionValidationLifecycle:
    """Coalesce one set review and apply generation-checked parent effects."""

    def __init__(self, repository: str, provider_identity: str, path: Optional[Path] = None, analyzer: Optional[Analyzer] = None) -> None:
        self.repository = repository
        self.policy_identity = decomposition_policy_identity(provider_identity)
        self.store = DecompositionValidationStore(repository, path)
        self.analyzer = analyzer or (lambda parent, children: analyze_issue_decomposition(parent, children))

    def identity(self, parent: dict[str, object], children: Sequence[dict[str, object]]) -> DecompositionIdentity:
        def member(snapshot: dict[str, object]) -> SetMemberIdentity:
            number = int(snapshot["number"])
            stable_id = snapshot.get("id")
            # GitHub's repository-qualified Issue number is itself stable. The
            # database id is preferred when present and the number is retained
            # to make diagnostics and membership explicit.
            issue_id = int(stable_id) if isinstance(stable_id, int) else number
            return SetMemberIdentity(number, issue_id, specification_digest(str(snapshot.get("title") or ""), str(snapshot.get("body") or "")))

        return DecompositionIdentity(self.repository, member(parent), tuple(sorted((member(child) for child in children), key=lambda item: (item.issue_id, item.issue_number))), self.policy_identity)

    def decide(self, identity: DecompositionIdentity, parent: DecompositionIssue, children: Sequence[DecompositionIssue]) -> DecompositionDecision:
        with self.store.locked(identity.key):
            existing = self.store.get(identity)
            if existing is not None:
                return existing
            analyzed = self.analyzer(parent, children)
            decision = DecompositionDecision(identity, analyzed.verdict, analyzed.findings)
            if decision.verdict in {"READY", "BLOCKED"}:
                self.store.save(decision)
            return decision

    def apply_blocked(self, github: object, decision: DecompositionDecision, fetch_set: Callable[[int], Optional[tuple[dict[str, object], list[dict[str, object]]]]]) -> Optional[str]:
        with self.store.locked(decision.identity.key):
            current = self.store.get(decision.identity)
            if current is None or current.verdict != "BLOCKED":
                return "durable decomposition BLOCKED decision is unavailable"

            def still_current() -> bool:
                fetched = fetch_set(decision.identity.parent.issue_number)
                return fetched is not None and is_implementation_ready(fetched[0]) and self.identity(*fetched) == decision.identity

            if not still_current():
                return None
            if not current.findings_published:
                marker = f"{DECOMPOSITION_FINDINGS_MARKER}:{current.identity.key}"
                comments = github.get_issue_comments_strict(self.repository, current.identity.parent.issue_number)  # type: ignore[attr-defined]
                if not still_current():
                    return None
                if not any(marker in str(comment.get("body") or "") for comment in comments if isinstance(comment, dict)):
                    github.add_comment_to_issue(self.repository, current.identity.parent.issue_number, self.findings_comment(current))  # type: ignore[attr-defined]
                current = DecompositionDecision(current.identity, current.verdict, current.findings, True, current.readiness_removed)
                self.store.save(current)
            if not still_current():
                return None
            github.remove_labels(self.repository, current.identity.parent.issue_number, [IMPLEMENTATION_READY_LABEL], item_type="issue")  # type: ignore[attr-defined]
            self.store.save(DecompositionDecision(current.identity, current.verdict, current.findings, current.findings_published, True))
        return None

    @staticmethod
    def findings_comment(decision: DecompositionDecision) -> str:
        lines = [f"<!-- {DECOMPOSITION_FINDINGS_MARKER}:{decision.identity.key} -->", "## Auto-Coder decomposition validation", "", "Implementation is blocked by defects in the submitted parent/child specification set:"]
        for finding in decision.findings:
            affected = ", ".join(f"#{item.issue_number} ({', '.join(item.requirement_ids) or 'contract-wide'})" for item in finding.affected_issues)
            lines.extend(["", f"- **{finding.category}** — {affected}: {finding.explanation}", f"  Clarification required: {finding.clarification}"])
        return "\n".join(lines)
