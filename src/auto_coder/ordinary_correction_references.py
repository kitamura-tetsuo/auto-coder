"""Controller-supplied ordinary correction references with immutable scope bindings.

This module defines no new store. References are derived from the canonical
ledger, carried by one review context, and rechecked before reconciliation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Optional, Sequence

from .canonical_pr_blocker_ledger import BlockerLedgerSnapshot, BlockerSnapshot, QualifiedRequirement
from .util.github_request_outcome import normalize_api_origin

CANONICAL_CORRECTION_PREFIX = "canonical:"
ORDINARY_OBSERVATION_ALIAS = "ordinary_observation"
ORDINARY_LABEL_ALIAS = "ordinary_observation_label"


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def qualified_requirements(requirement_ids: Sequence[str], issue_number: int = 0) -> tuple[QualifiedRequirement, ...]:
    """Preserve qualified IDs without embedding the qualifier into the local ID."""
    normalized = set()
    for identity in requirement_ids:
        match = re.fullmatch(r"#(\d+)/(REQ-[0-9A-Za-z_-]+)", identity)
        normalized.add((int(match.group(1)), match.group(2)) if match else (issue_number, identity))
    return tuple(QualifiedRequirement(issue_number=issue, requirement_id=identity) for issue, identity in sorted(normalized))


def requirement_identities(requirements: Sequence[QualifiedRequirement]) -> tuple[str, ...]:
    """Read both older qualified strings and normalized ledger requirements."""
    return tuple(sorted({f"#{entry.issue_number}/{entry.requirement_id}" if entry.issue_number and not entry.requirement_id.startswith("#") else entry.requirement_id for entry in requirements}))


def correction_scope_fingerprint(blocker: BlockerSnapshot) -> str:
    """Hash immutable accepted scope; observation prose and presentation are absent."""
    return _digest(
        {
            "requirements": requirement_identities(blocker.qualified_requirements),
            "boundary": blocker.authoritative_boundary,
            "incorrect_behavior": blocker.incorrect_behavior_or_missing_invariant,
            "required_outcome": blocker.required_correction_outcome,
            "evidence_needed": blocker.evidence_needed,
            "objective": blocker.original_objective_anchor,
            "accepted_scope": asdict(blocker.accepted_scope),
        }
    )


@dataclass(frozen=True)
class CanonicalCorrectionReference:
    correction_identity: str = ""
    scope_fingerprint: str = ""
    category: str = ""
    requirement_ids: tuple[str, ...] = ()
    authoritative_boundary: str = ""
    incorrect_behavior: str = ""
    required_outcome: str = ""
    observation_labels: tuple[str, ...] = ()

    @property
    def blocker_id(self) -> str:
        return self.correction_identity.removeprefix(CANONICAL_CORRECTION_PREFIX)


@dataclass(frozen=True)
class CanonicalCorrectionContext:
    api_origin: str = ""
    repository: str = ""
    pr_number: int = 0
    head_sha: str = ""
    base_sha: str = ""
    validation_snapshot: str = ""
    requirement_manifest_identity: str = ""
    references: tuple[CanonicalCorrectionReference, ...] = ()

    @property
    def fingerprint(self) -> str:
        return _digest(asdict(self))


def build_correction_reference_context(
    snapshot: BlockerLedgerSnapshot,
    head_sha: str,
    base_sha: str,
    validation_snapshot: str = "",
    requirement_manifest_identity: str = "",
) -> CanonicalCorrectionContext:
    references = tuple(
        CanonicalCorrectionReference(
            correction_identity=f"{CANONICAL_CORRECTION_PREFIX}{blocker.blocker_id}",
            scope_fingerprint=correction_scope_fingerprint(blocker),
            category=blocker.category,
            requirement_ids=requirement_identities(blocker.qualified_requirements),
            authoritative_boundary=blocker.authoritative_boundary,
            incorrect_behavior=blocker.incorrect_behavior_or_missing_invariant,
            required_outcome=blocker.required_correction_outcome,
            observation_labels=tuple(sorted({alias.alias_value for alias in blocker.aliases if alias.alias_type == ORDINARY_LABEL_ALIAS})),
        )
        for blocker in sorted(snapshot.get_open_blockers(), key=lambda entry: entry.blocker_id)
    )
    return CanonicalCorrectionContext(
        api_origin=normalize_api_origin(snapshot.api_origin),
        repository=snapshot.repository,
        pr_number=snapshot.pr_number,
        head_sha=head_sha,
        base_sha=base_sha,
        validation_snapshot=validation_snapshot,
        requirement_manifest_identity=requirement_manifest_identity,
        references=references,
    )


def validate_correction_reference(
    context: Optional[CanonicalCorrectionContext],
    identity: str,
    scope_fingerprint: str,
    context_fingerprint: str,
    requirement_ids: Sequence[str],
    authoritative_boundary: str,
    category: str,
) -> tuple[Optional[CanonicalCorrectionReference], str]:
    """Validate a supplied reference; arbitrary observation labels grant no identity."""
    if not identity.startswith(CANONICAL_CORRECTION_PREFIX):
        return None, "Correction binding supplied without a canonical reference" if scope_fingerprint or context_fingerprint else ""
    if context is None:
        return None, "Canonical correction reference has no controller-supplied context"
    references = [reference for reference in context.references if reference.correction_identity == identity]
    if len(references) != 1:
        return None, f"Canonical correction reference {identity!r} was supplied {len(references)} times; expected exactly once in this invocation"
    reference = references[0]
    if not scope_fingerprint or scope_fingerprint != reference.scope_fingerprint:
        return None, "Canonical correction reference contradicts the immutable scope binding"
    if not context_fingerprint or context_fingerprint != context.fingerprint:
        return None, "Canonical correction reference contradicts the invocation context binding"
    if tuple(sorted(set(requirement_ids))) != reference.requirement_ids:
        return None, "Canonical correction reference contradicts the qualified requirements"
    if authoritative_boundary != reference.authoritative_boundary:
        return None, "Canonical correction reference contradicts the authoritative boundary"
    if category != reference.category and {category, reference.category} != {"IMPLEMENTATION", "TEST_ORACLE"}:
        return None, "Canonical correction reference has an unsupported category transition"
    return reference, ""


def reference_target_error(context: CanonicalCorrectionContext, api_origin: str, repository: str, pr_number: int, head_sha: str, base_sha: str) -> str:
    if (context.api_origin, context.repository, context.pr_number, context.head_sha, context.base_sha) != (normalize_api_origin(api_origin), repository, pr_number, head_sha, base_sha):
        return "Canonical correction context does not belong to this repository, PR and target"
    return ""
