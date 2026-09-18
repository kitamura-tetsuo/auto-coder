"""Cross-head PR finding reconciliation before idempotent GitHub publication.

Implements Stage S3 of convergent PR review tracking (#2134, GitHub Issue #2137):
reconciles repeated PR review observations with existing blockers and GitHub threads
before publication, preventing duplicate roots across heads while preserving distinct defects.
"""

from __future__ import annotations

import re
import string
import uuid
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Optional, Sequence

if TYPE_CHECKING:
    from .github_app_reviewer import ReviewerAppIdentity
from .adversarial_validator import (
    AdversarialValidationFinding,
    AdversarialValidationResult,
    TestOracleGap,
)
from .canonical_pr_blocker_ledger import (
    AssociationAmbiguityError,
    BlockerAdmissionPayload,
    BlockerAlias,
    BlockerLedgerSnapshot,
    BlockerSnapshot,
    CanonicalPRBlockerLedger,
    CorrectionScope,
    QualifiedRequirement,
    ReconciliationDecision,
)
from .logger_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Domain Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HistoricalCorrection:
    """One independently actionable correction extracted from a GitHub review comment."""

    comment_id: int = 0
    category: str = "IMPLEMENTATION"
    blocker_id: Optional[str] = None
    gap_id: Optional[str] = None
    requirement_ids: tuple[str, ...] = ()
    authoritative_boundary: str = ""
    incorrect_behavior_or_invariant: str = ""
    required_outcome: str = ""
    body_text: str = ""
    path: str = ""
    line: Optional[int] = None
    commit_id: str = ""


@dataclass(frozen=True)
class HistoricalRootParseResult:
    """Result of parsing and authenticating historical PR review comments."""

    corrections: tuple[HistoricalCorrection, ...] = ()
    unverified_comment_ids: tuple[int, ...] = ()
    reply_comment_ids: tuple[int, ...] = ()
    all_authenticated_root_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ObservationCandidate:
    """Normalized observation scope to reconcile against ledger blockers."""

    source_type: str = ""  # "FINDING" or "TEST_ORACLE_GAP"
    category: str = ""  # "IMPLEMENTATION", "TEST_ORACLE", "SPECIFICATION", "REGRESSION"
    requirement_ids: tuple[str, ...] = ()
    authoritative_boundary: str = ""
    incorrect_behavior_or_invariant: str = ""
    required_outcome: str = ""
    original_objective_anchor: Optional[str] = None
    observation_identity: str = ""
    evidence: str = ""
    finding_ref: Optional[AdversarialValidationFinding] = None
    gap_ref: Optional[TestOracleGap] = None


@dataclass(frozen=True)
class FindingReconciliationResult:
    """Outcome of reconciling an entire validation result against the ledger."""

    snapshot: Optional[BlockerLedgerSnapshot] = None
    already_rooted_blocker_ids: tuple[str, ...] = ()
    unrooted_blocker_ids: tuple[str, ...] = ()
    blocker_for_finding: tuple[tuple[int, str], ...] = ()  # (finding_index, blocker_id)
    blocker_for_gap: tuple[tuple[str, str], ...] = ()  # (gap_id, blocker_id)
    unrooted_findings: tuple[AdversarialValidationFinding, ...] = ()
    unrooted_finding_blockers: tuple[str, ...] = ()
    unrooted_gaps: tuple[TestOracleGap, ...] = ()
    is_ambiguous: bool = False
    ambiguity_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Historical Comment Parsing (REQ-004, REQ-006, AS-002)
# ---------------------------------------------------------------------------


_BLOCKER_ID_RE = re.compile(r"(?:Blocker identity:\s*`?([^`\s\n]+)`?|blocker_id[=:]\s*`?([^`\s\n]+)`?)", re.IGNORECASE)
_GAP_ID_RE = re.compile(r"(?:Gap identity:\s*`?([^`\s\n]+)`?|gap_id[=:]\s*`?([^`\s\n]+)`?|TEST_ORACLE_GAP\s+([a-zA-Z0-9_-]+))", re.IGNORECASE)
_REQ_ID_RE = re.compile(r"\b(REQ-[0-9A-Za-z_-]+)\b")


