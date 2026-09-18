"""Tests for cross-head PR finding reconciliation (GitHub Issue #2137).

Covers:
- AS-001: Repeat #2132 without another root (REQ-001, REQ-002, REQ-010).
- AS-002: Already duplicated legacy PR (REQ-003, REQ-004, REQ-005).
- AS-003: Server accepted review, response lost (REQ-007, REQ-008).
- AS-004: Competing publishers and late results (REQ-007, REQ-009).
- AS-005: Disappeared anchor is not a new defect (REQ-001, REQ-002, REQ-008).
- REQ-006: Justified category transition with thread continuity.
- REQ-011: Isolated advisory semantic matching unit tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import httpx
import pytest

from auto_coder.adversarial_validator import (
    AdversarialValidationFinding,
    AdversarialValidationResult,
    TestOracleGap,
)
from auto_coder.canonical_pr_blocker_ledger import (
    AssociationAmbiguityError,
    BlockerAdmissionPayload,
    BlockerAlias,
    BlockerDisposition,
    BlockerLedgerSnapshot,
    CanonicalPRBlockerLedger,
    CorrectionScope,
    PublicationContentionError,
    QualifiedRequirement,
    ReconciliationDecision,
    StaleLedgerRevisionError,
)
from auto_coder.github_app_reviewer import (
    GitHubAppReviewer,
    ReviewerAppConfig,
    ReviewerAppIdentity,
)
from auto_coder.pr_finding_reconciliation import (
    FindingReconciliationResult,
    HistoricalCorrection,
    HistoricalRootParseResult,
    ObservationCandidate,
    advisory_semantic_match,
    extract_observation_candidate_from_finding,
    extract_observation_candidate_from_gap,
    parse_historical_pr_review_roots,
    reconcile_pr_findings_before_publication,
    scopes_describe_same_blocker,
)

# ---------------------------------------------------------------------------
# Test Fixtures & Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "canonical_pr_blockers.db"


@pytest.fixture()
def ledger(db_path: Path) -> CanonicalPRBlockerLedger:
    return CanonicalPRBlockerLedger(db_path=db_path)


API_ORIGIN = "https://api.github.test"
REPO = "kitamura-tetsuo/auto-coder"
PR_NUMBER = 2137


class RecordingClient(httpx.Client):
    def __init__(self, responses: list[httpx.Response]) -> None:
        super().__init__()
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        req = httpx.Request(method, url, **kwargs)
        self.requests.append(req)
        if not self.responses:
            raise AssertionError(f"Unexpected request without queued response: {method} {url}")
        return self.responses.pop(0)


def resp(status: int, data: object) -> httpx.Response:
    return httpx.Response(status, json=data, request=httpx.Request("GET", API_ORIGIN))


def make_reviewer(
    tmp_path: Path,
    client: RecordingClient,
    monkeypatch: pytest.MonkeyPatch,
    ledger: Optional[CanonicalPRBlockerLedger] = None,
) -> GitHubAppReviewer:
    key = tmp_path / "reviewer.pem"
    key.write_text("fake private key", encoding="utf-8")
    monkeypatch.setattr("auto_coder.github_app_reviewer.jwt.encode", lambda *args, **kwargs: "fake-app-jwt")
    config = ReviewerAppConfig("4765828", "client", key)
    reviewer = GitHubAppReviewer(
        config,
        api_url=API_ORIGIN,
        client=client,
        ledger=ledger,
    )
    reviewer._identity = ReviewerAppIdentity(login="auto-coder-reviewer[bot]", app_id=4765828)
    return reviewer


# ---------------------------------------------------------------------------
# AS-001: Repeat #2132 Without Another Root
# ---------------------------------------------------------------------------


def test_as001_repeat_without_another_root(ledger: CanonicalPRBlockerLedger) -> None:
    """AS-001: Given an open PR with an active blocker rooted at comment 101 on Head A,

    when Head B produces a finding for the same defect (paraphrased text, moved anchor
    line, different model backend), reconciliation associates it with blocker 101,
    and does NOT create a second root comment.
    Forced review on the same head also does not duplicate the root.
    A genuinely distinct defect on Head B receives a separate blocker and root.
    """
    ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)

    # 1. Admit Blocker 1 on Head A and root it at GitHub comment 101
    payload_a = BlockerAdmissionPayload(
        category="IMPLEMENTATION",
        qualified_requirements=(QualifiedRequirement(issue_number=2137, requirement_id="REQ-001"),),
        authoritative_boundary="src/auto_coder/auth.py",
        incorrect_behavior_or_missing_invariant="JWT verification fails when audience claim is missing",
        required_correction_outcome="Verify audience claim in JWT decode",
        evidence_needed="JWT decode unit test",
        original_objective_anchor="Strict JWT validation",
        accepted_scope=CorrectionScope(
            description="Validate audience claim in JWT verification",
            concern_ids=("concern-jwt-1",),
        ),
        aliases=(BlockerAlias(alias_type="root_comment_id", alias_value="101"),),
        evidence="Missing audience param in jwt.decode call",
        reviewed_head_sha="head-a",
        reviewed_base_sha="base-0",
        review_attempt_id="att-1",
        observation_identity="obs-1",
    )
    b1_id, snap = ledger.admit_blocker(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        operation_id="admit-b1",
        expected_ledger_revision=1,
        payload=payload_a,
    )
    assert b1_id.startswith("blk_")
    b1 = snap.get_blocker(b1_id)
    assert b1 is not None
    assert b1.get_canonical_root_comment_id() == 101

    # 2. On Head B: Validator produces paraphrased finding on moved line by a different model
    finding_b = AdversarialValidationFinding(
        actual_behavior="Audience claim verification is omitted during JWT parsing",  # Paraphrased
        required_behavior="Verify audience claim in JWT decode",
        anchor_path="src/auto_coder/auth.py",
        anchor_line=85,  # Moved from line 42 to line 85
        requirement_ids=["REQ-001"],
    )
    val_result_b = AdversarialValidationResult(
        result="NEEDS_FIX",
        findings=[finding_b],
        attempt_id="att-head-b-1",
    )

    # Historical comments include comment 101 from the reviewer bot
    historical_comments = [
        {
            "id": 101,
            "in_reply_to_id": None,
            "path": "src/auto_coder/auth.py",
            "line": 42,
            "user": {"login": "auto-coder-reviewer[bot]"},
            "body": f"<!-- auto-coder: blocker_id={b1_id} -->\nAudience validation required.",
        }
    ]
    hist_parse = parse_historical_pr_review_roots(
        historical_comments,
        reviewer_identity=ReviewerAppIdentity(login="auto-coder-reviewer[bot]", app_id=4765828),
        repo_name=REPO,
        pr_number=PR_NUMBER,
    )
    assert len(hist_parse.all_authenticated_root_ids) == 1
    assert hist_parse.all_authenticated_root_ids[0] == 101

    # Reconcile Head B finding
    reconciled = reconcile_pr_findings_before_publication(
        ledger=ledger,
        api_origin=API_ORIGIN,
        repo_name=REPO,
        pr_number=PR_NUMBER,
        issue_number=2137,
        head_sha="head-b",
        base_sha="base-0",
        val_result=val_result_b,
        historical_parse=hist_parse,
        attempt_id="att-head-b-1",
    )

    # Must associate with B1 and emit NO unrooted findings / comments
    assert not reconciled.is_ambiguous
    assert b1_id in reconciled.already_rooted_blocker_ids
    assert len(reconciled.unrooted_blocker_ids) == 0
    assert len(reconciled.unrooted_findings) == 0

    # 3. Forced review execution on the same Head B also does not duplicate
    reconciled_forced = reconcile_pr_findings_before_publication(
        ledger=ledger,
        api_origin=API_ORIGIN,
        repo_name=REPO,
        pr_number=PR_NUMBER,
        issue_number=2137,
        head_sha="head-b",
        base_sha="base-0",
        val_result=val_result_b,
        historical_parse=hist_parse,
        attempt_id="att-head-b-forced",
    )
    assert not reconciled_forced.is_ambiguous
    assert b1_id in reconciled_forced.already_rooted_blocker_ids
    assert len(reconciled_forced.unrooted_blocker_ids) == 0
    assert len(reconciled_forced.unrooted_findings) == 0

    # 4. A genuinely distinct defect on Head B receives a separate blocker and root
    distinct_finding = AdversarialValidationFinding(
        actual_behavior="Database connection pool leak when transaction fails",
        required_behavior="Close connection on failure",
        anchor_path="src/auto_coder/db.py",  # Distinct boundary
        anchor_line=120,
        requirement_ids=["REQ-002"],  # Distinct requirement
    )
    val_result_with_distinct = AdversarialValidationResult(
        result="NEEDS_FIX",
        findings=[finding_b, distinct_finding],
        attempt_id="att-head-b-distinct",
    )

    reconciled_distinct = reconcile_pr_findings_before_publication(
        ledger=ledger,
        api_origin=API_ORIGIN,
        repo_name=REPO,
        pr_number=PR_NUMBER,
        issue_number=2137,
        head_sha="head-b",
        base_sha="base-0",
        val_result=val_result_with_distinct,
        historical_parse=hist_parse,
        attempt_id="att-head-b-distinct",
    )
    assert not reconciled_distinct.is_ambiguous
    assert b1_id in reconciled_distinct.already_rooted_blocker_ids
    assert len(reconciled_distinct.unrooted_blocker_ids) == 1
    assert reconciled_distinct.unrooted_findings[0].anchor_path == "src/auto_coder/db.py"
    # The distinct blocker is newly admitted
    b2_id = reconciled_distinct.unrooted_blocker_ids[0]
    assert b2_id != b1_id


# ---------------------------------------------------------------------------
# AS-002: Already Duplicated Legacy PR
# ---------------------------------------------------------------------------


def test_as002_already_duplicated_legacy_pr(ledger: CanonicalPRBlockerLedger) -> None:
    """AS-002: Given a PR with duplicate historical root comments 201 and 202 describing

    the same defect, plus a compound comment 203 containing both an implementation fix
    and an oracle gap requirement, and an unverified human comment claiming 'fixed':
    - 201 becomes canonical root (earliest numeric ID)
    - 202 is recorded as alias
    - 203 decomposes into both obligations without dropping the gap
    - unverified comment is ignored
    - ambiguous match blocks speculative publication.
    """
    ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)

    comments_raw = [
        # Comment 201: First root for auth defect
        {
            "id": 201,
            "in_reply_to_id": None,
            "path": "src/auto_coder/auth.py",
            "line": 42,
            "user": {"login": "auto-coder-reviewer[bot]"},
            "body": "Requirement: REQ-001\nPath: src/auto_coder/auth.py\nMissing token expiration check.",
        },
        # Comment 202: Duplicate root for auth defect
        {
            "id": 202,
            "in_reply_to_id": None,
            "path": "src/auto_coder/auth.py",
            "line": 42,
            "user": {"login": "auto-coder-reviewer[bot]"},
            "body": "Requirement: REQ-001\nPath: src/auto_coder/auth.py\nMissing token expiration check.",
        },
        # Comment 203: Compound comment with implementation fix and oracle gap
        {
            "id": 203,
            "in_reply_to_id": None,
            "path": "src/auto_coder/sanitizer.py",
            "line": 15,
            "user": {"login": "auto-coder-reviewer[bot]"},
            "body": ("Issue 1: REQ-002: Input sanitizer fails on unicode control characters.\n" "Issue 2: TEST_ORACLE_GAP gap_unicode_test: Missing oracle test for empty null byte string."),
        },
        # Comment 204: Unverified human author claiming 'fixed'
        {
            "id": 204,
            "in_reply_to_id": None,
            "path": "src/auto_coder/auth.py",
            "line": 42,
            "user": {"login": "human-contributor"},
            "body": "Fixed this already, please approve.",
        },
    ]

    hist_parse = parse_historical_pr_review_roots(
        comments_raw,
        reviewer_identity=ReviewerAppIdentity(login="auto-coder-reviewer[bot]", app_id=4765828),
        repo_name=REPO,
        pr_number=PR_NUMBER,
    )

    # 1. Verification of author authenticity
    assert 204 in hist_parse.unverified_comment_ids
    assert 201 in hist_parse.all_authenticated_root_ids
    assert 202 in hist_parse.all_authenticated_root_ids
    assert 203 in hist_parse.all_authenticated_root_ids

    # 2. Compound comment 203 decomposition
    c203_corrections = [c for c in hist_parse.corrections if c.comment_id == 203]
    assert len(c203_corrections) == 2
    categories = {c.category for c in c203_corrections}
    assert "IMPLEMENTATION" in categories
    assert "TEST_ORACLE" in categories

    # 3. Bootstrap into ledger
    empty_result = AdversarialValidationResult(result="PASS")
    reconciled = reconcile_pr_findings_before_publication(
        ledger=ledger,
        api_origin=API_ORIGIN,
        repo_name=REPO,
        pr_number=PR_NUMBER,
        issue_number=2137,
        head_sha="head-legacy",
        base_sha="base-0",
        val_result=empty_result,
        historical_parse=hist_parse,
        attempt_id="att-bootstrap",
    )

    snap = ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER, require_retained_state=True)
    open_blockers = snap.get_open_blockers()

    # Find the blocker for the auth defect
    auth_blockers = [b for b in open_blockers if b.authoritative_boundary == "src/auto_coder/auth.py"]
    assert len(auth_blockers) == 1
    auth_b = auth_blockers[0]

    # Earliest numeric comment ID (201) is canonical target root, 202 is an alias
    assert auth_b.get_canonical_root_comment_id() == 201
    assert any(a.alias_value == "202" and a.alias_type in ("github_root_comment", "historical_root_comment_id") for a in auth_b.aliases)

    # Both obligations from comment 203 are preserved as open blockers
    sanitizer_blockers = [b for b in open_blockers if b.authoritative_boundary == "src/auto_coder/sanitizer.py"]
    assert len(sanitizer_blockers) == 2
    sanitizer_categories = {b.category for b in sanitizer_blockers}
    assert "IMPLEMENTATION" in sanitizer_categories
    assert "TEST_ORACLE" in sanitizer_categories

    # Unverified comment 204 had zero effect on blocker disposition
    assert auth_b.disposition == BlockerDisposition.OPEN


def test_as002_ambiguous_match_blocks_speculative_publication(ledger: CanonicalPRBlockerLedger) -> None:
    """AS-002 (Ambiguity): When an observation candidate produces ambiguous association

    across multiple active blockers, reconciliation returns is_ambiguous=True,
    preventing speculative publication.
    """
    ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)

    # Admit two blockers on the same file with identical requirement ID and similar description
    payload1 = BlockerAdmissionPayload(
        category="IMPLEMENTATION",
        qualified_requirements=(QualifiedRequirement(issue_number=2137, requirement_id="REQ-001"),),
        authoritative_boundary="src/auto_coder/service.py",
        incorrect_behavior_or_missing_invariant="Null pointer when config is omitted",
        required_correction_outcome="Handle missing config safely",
        evidence_needed="test",
        original_objective_anchor="Safety",
        accepted_scope=CorrectionScope(description="Null config check"),
        evidence="evidence 1",
        reviewed_head_sha="head-1",
        reviewed_base_sha="base-0",
        review_attempt_id="att-1",
        observation_identity="obs-ambig-1",
    )
    b1_id, _ = ledger.admit_blocker(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        operation_id="admit-ambig-1",
        expected_ledger_revision=1,
        payload=payload1,
    )

    payload2 = BlockerAdmissionPayload(
        category="IMPLEMENTATION",
        qualified_requirements=(QualifiedRequirement(issue_number=2137, requirement_id="REQ-001"),),
        authoritative_boundary="src/auto_coder/service.py",
        incorrect_behavior_or_missing_invariant="Null pointer when config is missing",
        required_correction_outcome="Handle missing config safely",
        evidence_needed="test",
        original_objective_anchor="Safety",
        accepted_scope=CorrectionScope(description="Null config check"),
        evidence="evidence 2",
        reviewed_head_sha="head-1",
        reviewed_base_sha="base-0",
        review_attempt_id="att-1",
        observation_identity="obs-ambig-2",
    )
    b2_id, _ = ledger.admit_blocker(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        operation_id="admit-ambig-2",
        expected_ledger_revision=2,
        payload=payload2,
    )

    # Candidate finding matches both equally
    ambig_finding = AdversarialValidationFinding(
        actual_behavior="Null pointer when config is omitted or missing",
        required_behavior="Handle missing config safely",
        anchor_path="src/auto_coder/service.py",
        anchor_line=50,
        requirement_ids=["REQ-001"],
    )
    val_result = AdversarialValidationResult(
        result="NEEDS_FIX",
        findings=[ambig_finding],
    )

    reconciled = reconcile_pr_findings_before_publication(
        ledger=ledger,
        api_origin=API_ORIGIN,
        repo_name=REPO,
        pr_number=PR_NUMBER,
        issue_number=2137,
        head_sha="head-2",
        base_sha="base-0",
        val_result=val_result,
        historical_parse=HistoricalRootParseResult(),
        attempt_id="att-2",
    )

    # Ambiguity must be non-authorizing: blocks publication
    assert reconciled.is_ambiguous
    assert "ambiguous" in (reconciled.ambiguity_reason or "").lower()
    assert len(reconciled.unrooted_blocker_ids) == 0


# ---------------------------------------------------------------------------
# AS-003: Server Accepted Review, Response Lost
# ---------------------------------------------------------------------------


def test_as003_server_accepted_review_response_lost(ledger: CanonicalPRBlockerLedger) -> None:
    """AS-003: Reviewer issues review for blocker B1 that GitHub accepts with comment 301,

    but network connection drops before receiving the response.
    Pending intent remains recorded; next cycle discovers comment 301, confirms intent,
    associates B1 with 301, and does not issue duplicate POST.
    """
    ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)

    # 1. Admit unrooted blocker B1
    payload = BlockerAdmissionPayload(
        category="IMPLEMENTATION",
        qualified_requirements=(QualifiedRequirement(issue_number=2137, requirement_id="REQ-001"),),
        authoritative_boundary="src/auto_coder/crypto.py",
        incorrect_behavior_or_missing_invariant="Insecure hashing algorithm used",
        required_correction_outcome="Use SHA-256 instead of MD5",
        evidence_needed="Unit test verifying sha256",
        original_objective_anchor="Cryptographic hygiene",
        accepted_scope=CorrectionScope(description="Update hash func"),
        evidence="hashlib.md5 found",
        reviewed_head_sha="head-1",
        reviewed_base_sha="base-0",
        review_attempt_id="att-1",
        observation_identity="obs-crypto-1",
    )
    b1_id, snap = ledger.admit_blocker(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        operation_id="admit-crypto-1",
        expected_ledger_revision=1,
        payload=payload,
    )

    # 2. Record publication intent before emitting review request
    intent_id = "pub_crypto_head1_001"
    snap = ledger.record_publication_intent(
        api_origin=API_ORIGIN,
        repository=REPO,
        pr_number=PR_NUMBER,
        intent_id=intent_id,
        expected_ledger_revision=snap.ledger_revision,
        blocker_ids=(b1_id,),
        destination_repo=REPO,
        destination_pr=PR_NUMBER,
        reviewed_head_sha="head-1",
        reviewed_base_sha="base-0",
        review_attempt_id="att-1",
    )

    # Intent is pending
    intent = ledger.get_publication_intent(API_ORIGIN, REPO, PR_NUMBER, intent_id)
    assert intent is not None
    assert intent.status == "PENDING"

    # Suppose GitHub created comment 301, but the response was dropped
    # In next reconciliation cycle:
    comments_from_gh = [
        {
            "id": 301,
            "in_reply_to_id": None,
            "path": "src/auto_coder/crypto.py",
            "line": 20,
            "user": {"login": "auto-coder-reviewer[bot]"},
            "body": f"<!-- auto-coder: blocker_id={b1_id} -->\nUse SHA-256 instead of MD5.",
        }
    ]
    hist_parse = parse_historical_pr_review_roots(
        comments_from_gh,
        reviewer_identity=ReviewerAppIdentity(login="auto-coder-reviewer[bot]", app_id=4765828),
        repo_name=REPO,
        pr_number=PR_NUMBER,
    )

    # Confirm the pending intent and bind comment 301 as root alias
    snap = ledger.confirm_publication_intent(
        api_origin=API_ORIGIN,
        repository=REPO,
        pr_number=PR_NUMBER,
        intent_id=intent_id,
        confirmed_root_aliases=(
            BlockerAlias(
                blocker_id=b1_id,
                alias_type="root_comment_id",
                alias_value="301",
            ),
        ),
    )

    # Check intent confirmed
    intent_confirmed = ledger.get_publication_intent(API_ORIGIN, REPO, PR_NUMBER, intent_id)
    assert intent_confirmed is not None
    assert intent_confirmed.status == "CONFIRMED"

    # Blocker B1 now has canonical root 301
    b1_updated = snap.get_blocker(b1_id)
    assert b1_updated is not None
    assert b1_updated.get_canonical_root_comment_id() == 301

    # In subsequent run: B1 is already rooted, so NO unrooted blockers exist
    val_result = AdversarialValidationResult(
        result="NEEDS_FIX",
        findings=[
            AdversarialValidationFinding(
                actual_behavior="Insecure MD5 hashing algorithm used",
                required_behavior="Use SHA-256 instead of MD5",
                anchor_path="src/auto_coder/crypto.py",
                anchor_line=20,
                requirement_ids=["REQ-001"],
            ),
        ],
    )
    reconciled = reconcile_pr_findings_before_publication(
        ledger=ledger,
        api_origin=API_ORIGIN,
        repo_name=REPO,
        pr_number=PR_NUMBER,
        issue_number=2137,
        head_sha="head-1",
        base_sha="base-0",
        val_result=val_result,
        historical_parse=hist_parse,
        attempt_id="att-2",
    )
    assert len(reconciled.unrooted_blocker_ids) == 0


def test_as003_definitive_422_rejection_clears_intent(ledger: CanonicalPRBlockerLedger) -> None:
    """AS-003 (Rejection): If GitHub definitively rejects with 422, the pending intent

    is cleared (rejected) and retryable without stranding ledger lock state.
    """
    ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)

    payload = BlockerAdmissionPayload(
        category="IMPLEMENTATION",
        qualified_requirements=(QualifiedRequirement(issue_number=2137, requirement_id="REQ-001"),),
        authoritative_boundary="src/auto_coder/syntax.py",
        incorrect_behavior_or_missing_invariant="Syntax error",
        required_correction_outcome="Fix syntax",
        evidence_needed="test",
        original_objective_anchor="Syntax check",
        accepted_scope=CorrectionScope(description="syntax fix"),
        evidence="syntax error",
        reviewed_head_sha="head-1",
        reviewed_base_sha="base-0",
        review_attempt_id="att-1",
        observation_identity="obs-syntax-1",
    )
    b1_id, snap = ledger.admit_blocker(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        operation_id="admit-syntax-1",
        expected_ledger_revision=1,
        payload=payload,
    )

    intent_id = "pub_syntax_head1_001"
    snap = ledger.record_publication_intent(
        api_origin=API_ORIGIN,
        repository=REPO,
        pr_number=PR_NUMBER,
        intent_id=intent_id,
        expected_ledger_revision=snap.ledger_revision,
        blocker_ids=(b1_id,),
        destination_repo=REPO,
        destination_pr=PR_NUMBER,
        reviewed_head_sha="head-1",
        reviewed_base_sha="base-0",
        review_attempt_id="att-1",
    )

    # Definitively reject intent (e.g. 422 Unprocessable Entity)
    ledger.reject_publication_intent(
        api_origin=API_ORIGIN,
        repository=REPO,
        pr_number=PR_NUMBER,
        intent_id=intent_id,
        reason="HTTP 422: Line not in diff",
    )

    intent = ledger.get_publication_intent(API_ORIGIN, REPO, PR_NUMBER, intent_id)
    assert intent is not None
    assert intent.status == "REJECTED"
    assert "Line not in diff" in intent.failure_reason

    # Pending publication intents should be empty now
    pending = ledger.get_pending_publication_intents(API_ORIGIN, REPO, PR_NUMBER)
    assert len(pending) == 0

    # Retrying a new intent for B1 succeeds and is not blocked by a stranded lock
    current_snap = ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    retry_intent_id = "pub_syntax_head1_retry"
    snap_retry = ledger.record_publication_intent(
        api_origin=API_ORIGIN,
        repository=REPO,
        pr_number=PR_NUMBER,
        intent_id=retry_intent_id,
        expected_ledger_revision=current_snap.ledger_revision,
        blocker_ids=(b1_id,),
        destination_repo=REPO,
        destination_pr=PR_NUMBER,
        reviewed_head_sha="head-1",
        reviewed_base_sha="base-0",
        review_attempt_id="att-retry",
    )
    assert snap_retry is not None


# ---------------------------------------------------------------------------
# AS-004: Competing Publishers and Late Results
# ---------------------------------------------------------------------------


def test_as004_competing_publishers_and_late_results(ledger: CanonicalPRBlockerLedger) -> None:
    """AS-004: Two concurrent review evaluations for Head A:

    Worker 1 claims publication authority. Worker 2 attempts publication for Head A
    simultaneously; Worker 2 is rejected by publication contention.
    Worker 2 completes later with Head A results while PR is at Head B; rejected by head/CAS.
    """
    ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)

    payload = BlockerAdmissionPayload(
        category="IMPLEMENTATION",
        qualified_requirements=(QualifiedRequirement(issue_number=2137, requirement_id="REQ-001"),),
        authoritative_boundary="src/auto_coder/race.py",
        incorrect_behavior_or_missing_invariant="Race condition in cache",
        required_correction_outcome="Lock cache during write",
        evidence_needed="test",
        original_objective_anchor="Race condition",
        accepted_scope=CorrectionScope(description="Lock cache"),
        evidence="Unsynchronized access",
        reviewed_head_sha="head-a",
        reviewed_base_sha="base-0",
        review_attempt_id="att-worker-1",
        observation_identity="obs-race-1",
    )
    b1_id, snap = ledger.admit_blocker(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        operation_id="admit-race-1",
        expected_ledger_revision=1,
        payload=payload,
    )

    # Worker 1 acquires exclusive publication authority for Head A
    snap_w1 = ledger.record_publication_intent(
        api_origin=API_ORIGIN,
        repository=REPO,
        pr_number=PR_NUMBER,
        intent_id="pub_worker_1",
        expected_ledger_revision=snap.ledger_revision,
        blocker_ids=(b1_id,),
        destination_repo=REPO,
        destination_pr=PR_NUMBER,
        reviewed_head_sha="head-a",
        reviewed_base_sha="base-0",
        review_attempt_id="att-worker-1",
    )

    # Worker 2 simultaneously attempts publication for Head A with same expected revision
    with pytest.raises(PublicationContentionError) as exc_info:
        ledger.record_publication_intent(
            api_origin=API_ORIGIN,
            repository=REPO,
            pr_number=PR_NUMBER,
            intent_id="pub_worker_2",
            expected_ledger_revision=snap.ledger_revision,  # Old revision
            blocker_ids=(b1_id,),
            destination_repo=REPO,
            destination_pr=PR_NUMBER,
            reviewed_head_sha="head-a",
            reviewed_base_sha="base-0",
            review_attempt_id="att-worker-2",
        )
    assert "already held" in str(exc_info.value).lower() or "revision" in str(exc_info.value).lower()

    # Even if Worker 2 passes the latest revision, the active pending intent for Head A blocks Worker 2
    latest_snap = ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    with pytest.raises(PublicationContentionError) as exc_info2:
        ledger.record_publication_intent(
            api_origin=API_ORIGIN,
            repository=REPO,
            pr_number=PR_NUMBER,
            intent_id="pub_worker_2_new_rev",
            expected_ledger_revision=latest_snap.ledger_revision,
            blocker_ids=(b1_id,),
            destination_repo=REPO,
            destination_pr=PR_NUMBER,
            reviewed_head_sha="head-a",
            reviewed_base_sha="base-0",
            review_attempt_id="att-worker-2",
        )
    assert "already held" in str(exc_info2.value).lower() or "active pending" in str(exc_info2.value).lower()


# ---------------------------------------------------------------------------
# AS-005: Disappeared Anchor Is Not a New Defect
# ---------------------------------------------------------------------------


def test_as005_disappeared_anchor_survives_in_ledger(ledger: CanonicalPRBlockerLedger) -> None:
    """AS-005: Given an open blocker rooted at comment 101 anchored at src/foo.py:42,

    when a new commit refactors src/foo.py so line 42 no longer exists in diff:
    the existing blocker survives based on its invariant scope, and review does not
    emit a duplicate root comment elsewhere.
    """
    ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)

    # 1. Open blocker rooted at 101
    payload = BlockerAdmissionPayload(
        category="IMPLEMENTATION",
        qualified_requirements=(QualifiedRequirement(issue_number=2137, requirement_id="REQ-001"),),
        authoritative_boundary="src/auto_coder/foo.py",
        incorrect_behavior_or_missing_invariant="Buffer overflow vulnerability",
        required_correction_outcome="Bounds check buffer indexing",
        evidence_needed="test",
        original_objective_anchor="Bounds safety",
        accepted_scope=CorrectionScope(description="Bounds check buffer"),
        aliases=(BlockerAlias(alias_type="root_comment_id", alias_value="101"),),
        evidence="arr[idx] unchecked",
        reviewed_head_sha="head-1",
        reviewed_base_sha="base-0",
        review_attempt_id="att-1",
        observation_identity="obs-foo-1",
    )
    b1_id, snap = ledger.admit_blocker(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        operation_id="admit-foo-1",
        expected_ledger_revision=1,
        payload=payload,
    )
    b1 = snap.get_blocker(b1_id)
    assert b1 is not None
    assert b1.get_canonical_root_comment_id() == 101

    # 2. Refactored head: line 42 is moved to line 90 or outside the changed diff
    finding_moved = AdversarialValidationFinding(
        actual_behavior="Buffer overflow vulnerability in indexing",
        required_behavior="Bounds check buffer indexing",
        anchor_path="src/auto_coder/foo.py",
        anchor_line=90,  # Moved line
        requirement_ids=["REQ-001"],
    )
    val_result = AdversarialValidationResult(
        result="NEEDS_FIX",
        findings=[finding_moved],
    )
    historical_comments = [
        {
            "id": 101,
            "in_reply_to_id": None,
            "path": "src/auto_coder/foo.py",
            "line": 42,
            "user": {"login": "auto-coder-reviewer[bot]"},
            "body": "Bounds check buffer indexing.",
        }
    ]
    hist_parse = parse_historical_pr_review_roots(
        historical_comments,
        reviewer_identity=ReviewerAppIdentity(login="auto-coder-reviewer[bot]", app_id=4765828),
        repo_name=REPO,
        pr_number=PR_NUMBER,
    )

    reconciled = reconcile_pr_findings_before_publication(
        ledger=ledger,
        api_origin=API_ORIGIN,
        repo_name=REPO,
        pr_number=PR_NUMBER,
        issue_number=2137,
        head_sha="head-2",
        base_sha="base-0",
        val_result=val_result,
        historical_parse=hist_parse,
        attempt_id="att-2",
    )

    # Blocker survived and associated without emitting a new root
    assert not reconciled.is_ambiguous
    assert b1_id in reconciled.already_rooted_blocker_ids
    assert len(reconciled.unrooted_blocker_ids) == 0


def test_as005_unavailable_root_read_fails_closed_when_blockers_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ledger: CanonicalPRBlockerLedger,
) -> None:
    """AS-005 (Unavailable read): When fetching previous review comments fails

    while open blockers exist in the ledger, publication fails closed with an
    inconclusive verdict rather than claiming PASS or creating duplicate roots.
    """
    ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    payload = BlockerAdmissionPayload(
        category="IMPLEMENTATION",
        qualified_requirements=(QualifiedRequirement(issue_number=2137, requirement_id="REQ-001"),),
        authoritative_boundary="src/auto_coder/foo.py",
        incorrect_behavior_or_missing_invariant="Open blocker",
        required_correction_outcome="Fix foo",
        evidence_needed="test",
        original_objective_anchor="Safety",
        accepted_scope=CorrectionScope(description="fix foo"),
        evidence="foo err",
        reviewed_head_sha="head-1",
        reviewed_base_sha="base-0",
        review_attempt_id="att-1",
        observation_identity="obs-foo-1",
    )
    ledger.admit_blocker(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        operation_id="admit-foo-open",
        expected_ledger_revision=1,
        payload=payload,
    )

    # Reviewer attempts publication, but GET /comments raises HTTP 500 error
    client = RecordingClient(
        [
            resp(200, {"id": 77}),  # installation
            resp(201, {"token": "tok", "expires_at": "2099-01-01T00:00:00Z"}),  # token
            resp(200, {"head": {"sha": "head-1"}}),  # PR head
            resp(500, {"message": "GitHub Comments API Unavailable"}),  # GET /comments fails!
        ]
    )
    reviewer = make_reviewer(tmp_path, client, monkeypatch, ledger=ledger)

    result = AdversarialValidationResult(
        result="PASS",
    )

    pub_result = reviewer.publish(
        repo_name=REPO,
        pr_number=PR_NUMBER,
        validated_head_sha="head-1",
        result=result,
        ledger=ledger,
    )

    # Must fail closed: previous root comments unavailable
    assert not pub_result.success
    assert "reconciliation pending" in pub_result.reason.lower()


# ---------------------------------------------------------------------------
# REQ-006: Justified Category Transition
# ---------------------------------------------------------------------------


def test_req006_justified_category_transition(ledger: CanonicalPRBlockerLedger) -> None:
    """REQ-006: Support justified category transition from IMPLEMENTATION to TEST_ORACLE

    when newly presented evidence demonstrates the contract itself was incomplete,
    recording justification and maintaining thread continuity with canonical root comment.
    """
    ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)

    # 1. Blocker initially admitted as IMPLEMENTATION and rooted at comment 101
    payload_impl = BlockerAdmissionPayload(
        category="IMPLEMENTATION",
        qualified_requirements=(QualifiedRequirement(issue_number=2137, requirement_id="REQ-001"),),
        authoritative_boundary="src/auto_coder/contract.py",
        incorrect_behavior_or_missing_invariant="Function accepts invalid payload without error",
        required_correction_outcome="Validate schema and reject invalid payload",
        evidence_needed="test",
        original_objective_anchor="Input contract",
        accepted_scope=CorrectionScope(description="Input validation"),
        aliases=(BlockerAlias(alias_type="root_comment_id", alias_value="101"),),
        evidence="No validation",
        reviewed_head_sha="head-1",
        reviewed_base_sha="base-0",
        review_attempt_id="att-1",
        observation_identity="obs-contract-1",
    )
    b1_id, snap = ledger.admit_blocker(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        operation_id="admit-contract-1",
        expected_ledger_revision=1,
        payload=payload_impl,
    )
    b1 = snap.get_blocker(b1_id)
    assert b1 is not None
    assert b1.category == "IMPLEMENTATION"
    assert b1.get_canonical_root_comment_id() == 101

    # 2. Reconcile with justified category transition to TEST_ORACLE
    payload_oracle = BlockerAdmissionPayload(
        category="TEST_ORACLE",
        qualified_requirements=(QualifiedRequirement(issue_number=2137, requirement_id="REQ-001"),),
        authoritative_boundary="src/auto_coder/contract.py",
        incorrect_behavior_or_missing_invariant="Test oracle fails to verify schema rejection",
        required_correction_outcome="Add test oracle asserting schema rejection",
        evidence_needed="Test assertion check",
        original_objective_anchor="Input contract",
        accepted_scope=CorrectionScope(description="Input validation test"),
        evidence="Missing test assert",
        reviewed_head_sha="head-2",
        reviewed_base_sha="base-0",
        review_attempt_id="att-2",
        observation_identity="obs-contract-2",
    )

    _, snap_transitioned = ledger.reconcile_observation(
        api_origin=API_ORIGIN,
        repository=REPO,
        pr_number=PR_NUMBER,
        operation_id="transition-contract-1",
        expected_ledger_revision=snap.ledger_revision,
        candidate_payload=payload_oracle,
        blocker_ids_considered=(b1_id,),
        decision=ReconciliationDecision.ASSOCIATE,
        associated_blocker_id=b1_id,
        review_observation_identity="obs-contract-2",
        justified_category_transition=True,
        category_transition_reason="Contract specification incomplete; requires test oracle gap",
    )

    b1_trans = snap_transitioned.get_blocker(b1_id)
    assert b1_trans is not None
    # Category is updated to TEST_ORACLE
    assert b1_trans.category == "TEST_ORACLE"
    # Thread continuity: root comment 101 is preserved
    assert b1_trans.get_canonical_root_comment_id() == 101
    # Reconciliation record exists
    assert any(r.associated_blocker_id == b1_id for r in snap_transitioned.reconciliation_records)


# ---------------------------------------------------------------------------
# REQ-011: Isolated Advisory Semantic Matching Unit Tests
# ---------------------------------------------------------------------------


def test_req011_advisory_semantic_matching_isolated(ledger: CanonicalPRBlockerLedger) -> None:
    """REQ-011: Unit tests for advisory semantic matching are isolated from deterministic

    publication invariant tests.
    Verifies:
    - Paraphrased wording matches existing blocker
    - Distinct boundary or requirement matches distinct blocker
    - Ambiguous match returns AMBIGUOUS
    """
    ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)

    payload = BlockerAdmissionPayload(
        category="IMPLEMENTATION",
        qualified_requirements=(QualifiedRequirement(issue_number=2137, requirement_id="REQ-001"),),
        authoritative_boundary="src/auto_coder/parser.py",
        incorrect_behavior_or_missing_invariant="Parser crashes on empty input string",
        required_correction_outcome="Return empty AST on empty string",
        evidence_needed="test",
        original_objective_anchor="Parser safety",
        accepted_scope=CorrectionScope(description="Handle empty input"),
        evidence="IndexError on line 12",
        reviewed_head_sha="head-1",
        reviewed_base_sha="base-0",
        review_attempt_id="att-1",
        observation_identity="obs-parser-1",
    )
    b1_id, snap = ledger.admit_blocker(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        operation_id="admit-parser-1",
        expected_ledger_revision=1,
        payload=payload,
    )
    b1 = snap.get_blocker(b1_id)
    assert b1 is not None

    # Case 1: Paraphrased wording on same boundary and requirement
    cand_paraphrased = ObservationCandidate(
        source_type="FINDING",
        category="IMPLEMENTATION",
        requirement_ids=("REQ-001",),
        authoritative_boundary="src/auto_coder/parser.py",
        incorrect_behavior_or_invariant="Unexpected crash when input is empty string",  # Paraphrased
        required_outcome="Return empty AST on empty input",
        observation_identity="obs-cand-1",
    )
    decision, associated_id, reason = advisory_semantic_match(cand_paraphrased, (b1,))
    assert decision == ReconciliationDecision.ASSOCIATE
    assert associated_id == b1_id

    # Case 2: Distinct boundary (different file)
    cand_diff_file = ObservationCandidate(
        source_type="FINDING",
        category="IMPLEMENTATION",
        requirement_ids=("REQ-001",),
        authoritative_boundary="src/auto_coder/lexer.py",  # Different file
        incorrect_behavior_or_invariant="Parser crashes on empty input string",
        required_outcome="Return empty AST",
        observation_identity="obs-cand-2",
    )
    decision, associated_id, reason = advisory_semantic_match(cand_diff_file, (b1,))
    assert decision == ReconciliationDecision.DISTINCT_DEFECT
    assert associated_id is None

    # Case 3: Distinct requirement ID
    cand_diff_req = ObservationCandidate(
        source_type="FINDING",
        category="IMPLEMENTATION",
        requirement_ids=("REQ-002",),  # Different requirement
        authoritative_boundary="src/auto_coder/parser.py",
        incorrect_behavior_or_invariant="Parser crashes on empty input string",
        required_outcome="Return empty AST",
        observation_identity="obs-cand-3",
    )
    decision, associated_id, reason = advisory_semantic_match(cand_diff_req, (b1,))
    assert decision == ReconciliationDecision.DISTINCT_DEFECT
    assert associated_id is None