def parse_historical_pr_review_roots(
    comments: Sequence[dict[str, object]],
    reviewer_identity: Optional[ReviewerAppIdentity] = None,
    repo_name: str = "",
    pr_number: int = 0,
) -> HistoricalRootParseResult:
    """Parse historical review comments across all pages, filtering by authentic root status.

    Per REQ-004 and REQ-006:
    - Exclude comments with `in_reply_to_id` (replies are not roots).
    - Exclude comments from unverified authors (must match reviewer_identity).
    - Forged matching markers from unverified authors grant no authority and establish no aliases.
    - Parse compound roots owning multiple corrections, retaining all obligations.
    """
    corrections: list[HistoricalCorrection] = []
    unverified_ids: list[int] = []
    reply_ids: list[int] = []
    authenticated_root_ids: list[int] = []

    for comment in comments:
        cid = comment.get("id")
        if not isinstance(cid, int) or cid <= 0:
            continue

        in_reply_to = comment.get("in_reply_to_id")
        if in_reply_to is not None:
            reply_ids.append(cid)
            continue

        user = comment.get("user")
        user_login = user.get("login") if isinstance(user, dict) else None
        if reviewer_identity is not None and not reviewer_identity.matches_login(user_login):
            unverified_ids.append(cid)
            continue

        authenticated_root_ids.append(cid)
        body = str(comment.get("body", ""))
        path = str(comment.get("path", ""))
        raw_line = comment.get("line")
        line_val: Optional[int] = raw_line if isinstance(raw_line, int) else None
        commit_id = str(comment.get("commit_id", ""))

        # Check for compound roots: split by headings, issue markers, or dividers
        parts = re.split(r"(?=(?:### Auto-Coder |### (?:Adversarial|Material)|Issue \d+:|\n\n---\n\n))", body)
        sections = [p.strip() for p in parts if p.strip()]
        if not sections:
            sections = [body]

        for section in sections:
            parsed = _parse_comment_section(section, cid, path, line_val, commit_id)
            if parsed is not None:
                corrections.append(parsed)

    return HistoricalRootParseResult(
        corrections=tuple(corrections),
        unverified_comment_ids=tuple(unverified_ids),
        reply_comment_ids=tuple(reply_ids),
        all_authenticated_root_ids=tuple(authenticated_root_ids),
    )


def _parse_comment_section(
    section_text: str,
    comment_id: int,
    path: str,
    line: Optional[int],
    commit_id: str,
) -> Optional[HistoricalCorrection]:
    """Parse a single finding or gap section within a review comment."""
    lower_text = section_text.lower()
    is_gap = "test-oracle gap" in lower_text or "test oracle gap" in lower_text or "test_oracle" in lower_text or "gap identity" in lower_text
    is_finding = "adversarial finding" in lower_text or "finding" in lower_text or "violated requirement" in lower_text or "requirement:" in lower_text or bool(_REQ_ID_RE.search(section_text))
    if not (is_gap or is_finding or _BLOCKER_ID_RE.search(section_text) or _GAP_ID_RE.search(section_text)):
        return None

    blocker_id = None
    blocker_match = _BLOCKER_ID_RE.search(section_text)
    if blocker_match:
        blocker_id = next((g.strip() for g in blocker_match.groups() if g), None)

    gap_id = None
    gap_match = _GAP_ID_RE.search(section_text)
    if gap_match:
        gap_id = next((g.strip() for g in gap_match.groups() if g), None)

    req_ids = tuple(dict.fromkeys(_REQ_ID_RE.findall(section_text)))
    category = "TEST_ORACLE" if is_gap else "IMPLEMENTATION"

    boundary = _extract_section_field(section_text, "Authoritative boundary")
    if not boundary:
        boundary = _extract_section_field(section_text, "Reachable path")
    if not boundary:
        bound_m = re.search(r"(?:Path|Boundary|File):\s*([^\s\n]+)", section_text, re.IGNORECASE)
        if bound_m:
            boundary = bound_m.group(1).strip()
        else:
            boundary = path or "repository"

    invariant = _extract_section_field(section_text, "Protected invariant")
    if not invariant:
        invariant = _extract_section_field(section_text, "Actual behavior")
    if not invariant:
        invariant = _extract_section_field(section_text, "Minimal plausible incorrect implementation")
    if not invariant:
        for line_entry in section_text.splitlines():
            l_str = line_entry.strip()
            if l_str and not l_str.startswith("#") and not l_str.startswith("*") and not l_str.lower().startswith("requirement") and not l_str.lower().startswith("path"):
                invariant = l_str
                break
    if not invariant:
        invariant = section_text[:200].strip()

    outcome = _extract_section_field(section_text, "Focused regression scenario requested")
    if not outcome:
        outcome = _extract_section_field(section_text, "Required behavior")
    if not outcome:
        outcome = _extract_section_field(section_text, "Suggested regression scenario")
    if not outcome:
        outcome = invariant or section_text[:200].strip()

    return HistoricalCorrection(
        comment_id=comment_id,
        category=category,
        blocker_id=blocker_id,
        gap_id=gap_id,
        requirement_ids=req_ids,
        authoritative_boundary=boundary,
        incorrect_behavior_or_invariant=invariant,
        required_outcome=outcome,
        body_text=section_text,
        path=path,
        line=line,
        commit_id=commit_id,
    )


def _extract_section_field(text: str, heading: str) -> str:
    """Extract the markdown content under a bold heading `**<heading>**`."""
    pattern = rf"\*\*{re.escape(heading)}\*\*\s*\n+([^\n*#]+(?:\n[^\n*#]+)*)"
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return ""


# ---------------------------------------------------------------------------
# Scope Extraction & Normalization
# ---------------------------------------------------------------------------


def extract_observation_candidate_from_finding(
    finding: AdversarialValidationFinding,
    issue_number: int = 0,
) -> ObservationCandidate:
    """Extract normalized observation candidate from an ordinary finding."""
    req_ids = tuple(finding.all_requirement_ids)
    boundary = finding.anchor_path or finding.reachability or "repository"
    incorrect_behavior = finding.actual_behavior or finding.counterexample or finding.evidence or "Demonstrated specification violation"
    required_outcome = finding.required_behavior or finding.suggested_regression_scenario or "Corrective behavior according to specification"
    obs_id = finding.finding_identity or finding.correction_identity or f"obs_{uuid.uuid4().hex[:8]}"

    return ObservationCandidate(
        source_type="FINDING",
        category="IMPLEMENTATION",
        requirement_ids=req_ids,
        authoritative_boundary=boundary,
        incorrect_behavior_or_invariant=incorrect_behavior,
        required_outcome=required_outcome,
        observation_identity=obs_id,
        evidence=finding.evidence,
        finding_ref=finding,
    )


def extract_observation_candidate_from_gap(
    gap: TestOracleGap,
    issue_number: int = 0,
) -> ObservationCandidate:
    """Extract normalized observation candidate from a material test-oracle gap."""
    req_ids = (gap.requirement_id,) if gap.requirement_id else ()
    boundary = gap.authoritative_boundary or gap.anchor_path or "repository"
    incorrect_behavior = gap.invariant or gap.plausible_incorrect_implementation or "Missing regression test protection"
    required_outcome = gap.focused_regression_scenario or gap.material_consequence or "Add focused regression test"
    obs_id = gap.gap_id or f"gap_{uuid.uuid4().hex[:8]}"

    return ObservationCandidate(
        source_type="TEST_ORACLE_GAP",
        category="TEST_ORACLE",
        requirement_ids=req_ids,
        authoritative_boundary=boundary,
        incorrect_behavior_or_invariant=incorrect_behavior,
        required_outcome=required_outcome,
        observation_identity=obs_id,
        gap_ref=gap,
    )


# ---------------------------------------------------------------------------
# Equivalence & Semantic Matching (REQ-002, REQ-011)
# ---------------------------------------------------------------------------


_FILL_WORDS = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "was",
    "were",
    "to",
    "in",
    "on",
    "at",
    "by",
    "for",
    "with",
    "about",
    "against",
    "between",
    "into",
    "through",
    "during",
    "before",
    "after",
    "above",
    "below",
    "from",
    "up",
    "down",
    "of",
    "off",
    "over",
    "under",
    "again",
    "further",
    "then",
    "once",
    "here",
    "there",
    "when",
    "where",
    "why",
    "how",
    "all",
    "any",
    "both",
    "each",
    "few",
    "more",
    "most",
    "other",
    "some",
    "such",
    "no",
    "nor",
    "not",
    "only",
    "own",
    "same",
    "so",
    "than",
    "too",
    "very",
    "can",
    "will",
    "just",
    "should",
    "now",
    "must",
    "be",
    "does",
    "do",
    "did",
    "issue",
    "defect",
    "finding",
    "problem",
    "note",
}


def _normalize_tokens(text: str) -> set[str]:
    """Tokenize and filter text for robust semantic comparison across paraphrases."""
    clean = text.lower().translate(str.maketrans(string.punctuation, " " * len(string.punctuation)))
    tokens = clean.split()
    return {t for t in tokens if len(t) > 2 and t not in _FILL_WORDS}


def _normalize_boundary(boundary: str) -> str:
    """Normalize boundary path/component string."""
    return boundary.strip().lower().replace("\\", "/").rstrip("/")


def scopes_describe_same_blocker(
    cand: ObservationCandidate,
    blocker: BlockerSnapshot,
    advisory_mode: bool = False,
) -> bool:
    """Determine whether an observation candidate describes the same blocker as an existing one.

    Per REQ-002:
    - Same independently actionable correction on the same authoritative boundary
      with the same incorrect behavior / protected invariant and corrective outcome.
    - Wording, head, requirement-order, evidence-location, thread-anchor, backend,
      or category presentation changes ALONE must preserve that ID.
    - Independently actionable corrections with materially different boundaries or
      outcomes remain separate even if they share a requirement or source file.
    """
    if cand.category and blocker.category and cand.category != blocker.category:
        if not advisory_mode:
            return False

    cand_b = _normalize_boundary(cand.authoritative_boundary)
    blk_b = _normalize_boundary(blocker.authoritative_boundary)

    boundaries_match = cand_b == blk_b or cand_b in blk_b or blk_b in cand_b
    if not boundaries_match and cand_b and blk_b:
        return False

    cand_reqs = set(cand.requirement_ids)
    blk_reqs = {r.requirement_id for r in blocker.qualified_requirements}
    if cand_reqs and blk_reqs:
        if not cand_reqs.intersection(blk_reqs):
            return False

    cand_outcome_tokens = _normalize_tokens(cand.required_outcome)
    blk_outcome_tokens = _normalize_tokens(blocker.required_correction_outcome)

    cand_behavior_tokens = _normalize_tokens(cand.incorrect_behavior_or_invariant)
    blk_behavior_tokens = _normalize_tokens(blocker.incorrect_behavior_or_missing_invariant)

    outcome_overlap = len(cand_outcome_tokens.intersection(blk_outcome_tokens)) if cand_outcome_tokens and blk_outcome_tokens else 0
    behavior_overlap = len(cand_behavior_tokens.intersection(blk_behavior_tokens)) if cand_behavior_tokens and blk_behavior_tokens else 0

    if cand_outcome_tokens and blk_outcome_tokens and outcome_overlap == 0:
        if behavior_overlap == 0:
            return False

    if boundaries_match:
        if outcome_overlap > 0 or behavior_overlap > 0:
            return True
        if not cand_outcome_tokens or not blk_outcome_tokens:
            return True

    return False


def advisory_semantic_match(
    cand: ObservationCandidate,
    candidate_blockers: Sequence[BlockerSnapshot],
) -> tuple[ReconciliationDecision, Optional[str], Optional[str]]:
    """Advisory semantic matching routine per REQ-003 and REQ-011.

    Returns:
        (decision, matched_blocker_id, justification_or_reason)
    """
    matches: list[BlockerSnapshot] = []
    for b in candidate_blockers:
        if scopes_describe_same_blocker(cand, b, advisory_mode=True):
            matches.append(b)

    if len(matches) == 1:
        matched = matches[0]
        category_transition = cand.category != matched.category
        reason = f"Paraphrased correction on boundary {matched.authoritative_boundary!r} " f"with category transition {matched.category} -> {cand.category}" if category_transition else f"Paraphrased observation for existing blocker {matched.blocker_id}"
        return ReconciliationDecision.ASSOCIATE, matched.blocker_id, reason

    if len(matches) > 1:
        reason = f"Observation {cand.observation_identity!r} ambiguously matches {len(matches)} blockers: {[b.blocker_id for b in matches]}"
        return ReconciliationDecision.AMBIGUOUS, None, reason

    return ReconciliationDecision.DISTINCT_DEFECT, None, "Materially distinct correction scope"


# ---------------------------------------------------------------------------
# High-Level Reconciliation Coordinator (REQ-001, REQ-004, REQ-005, REQ-010)
# ---------------------------------------------------------------------------


def reconcile_pr_findings_before_publication(
    ledger: CanonicalPRBlockerLedger,
    api_origin: str,
    repo_name: str,
    pr_number: int,
    issue_number: int,
    head_sha: str,
    base_sha: str,
    val_result: AdversarialValidationResult,
    historical_parse: HistoricalRootParseResult,
    attempt_id: str = "",
    expected_ledger_revision: Optional[int] = None,
) -> FindingReconciliationResult:
    """Coordinate reconciliation of validation observations against the ledger and GitHub roots.

    Performs:
    1. Namespace initialization if necessary.
    2. Historical root import: groups authenticated root comments, picks earliest numeric ID
       as canonical publication target, records aliases, and preserves compound root obligations (REQ-004).
    3. Rereview observation reconciliation: matches each finding and gap against candidate scopes.
    4. Categorizes items into already-rooted (reuse canonical root) vs unrooted (require publication).
    5. Detects ambiguity and halts speculative publication (REQ-003).
    """
    try:
        snapshot = ledger.get_snapshot(api_origin, repo_name, pr_number, require_retained_state=True)
    except Exception:
        snapshot = ledger.initialize_namespace(api_origin, repo_name, pr_number)

    rev = snapshot.ledger_revision if expected_ledger_revision is None else expected_ledger_revision

    rev = _bootstrap_historical_roots(
        ledger,
        api_origin,
        repo_name,
        pr_number,
        issue_number,
        head_sha,
        base_sha,
        historical_parse.corrections,
        rev,
    )
    snapshot = ledger.get_snapshot(api_origin, repo_name, pr_number, require_retained_state=True)

    candidates: list[ObservationCandidate] = []
    for finding in val_result.findings:
        candidates.append(extract_observation_candidate_from_finding(finding, issue_number))
    for gap in val_result.open_test_oracle_gaps:
        candidates.append(extract_observation_candidate_from_gap(gap, issue_number))

    already_rooted: list[str] = []
    unrooted: list[str] = []
    blocker_for_finding: list[tuple[int, str]] = []
    blocker_for_gap: list[tuple[str, str]] = []
    unrooted_findings: list[AdversarialValidationFinding] = []
    unrooted_finding_blockers: list[str] = []
    unrooted_gaps: list[TestOracleGap] = []

    finding_idx = 0
    for cand in candidates:
        open_blockers = snapshot.get_open_blockers()
        all_blockers = snapshot.blockers

        matched_blocker_id: Optional[str] = None
        if cand.gap_ref and cand.gap_ref.gap_id:
            for b in all_blockers:
                for alias in b.aliases:
                    if alias.alias_type == "test_oracle_gap" and alias.alias_value == cand.gap_ref.gap_id:
                        matched_blocker_id = b.blocker_id
                        break
                if matched_blocker_id:
                    break

        if not matched_blocker_id:
            decision, associated_id, reason = advisory_semantic_match(cand, open_blockers)
        else:
            decision, associated_id, reason = ReconciliationDecision.ASSOCIATE, matched_blocker_id, "Matched explicit TOG ID alias"

        if decision == ReconciliationDecision.AMBIGUOUS:
            return FindingReconciliationResult(
                snapshot=snapshot,
                is_ambiguous=True,
                ambiguity_reason=reason,
            )

        if decision == ReconciliationDecision.ASSOCIATE and associated_id:
            effective_id = associated_id
            justified_transition = False
            transition_reason = ""
            target_blocker = snapshot.get_blocker(effective_id)
            if target_blocker and target_blocker.category != cand.category:
                justified_transition = True
                transition_reason = f"Justified category transition from {target_blocker.category} to {cand.category}"

            payload = BlockerAdmissionPayload(
                category=cand.category,
                qualified_requirements=tuple(QualifiedRequirement(issue_number=issue_number, requirement_id=r) for r in cand.requirement_ids),
                authoritative_boundary=cand.authoritative_boundary,
                incorrect_behavior_or_missing_invariant=cand.incorrect_behavior_or_invariant,
                required_correction_outcome=cand.required_outcome,
                evidence_needed=cand.required_outcome,
                original_objective_anchor=cand.original_objective_anchor,
                accepted_scope=CorrectionScope(
                    description=cand.incorrect_behavior_or_invariant,
                    concern_ids=(cand.observation_identity,),
                ),
                evidence=cand.evidence,
                reviewed_head_sha=head_sha,
                reviewed_base_sha=base_sha,
                review_attempt_id=attempt_id,
                observation_identity=cand.observation_identity,
            )
            op_id = f"reconcile_{uuid.uuid4().hex[:10]}"
            try:
                effective_id, snapshot = ledger.reconcile_observation(
                    api_origin,
                    repo_name,
                    pr_number,
                    operation_id=op_id,
                    expected_ledger_revision=snapshot.ledger_revision,
                    candidate_payload=payload,
                    blocker_ids_considered=tuple(b.blocker_id for b in open_blockers),
                    decision=ReconciliationDecision.ASSOCIATE,
                    associated_blocker_id=effective_id,
                    evidence=cand.evidence,
                    review_observation_identity=cand.observation_identity,
                    justified_category_transition=justified_transition,
                    category_transition_reason=transition_reason,
                )
            except AssociationAmbiguityError as exc:
                return FindingReconciliationResult(
                    snapshot=snapshot,
                    is_ambiguous=True,
                    ambiguity_reason=str(exc),
                )
        else:
            aliases_to_add: list[BlockerAlias] = []
            if cand.gap_ref and cand.gap_ref.gap_id:
                aliases_to_add.append(BlockerAlias(alias_type="test_oracle_gap", alias_value=cand.gap_ref.gap_id))

            payload = BlockerAdmissionPayload(
                category=cand.category,
                qualified_requirements=tuple(QualifiedRequirement(issue_number=issue_number, requirement_id=r) for r in cand.requirement_ids),
                authoritative_boundary=cand.authoritative_boundary,
                incorrect_behavior_or_missing_invariant=cand.incorrect_behavior_or_invariant,
                required_correction_outcome=cand.required_outcome,
                evidence_needed=cand.required_outcome,
                original_objective_anchor=cand.original_objective_anchor,
                accepted_scope=CorrectionScope(
                    description=cand.incorrect_behavior_or_invariant,
                    concern_ids=(cand.observation_identity,),
                ),
                aliases=tuple(aliases_to_add),
                evidence=cand.evidence,
                reviewed_head_sha=head_sha,
                reviewed_base_sha=base_sha,
                review_attempt_id=attempt_id,
                observation_identity=cand.observation_identity,
            )
            op_id = f"admit_{uuid.uuid4().hex[:10]}"
            effective_id, snapshot = ledger.admit_blocker(
                api_origin,
                repo_name,
                pr_number,
                operation_id=op_id,
                expected_ledger_revision=snapshot.ledger_revision,
                payload=payload,
                review_observation_identity=cand.observation_identity,
            )

        blk = snapshot.get_blocker(effective_id)
        canonical_root = blk.get_canonical_root_comment_id() if blk else None

        if canonical_root is not None:
            already_rooted.append(effective_id)
        else:
            unrooted.append(effective_id)
            if cand.finding_ref:
                unrooted_findings.append(cand.finding_ref)
                unrooted_finding_blockers.append(effective_id)
            if cand.gap_ref:
                unrooted_gaps.append(cand.gap_ref)

        if cand.finding_ref:
            blocker_for_finding.append((finding_idx, effective_id))
            finding_idx += 1
        if cand.gap_ref:
            blocker_for_gap.append((cand.gap_ref.gap_id, effective_id))

    return FindingReconciliationResult(
        snapshot=snapshot,
        already_rooted_blocker_ids=tuple(dict.fromkeys(already_rooted)),
        unrooted_blocker_ids=tuple(dict.fromkeys(unrooted)),
        blocker_for_finding=tuple(blocker_for_finding),
        blocker_for_gap=tuple(blocker_for_gap),
        unrooted_findings=tuple(unrooted_findings),
        unrooted_finding_blockers=tuple(unrooted_finding_blockers),
        unrooted_gaps=tuple(unrooted_gaps),
        is_ambiguous=False,
    )


def _bootstrap_historical_roots(
    ledger: CanonicalPRBlockerLedger,
    api_origin: str,
    repo_name: str,
    pr_number: int,
    issue_number: int,
    head_sha: str,
    base_sha: str,
    corrections: Sequence[HistoricalCorrection],
    current_rev: int,
) -> int:
    """Bootstrap historical review roots into the canonical ledger.

    Per REQ-004:
    - Retain every imported root's complete concern ownership and provenance.
    - Select earliest authenticated equivalent root by numeric comment ID as canonical target.
    - Retain other equivalent roots as aliases without automatically claiming them fixed.
    - A compound root owning several corrections retains all of them.
    """
    if not corrections:
        return current_rev

    groups: list[list[HistoricalCorrection]] = []
    for corr in corrections:
        placed = False
        for g in groups:
            rep = g[0]
            if rep.category != corr.category:
                continue
            cand_rep = ObservationCandidate(
                source_type="FINDING" if rep.category != "TEST_ORACLE" else "TEST_ORACLE_GAP",
                category=rep.category,
                requirement_ids=rep.requirement_ids,
                authoritative_boundary=rep.authoritative_boundary,
                incorrect_behavior_or_invariant=rep.incorrect_behavior_or_invariant,
                required_outcome=rep.required_outcome,
            )
            target = BlockerSnapshot(
                category=corr.category,
                qualified_requirements=tuple(QualifiedRequirement(issue_number=issue_number, requirement_id=r) for r in corr.requirement_ids),
                authoritative_boundary=corr.authoritative_boundary,
                incorrect_behavior_or_missing_invariant=corr.incorrect_behavior_or_invariant,
                required_correction_outcome=corr.required_outcome,
            )
            if scopes_describe_same_blocker(cand_rep, target):
                g.append(corr)
                placed = True
                break
        if not placed:
            groups.append([corr])

    for g in groups:
        sorted_by_id = sorted(g, key=lambda c: c.comment_id)
        earliest = sorted_by_id[0]

        snapshot = ledger.get_snapshot(api_origin, repo_name, pr_number, require_retained_state=True)
        cand_earliest = ObservationCandidate(
            source_type="FINDING" if earliest.category != "TEST_ORACLE" else "TEST_ORACLE_GAP",
            category=earliest.category,
            requirement_ids=earliest.requirement_ids,
            authoritative_boundary=earliest.authoritative_boundary,
            incorrect_behavior_or_invariant=earliest.incorrect_behavior_or_invariant,
            required_outcome=earliest.required_outcome,
        )
        matching_blocker: Optional[BlockerSnapshot] = None
        for b in snapshot.blockers:
            has_comment = any(a.alias_type == "github_root_comment" and a.alias_value == str(earliest.comment_id) for a in b.aliases)
            if has_comment and scopes_describe_same_blocker(cand_earliest, b):
                matching_blocker = b
                break

        if matching_blocker is None:
            aliases: list[BlockerAlias] = [BlockerAlias(alias_type="github_root_comment", alias_value=str(corr.comment_id)) for corr in sorted_by_id]
            if earliest.gap_id:
                aliases.append(BlockerAlias(alias_type="test_oracle_gap", alias_value=earliest.gap_id))

            payload = BlockerAdmissionPayload(
                category=earliest.category,
                qualified_requirements=tuple(QualifiedRequirement(issue_number=issue_number, requirement_id=r) for r in earliest.requirement_ids),
                authoritative_boundary=earliest.authoritative_boundary,
                incorrect_behavior_or_missing_invariant=earliest.incorrect_behavior_or_invariant,
                required_correction_outcome=earliest.required_outcome,
                evidence_needed=earliest.required_outcome,
                accepted_scope=CorrectionScope(
                    description=earliest.incorrect_behavior_or_invariant,
                    concern_ids=(f"hist_{earliest.comment_id}_{earliest.category.lower()}",),
                ),
                aliases=tuple(aliases),
                reviewed_head_sha=earliest.commit_id or head_sha,
                reviewed_base_sha=base_sha,
                observation_identity=f"import_{earliest.comment_id}_{earliest.category.lower()}",
            )
            op_id = f"import_{earliest.comment_id}_{earliest.category.lower()}_{uuid.uuid4().hex[:6]}"
            _, snapshot = ledger.admit_blocker(
                api_origin,
                repo_name,
                pr_number,
                operation_id=op_id,
                expected_ledger_revision=snapshot.ledger_revision,
                payload=payload,
                review_observation_identity=f"import_{earliest.comment_id}",
            )
            current_rev = snapshot.ledger_revision
        else:
            for corr in sorted_by_id:
                has_alias = any(a.alias_type == "github_root_comment" and a.alias_value == str(corr.comment_id) for a in matching_blocker.aliases)
                if not has_alias:
                    op_id = f"alias_{matching_blocker.blocker_id}_{corr.comment_id}"
                    snapshot = ledger.add_alias(
                        api_origin,
                        repo_name,
                        pr_number,
                        operation_id=op_id,
                        expected_ledger_revision=snapshot.ledger_revision,
                        blocker_id=matching_blocker.blocker_id,
                        alias_type="github_root_comment",
                        alias_value=str(corr.comment_id),
                    )
                    current_rev = snapshot.ledger_revision

    return current_rev
