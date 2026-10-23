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
from auto_coder.execution_trace import EventKind, Outcome, bind_scope, get_trace_collector
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
    UnrecordedOrdinaryObservationError,
    advisory_semantic_match,
    extract_observation_candidate_from_finding,
    extract_observation_candidate_from_gap,
    ordinary_observation_owners,
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


@pytest.mark.parametrize("reverse_order", [False, True])
def test_presentation_guide_and_audit_findings_keep_distinct_ids(ledger: CanonicalPRBlockerLedger, reverse_order: bool) -> None:
    """PR #5453: sharing REQ-009 and a diff anchor does not imply one defect."""
    findings = [
        AdversarialValidationFinding(
            requirement_ids=["REQ-009"],
            anchor_path="server/src/mcp/mcp-api.ts",
            actual_behavior="Guide unchanged: catalog still says the nine mutation tools, no presentation entry or example; only the tools/list description string mentions the new tool.",
            required_behavior="Update the MCP operator guide with saved-versus-rendered semantics, exact-column targeting, reset/default rules, separate presentation/query revisions, five-minute process-local replay window, reuse rejection, and a Japanese read/preview/apply/reread example.",
            finding_identity="operator-guide-missing-presentation",
        ),
        AdversarialValidationFinding(
            requirement_ids=["REQ-009"],
            anchor_path="server/src/mcp/mcp-api.ts",
            actual_behavior="Audit record is written with outcome success and correct applied/replayed/entity/operationId but priorRevision and newRevision undefined even though both presentation revisions are known from the result.",
            required_behavior="Success/preview/no-op/replay audit carries prior/resulting presentation revisions when known alongside correlation, fingerprint, tool/project/grid/operation IDs and dryRun/applied/replayed state.",
            finding_identity="presentation-audit-missing-revisions",
        ),
    ]
    if reverse_order:
        findings.reverse()
    result = AdversarialValidationResult(result="NEEDS_FIX", findings=findings)
    first = reconcile_pr_findings_before_publication(ledger, API_ORIGIN, REPO, PR_NUMBER, 5436, "head-a", "base", result, HistoricalRootParseResult())
    assert first.is_ambiguous is False
    assert len(first.unrooted_blocker_ids) == 2
    assert len(set(first.unrooted_finding_blockers)) == 2
    assert first.unrooted_findings == tuple(findings)
    assert first.snapshot is not None
    assert len(first.snapshot.blockers) == 2

    # A new attempt with moved diff lines still reuses each defect's own ID.
    for finding in findings:
        finding.anchor_line = 99
    second = reconcile_pr_findings_before_publication(ledger, API_ORIGIN, REPO, PR_NUMBER, 5436, "head-b", "base", result, HistoricalRootParseResult())
    assert second.is_ambiguous is False
    assert second.unrooted_finding_blockers == first.unrooted_finding_blockers
    assert second.snapshot is not None
    assert len(second.snapshot.blockers) == 2


@pytest.mark.parametrize(
    "behavior,outcome",
    [
        ("Audit omits known presentation revisions", "Update the operator guide with presentation revisions"),
        ("", "Record known presentation revisions in audit logs"),
        ("Audit omits known presentation revisions", ""),
    ],
)
def test_shared_boundary_requires_matching_behavior_and_correction(behavior: str, outcome: str) -> None:
    from auto_coder.canonical_pr_blocker_ledger import BlockerSnapshot

    blocker = BlockerSnapshot(
        category="IMPLEMENTATION",
        authoritative_boundary="server/src/mcp/mcp-api.ts",
        qualified_requirements=(QualifiedRequirement(issue_number=5436, requirement_id="REQ-009"),),
        incorrect_behavior_or_missing_invariant="Audit omits known presentation revisions",
        required_correction_outcome="Record known presentation revisions in audit logs",
    )
    candidate = ObservationCandidate(
        category="IMPLEMENTATION",
        authoritative_boundary=blocker.authoritative_boundary,
        requirement_ids=("REQ-009",),
        incorrect_behavior_or_invariant=behavior,
        required_outcome=outcome,
    )
    assert scopes_describe_same_blocker(candidate, blocker) is False
    assert scopes_describe_same_blocker(candidate, blocker, advisory_mode=True) is False


@pytest.mark.parametrize("candidate_boundary,blocker_boundary", [("", "src/api.py"), ("src/api.py", ""), ("", "")])
def test_missing_boundary_cannot_associate_identical_scope_text(candidate_boundary: str, blocker_boundary: str) -> None:
    from auto_coder.canonical_pr_blocker_ledger import BlockerSnapshot

    candidate = ObservationCandidate(
        category="IMPLEMENTATION",
        authoritative_boundary=candidate_boundary,
        incorrect_behavior_or_invariant="Audit omits known presentation revisions",
        required_outcome="Record known presentation revisions in audit logs",
    )
    blocker = BlockerSnapshot(
        category=candidate.category,
        authoritative_boundary=blocker_boundary,
        incorrect_behavior_or_missing_invariant=candidate.incorrect_behavior_or_invariant,
        required_correction_outcome=candidate.required_outcome,
    )
    assert scopes_describe_same_blocker(candidate, blocker) is False
    assert advisory_semantic_match(candidate, [blocker])[0] == ReconciliationDecision.DISTINCT_DEFECT


def test_common_remedy_does_not_merge_different_response_defects() -> None:
    from auto_coder.canonical_pr_blocker_ledger import BlockerSnapshot

    blocker = BlockerSnapshot(
        category="IMPLEMENTATION",
        authoritative_boundary="server/src/mcp/mcp-api.ts",
        incorrect_behavior_or_missing_invariant="Applied change returns missing presentation revision",
        required_correction_outcome="Correct the presentation response fields",
    )
    candidate = ObservationCandidate(
        category="IMPLEMENTATION",
        authoritative_boundary=blocker.authoritative_boundary,
        incorrect_behavior_or_invariant="Preview returns applied true without changing presentation",
        required_outcome=blocker.required_correction_outcome,
    )
    assert scopes_describe_same_blocker(candidate, blocker, advisory_mode=True) is False


@pytest.mark.parametrize("equivalent", [False, True])
@pytest.mark.parametrize("ambiguous_owners", [False, True])
def test_publication_confirms_one_root_per_blocker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger: CanonicalPRBlockerLedger, equivalent: bool, ambiguous_owners: bool) -> None:
    """Distinct defects get distinct roots; repeated observations share one root."""
    first = AdversarialValidationFinding(
        requirement_ids=["REQ-009"],
        anchor_path="server/src/mcp/mcp-api.ts",
        anchor_line=1,
        actual_behavior="Successful presentation audit omits prior and resulting revisions",
        required_behavior="Record known prior and resulting presentation revisions in audit logs",
        evidence="Success audit reads the wrong revision fields",
    )
    second = AdversarialValidationFinding(
        requirement_ids=["REQ-009"],
        anchor_path=first.anchor_path,
        anchor_line=2,
        actual_behavior=first.actual_behavior if equivalent else "Operator guide lists nine mutation tools and omits the presentation example",
        required_behavior=first.required_behavior if equivalent else "Document saved presentation semantics and the Japanese read preview apply reread example in the operator guide",
        evidence="Independent evidence for the second observation",
    )
    retained_ids: set[str] = set()
    if ambiguous_owners:
        snapshot = ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
        for index in range(2):
            blocker_id, snapshot = ledger.admit_blocker(
                API_ORIGIN,
                REPO,
                PR_NUMBER,
                operation_id=f"retained-owner-{index}",
                expected_ledger_revision=snapshot.ledger_revision,
                payload=BlockerAdmissionPayload(
                    category="IMPLEMENTATION",
                    qualified_requirements=(QualifiedRequirement(issue_number=0, requirement_id="REQ-009"),),
                    authoritative_boundary=first.anchor_path,
                    incorrect_behavior_or_missing_invariant=first.actual_behavior,
                    required_correction_outcome=first.required_behavior,
                    accepted_scope=CorrectionScope(description=f"Retained obligation {index}"),
                    aliases=(BlockerAlias(alias_type="github_root_comment", alias_value="4176063590"),),
                ),
            )
            retained_ids.add(blocker_id)

    class PublicationClient(RecordingClient):
        review_body = ""
        review_comments: list[dict[str, object]] = []

        def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
            if method == "POST" and url.endswith("/reviews"):
                self.requests.append(httpx.Request(method, url, **kwargs))
                payload = kwargs["json"]
                assert isinstance(payload, dict)
                self.review_body = payload["body"]
                self.review_comments = payload["comments"]
                return resp(200, {"id": 77})
            if url.endswith("/reviews/77"):
                return resp(200, {"id": 77, "body": self.review_body, "commit_id": "head", "state": "CHANGES_REQUESTED", "user": {"login": "auto-coder-reviewer[bot]"}})
            if "/reviews/77/comments?" in url:
                return resp(200, [dict(comment, id=701 + index, pull_request_review_id=77, user={"login": "auto-coder-reviewer[bot]"}) for index, comment in enumerate(self.review_comments)])
            return super().request(method, url, **kwargs)

    client = PublicationClient(
        [
            resp(200, {"id": 1}),
            resp(201, {"token": "fake-token", "expires_at": "2099-01-01T00:00:00Z"}),
            resp(200, {"head": {"sha": "head"}}),
            resp(200, []),
            resp(200, [{"filename": first.anchor_path, "patch": "@@ -1,2 +1,2 @@\n-old\n+new\n+other"}]),
        ]
    )
    reviewer = make_reviewer(tmp_path, client, monkeypatch, ledger=ledger)
    monkeypatch.setattr(reviewer, "_identity", ReviewerAppIdentity("auto-coder-reviewer[bot]", 4765828))
    publication = reviewer.publish(REPO, PR_NUMBER, "head", AdversarialValidationResult(result="NEEDS_FIX", findings=[first, second]), operation_id="test-publication")

    assert publication.success is True
    assert publication.reason == ""
    expected_roots = 1 if equivalent else 2
    assert len(client.review_comments) == expected_roots
    assert f"{expected_roots} actionable finding thread(s) are attached" in client.review_body
    snapshot = ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    assert len(snapshot.blockers) == expected_roots + len(retained_ids)
    assert {blocker.get_canonical_root_comment_id() for blocker in snapshot.blockers if blocker.blocker_id not in retained_ids} == set(range(701, 701 + expected_roots))
    assert all(snapshot.get_blocker(blocker_id).disposition == BlockerDisposition.OPEN for blocker_id in retained_ids)
    intent = ledger.get_publication_intent(API_ORIGIN, REPO, PR_NUMBER, "test-publication")
    assert intent is not None
    assert intent.status == "CONFIRMED"
    assert len(intent.confirmed_roots) == expected_roots
    assert ledger.get_pending_publication_intents(API_ORIGIN, REPO, PR_NUMBER) == ()
    assert len(client.responses) == 0
    submitted_bodies = "\n".join(str(comment["body"]) for comment in client.review_comments)
    assert first.evidence in submitted_bodies
    assert second.evidence in submitted_bodies
    if equivalent:
        assert client.review_comments[0]["line"] == 1


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


def test_authenticated_declared_root_reassociates_without_reparsing_scope(
    ledger: CanonicalPRBlockerLedger,
) -> None:
    """Issue #2308: a retained identity outranks a changed historical rendering."""
    ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    payload = BlockerAdmissionPayload(
        category="IMPLEMENTATION",
        qualified_requirements=(QualifiedRequirement(issue_number=2308, requirement_id="REQ-002"),),
        authoritative_boundary="src/auto_coder/original.py",
        incorrect_behavior_or_missing_invariant="the published root lacks its alias",
        required_correction_outcome="retain the original blocker identity",
        evidence_needed="canonical alias",
        accepted_scope=CorrectionScope(
            description="associate the published root",
            concern_ids=("concern-original",),
        ),
        observation_identity="original-observation",
    )
    blocker_id, before = ledger.admit_blocker(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        operation_id="admit-original",
        expected_ledger_revision=1,
        payload=payload,
    )

    parsed = parse_historical_pr_review_roots(
        [
            {
                "id": 2308001,
                "user": {"login": "auto-coder-reviewer[bot]"},
                "path": "src/auto_coder/moved.py",
                "body": ("### Adversarial finding\n" "Requirement: REQ-002\n" "**Reachable path**\n" "src/auto_coder/moved.py now uses different explanatory wording.\n\n" f"Blocker identity: `{blocker_id}`"),
            }
        ],
        reviewer_identity=ReviewerAppIdentity(login="auto-coder-reviewer[bot]", app_id=4765828),
        repo_name=REPO,
        pr_number=PR_NUMBER,
    )
    result = reconcile_pr_findings_before_publication(
        ledger,
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        2308,
        "new-head",
        "new-base",
        AdversarialValidationResult(result="PASS"),
        parsed,
    )

    assert not result.is_ambiguous
    after = ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER, require_retained_state=True)
    assert len(after.blockers) == 1
    blocker = after.get_blocker(blocker_id)
    assert blocker is not None
    assert blocker.accepted_scope == before.get_blocker(blocker_id).accepted_scope
    assert blocker.authoritative_boundary == "src/auto_coder/original.py"
    assert blocker.concern_ids == ("concern-original",)
    assert blocker.get_canonical_root_comment_id() == 2308001

    repeated = reconcile_pr_findings_before_publication(
        ledger,
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        2308,
        "later-head",
        "new-base",
        AdversarialValidationResult(result="PASS"),
        parsed,
    )
    assert not repeated.is_ambiguous
    final = ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER, require_retained_state=True)
    assert len(final.blockers) == 1
    aliases = [alias for alias in final.get_blocker(blocker_id).aliases if alias.alias_type == "github_root_comment" and alias.alias_value == "2308001"]
    assert len(aliases) == 1


def test_historical_identity_conflicts_fail_closed(
    ledger: CanonicalPRBlockerLedger,
) -> None:
    """Issue #2308: quoted, contradictory, and unknown identities cannot authorize."""
    ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    parsed = parse_historical_pr_review_roots(
        [
            {
                "id": 2308002,
                "user": {"login": "auto-coder-reviewer[bot]"},
                "body": ("### Adversarial finding\nRequirement: REQ-003\n" "> Blocker identity: `blk_quoted`\n" "```text\nBlocker identity: `blk_fenced`\n```\n" "Blocker identity: `blk_unknown_a`\n" "Blocker identity: `blk_unknown_b`"),
            }
        ],
        reviewer_identity=ReviewerAppIdentity(login="auto-coder-reviewer[bot]", app_id=4765828),
    )
    assert parsed.corrections[0].blocker_id is None
    assert parsed.corrections[0].blocker_identity_conflict == (
        "blk_unknown_a",
        "blk_unknown_b",
    )

    result = reconcile_pr_findings_before_publication(
        ledger,
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        2308,
        "head",
        "base",
        AdversarialValidationResult(result="PASS"),
        parsed,
    )
    assert result.is_ambiguous
    assert "Conflicting blocker declarations" in (result.ambiguity_reason or "")
    snapshot = ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER, require_retained_state=True)
    assert snapshot.blockers == ()

    unknown = parse_historical_pr_review_roots(
        [
            {
                "id": 2308003,
                "user": {"login": "auto-coder-reviewer[bot]"},
                "body": ("### Adversarial finding\nRequirement: REQ-003\n" "Blocker identity: `blk_not_retained`"),
            }
        ],
        reviewer_identity=ReviewerAppIdentity(login="auto-coder-reviewer[bot]", app_id=4765828),
    )
    unknown_result = reconcile_pr_findings_before_publication(
        ledger,
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        2308,
        "head",
        "base",
        AdversarialValidationResult(result="PASS"),
        unknown,
    )
    assert unknown_result.is_ambiguous
    assert "unknown blocker 'blk_not_retained'" in (unknown_result.ambiguity_reason or "")
    unchanged = ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER, require_retained_state=True)
    assert unchanged.blockers == ()


def test_compound_root_reassociates_each_independently_declared_owner(
    db_path: Path,
) -> None:
    """Issue #2308: one compound root may retain two independent owners."""
    ledger = CanonicalPRBlockerLedger(db_path=db_path)
    ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    blocker_ids: list[str] = []
    for index, boundary in enumerate(("src/owner_a.py", "src/owner_b.py"), 1):
        snapshot = ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER, require_retained_state=True)
        blocker_id, _ = ledger.admit_blocker(
            API_ORIGIN,
            REPO,
            PR_NUMBER,
            operation_id=f"admit-compound-owner-{index}",
            expected_ledger_revision=snapshot.ledger_revision,
            payload=BlockerAdmissionPayload(
                category="IMPLEMENTATION",
                qualified_requirements=(QualifiedRequirement(issue_number=2308, requirement_id=f"REQ-00{index}"),),
                authoritative_boundary=boundary,
                incorrect_behavior_or_missing_invariant=f"owner {index} behavior",
                required_correction_outcome=f"owner {index} outcome",
                evidence_needed=f"owner {index} evidence",
                accepted_scope=CorrectionScope(
                    description=f"owner {index} scope",
                    concern_ids=(f"owner-{index}-concern",),
                ),
                observation_identity=f"owner-{index}-observation",
            ),
        )
        blocker_ids.append(blocker_id)

    body = "### Adversarial finding A\n" "Requirement: REQ-001\nPath: src/owner_a.py\nOwner A behavior.\n\n" f"Blocker identity: `{blocker_ids[0]}`\n\n" "### Adversarial finding B\n" "Requirement: REQ-002\nPath: src/owner_b.py\nOwner B behavior.\n\n" f"Blocker identity: `{blocker_ids[1]}`"
    parsed = parse_historical_pr_review_roots(
        [{"id": 2308010, "user": {"login": "reviewer[bot]"}, "body": body}],
        reviewer_identity=ReviewerAppIdentity(login="reviewer[bot]", app_id=42),
    )
    assert [correction.blocker_id for correction in parsed.corrections] == blocker_ids

    for active_ledger in (ledger, ledger, CanonicalPRBlockerLedger(db_path=db_path)):
        result = reconcile_pr_findings_before_publication(
            active_ledger,
            API_ORIGIN,
            REPO,
            PR_NUMBER,
            2308,
            "head",
            "base",
            AdversarialValidationResult(result="PASS"),
            parsed,
        )
        assert not result.is_ambiguous

    final = ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER, require_retained_state=True)
    assert len(final.blockers) == 2
    for index, blocker_id in enumerate(blocker_ids, 1):
        blocker = final.get_blocker(blocker_id)
        assert blocker is not None
        assert blocker.accepted_scope.description == f"owner {index} scope"
        assert blocker.concern_ids == (f"owner-{index}-concern",)
        assert blocker.get_root_comment_ids() == (2308010,)


@pytest.mark.parametrize("example_id", ("retained", "unknown"))
def test_fenced_finding_heading_cannot_supply_blocker_identity(
    ledger: CanonicalPRBlockerLedger,
    example_id: str,
) -> None:
    """Issue #2308: section splitting retains fenced declaration context."""
    ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    retained_id, _ = ledger.admit_blocker(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        operation_id="admit-fenced-example-target",
        expected_ledger_revision=1,
        payload=BlockerAdmissionPayload(
            category="IMPLEMENTATION",
            authoritative_boundary="src/retained.py",
            incorrect_behavior_or_missing_invariant="retained behavior",
            required_correction_outcome="retained outcome",
            evidence_needed="retained evidence",
            accepted_scope=CorrectionScope(description="retained scope"),
            observation_identity="retained-observation",
        ),
    )
    fenced_id = retained_id if example_id == "retained" else "blk_unknown_example"
    body = "Copied example:\n```markdown\n### Adversarial finding\n" f"Blocker identity: `{fenced_id}`\n```\n\n" "### Adversarial finding\nRequirement: REQ-001\nPath: src/real.py\n" "Real behavior.\n\n" f"Blocker identity: `{retained_id}`"
    parsed = parse_historical_pr_review_roots(
        [{"id": 2308011, "user": {"login": "reviewer[bot]"}, "body": body}],
        reviewer_identity=ReviewerAppIdentity(login="reviewer[bot]", app_id=42),
    )

    assert len(parsed.corrections) == 1
    assert parsed.corrections[0].blocker_id == retained_id
    assert parsed.corrections[0].blocker_identity_conflict == ()
    result = reconcile_pr_findings_before_publication(
        ledger,
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        2308,
        "head",
        "base",
        AdversarialValidationResult(result="PASS"),
        parsed,
    )
    assert not result.is_ambiguous
    snapshot = ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER, require_retained_state=True)
    assert len(snapshot.blockers) == 1
    blocker = snapshot.get_blocker(retained_id)
    assert blocker is not None
    assert blocker.get_root_comment_ids() == (2308011,)


# ---------------------------------------------------------------------------
# AS-002: Already Duplicated Legacy PR
# ---------------------------------------------------------------------------


def test_two_tier_historical_roots_preserve_distinct_retained_owners(ledger: CanonicalPRBlockerLedger) -> None:
    """PR #2417: actual two-tier markup must not merge two corrected defects."""
    comments = json.loads((Path(__file__).parent / "fixtures/pr_finding_reconciliation/pr2417_roots.json").read_text())
    parsed = parse_historical_pr_review_roots(
        comments,
        reviewer_identity=ReviewerAppIdentity(login=comments[0]["user"]["login"], app_id=4765828),
    )
    expected = [
        (
            4176063590,
            "OrdinaryClosureEvidence.retain and durable reconstruction of ordinary_closure_evidence.json.",
            "Retention stores a lossy OrdinaryOutcome. Coverage evidence is dropped, findings and gaps become counts or generic blocker messages, and raw_response becomes only a digest. Thread dispositions, recovery evidence and diagnostic details are not retained.",
            "Persist the complete ordinary semantic result alongside its assessment before granting reusable closure authority.",
        ),
        (
            4176063592,
            "Semantic PASS validation before durable bounded closure certification.",
            "_ordinary_outcome treats any nonempty list of VERIFIED/IRRELEVANT entries as complete coverage. Application never compares those identities with the observed Requirements snapshot, so this subset satisfies _authority_gap and reaches certify_closure.",
            "The durable boundary must refuse certification because REQ-002 was never verified in that ordinary evaluation.",
        ),
    ]
    assert [(c.comment_id, c.authoritative_boundary, c.incorrect_behavior_or_invariant, c.required_outcome) for c in parsed.corrections] == expected
    snapshot = ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    owners = {}
    for root_id, boundary, actual, outcome in expected:
        blocker_id, snapshot = ledger.admit_blocker(
            API_ORIGIN,
            REPO,
            PR_NUMBER,
            operation_id=f"seed-{root_id}",
            expected_ledger_revision=snapshot.ledger_revision,
            payload=BlockerAdmissionPayload(
                category="IMPLEMENTATION",
                qualified_requirements=(QualifiedRequirement(issue_number=2406, requirement_id="REQ-002"),),
                authoritative_boundary=boundary,
                incorrect_behavior_or_missing_invariant=actual,
                required_correction_outcome=outcome,
                evidence_needed=outcome,
                accepted_scope=CorrectionScope(description=actual, concern_ids=(f"concern-{root_id}",)),
                aliases=(BlockerAlias(alias_type="github_root_comment", alias_value=str(root_id)),),
            ),
        )
        owners[root_id] = blocker_id
    for attempt in range(2):
        reconciled = reconcile_pr_findings_before_publication(
            ledger,
            API_ORIGIN,
            REPO,
            PR_NUMBER,
            issue_number=2406,
            head_sha="fixed-head",
            base_sha="base",
            val_result=AdversarialValidationResult(result="PASS"),
            historical_parse=parsed,
            attempt_id=f"replay-{attempt}",
        )
        assert reconciled.is_ambiguous is False
        assert reconciled.unrooted_blocker_ids == ()
        snapshot = ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER, require_retained_state=True)
        assert len(snapshot.blockers) == 2
        for root_id, owner in owners.items():
            assert [b.blocker_id for b in snapshot.get_blockers_for_alias("github_root_comment", str(root_id))] == [owner]


@pytest.mark.parametrize("heading", ["**{name}:** {value}", "**{name}**: {value}", "**{name}**\n{value}"])
def test_historical_scope_fields_support_inline_and_multiline_markdown(heading: str) -> None:
    fields = [("Authoritative boundary", "service.verify"), ("Actual behavior", "Accepts invalid tokens.\nDrops *signature* evidence."), ("Required behavior", "Reject invalid tokens.")]
    body = "<!-- shared publication marker -->\nRequirement: REQ-001\n" + "\n\n".join(heading.format(name=name, value=value) for name, value in fields)
    parsed = parse_historical_pr_review_roots([{"id": 1, "path": "service.py", "body": body}])
    assert len(parsed.corrections) == 1
    correction = parsed.corrections[0]
    assert correction.authoritative_boundary == "service.verify"
    assert correction.incorrect_behavior_or_invariant == "Accepts invalid tokens.\nDrops *signature* evidence."
    assert correction.required_outcome == "Reject invalid tokens."


def test_historical_fallback_ignores_publication_markers() -> None:
    parsed = parse_historical_pr_review_roots(
        [
            {
                "id": 1,
                "path": "service.py",
                "body": "<!-- shared publication marker -->\nRequirement: REQ-001\nPath: service.py\nMissing signature verification.",
            }
        ]
    )
    assert len(parsed.corrections) == 1
    assert parsed.corrections[0].authoritative_boundary == "service.py"
    assert parsed.corrections[0].incorrect_behavior_or_invariant == "Missing signature verification."
    assert parsed.corrections[0].required_outcome == "Missing signature verification."


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


def test_ambiguous_match_splits_without_discarding_existing_owners(ledger: CanonicalPRBlockerLedger, db_path: Path) -> None:
    """An ambiguous observation gets an independent, restart-stable owner."""
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

    assert reconciled.is_ambiguous is False
    assert len(reconciled.unrooted_blocker_ids) == 1
    split_id = reconciled.unrooted_blocker_ids[0]
    assert split_id not in (b1_id, b2_id)
    assert reconciled.blocker_for_finding == ((0, split_id),)
    assert reconciled.unrooted_findings == (ambig_finding,)
    assert reconciled.snapshot is not None
    assert len(reconciled.snapshot.blockers) == 3
    assert all(b.disposition == BlockerDisposition.OPEN for b in reconciled.snapshot.blockers)
    assert reconciled.snapshot.get_blocker(b1_id).incorrect_behavior_or_missing_invariant == payload1.incorrect_behavior_or_missing_invariant
    assert reconciled.snapshot.get_blocker(b2_id).incorrect_behavior_or_missing_invariant == payload2.incorrect_behavior_or_missing_invariant

    ambig_finding.anchor_line = 99
    repeated = reconcile_pr_findings_before_publication(
        CanonicalPRBlockerLedger(db_path=db_path),
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        2137,
        "head-3",
        "base-0",
        val_result,
        HistoricalRootParseResult(),
        attempt_id="att-3",
    )
    assert repeated.is_ambiguous is False
    assert repeated.blocker_for_finding == ((0, split_id),)
    assert repeated.snapshot is not None
    assert len(repeated.snapshot.blockers) == 3

    from auto_coder.ordinary_correction_references import build_correction_reference_context

    reference_context = build_correction_reference_context(repeated.snapshot, "head-4", "base-0")
    reference = next(item for item in reference_context.references if item.blocker_id == split_id)
    restatement = AdversarialValidationFinding(
        requirement_ids=list(reference.requirement_ids),
        anchor_path=reference.authoritative_boundary,
        actual_behavior="Configuration absence reaches an unchecked dereference.",
        required_behavior="Safely handle the absent object before it is read.",
        finding_identity="restated-split",
        correction_identity=reference.correction_identity,
        correction_scope_fingerprint=reference.scope_fingerprint,
        correction_context_fingerprint=reference_context.fingerprint,
    )
    restored = reconcile_pr_findings_before_publication(
        CanonicalPRBlockerLedger(db_path=db_path),
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        2137,
        "head-4",
        "base-0",
        AdversarialValidationResult(result="NEEDS_FIX", findings=[restatement], correction_reference_context=reference_context),
        HistoricalRootParseResult(),
        attempt_id="att-4",
    )
    assert not restored.is_ambiguous
    assert restored.blocker_for_finding == ((0, split_id),)
    assert len(restored.snapshot.blockers) == 3
    for retained_id in (b1_id, b2_id, split_id):
        assert restored.snapshot.get_blocker(retained_id).accepted_scope == repeated.snapshot.get_blocker(retained_id).accepted_scope
        assert restored.snapshot.get_blocker(retained_id).disposition == BlockerDisposition.OPEN


@pytest.mark.parametrize("matches_existing", [False, True])
def test_historical_multi_owner_root_splits_and_replays(db_path: Path, matches_existing: bool) -> None:
    """The PR #2417 ambiguity preserves each owner and continues after restart."""
    ledger = CanonicalPRBlockerLedger(db_path=db_path)
    snapshot = ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    original_ids: set[str] = set()
    for index in range(2):
        blocker_id, snapshot = ledger.admit_blocker(
            API_ORIGIN,
            REPO,
            PR_NUMBER,
            operation_id=f"historical-owner-{index}",
            expected_ledger_revision=snapshot.ledger_revision,
            payload=BlockerAdmissionPayload(
                category="IMPLEMENTATION",
                qualified_requirements=(QualifiedRequirement(issue_number=2137, requirement_id="REQ-001"),),
                authoritative_boundary="service.py",
                incorrect_behavior_or_missing_invariant="Null pointer when config is missing",
                required_correction_outcome="Handle missing config safely",
                accepted_scope=CorrectionScope(description=f"Independent retained concern {index}"),
                aliases=(BlockerAlias(alias_type="github_root_comment", alias_value="4176063590"),),
            ),
        )
        original_ids.add(blocker_id)
    before = snapshot
    behavior = "Null pointer when config is missing" if matches_existing else "Access token expiration is never validated"
    outcome = "Handle missing config safely" if matches_existing else "Reject expired access tokens before dispatch"
    parsed = parse_historical_pr_review_roots(
        [{"id": 4176063590, "path": "service.py", "user": {"login": "reviewer[bot]"}, "body": f"### Adversarial finding\nRequirement: REQ-001\n**Actual:** {behavior}\n**Expected:** {outcome}"}],
        reviewer_identity=ReviewerAppIdentity("reviewer[bot]", 42),
    )
    finding = AdversarialValidationFinding(requirement_ids=["REQ-001"], anchor_path="service.py", actual_behavior=behavior, required_behavior=outcome)
    result = AdversarialValidationResult(result="NEEDS_FIX", findings=[finding])
    collector = get_trace_collector()
    handle = collector.start_execution(REPO, "pr", PR_NUMBER, "test")
    with bind_scope(handle.scope):
        first = reconcile_pr_findings_before_publication(ledger, API_ORIGIN, REPO, PR_NUMBER, 2137, "head", "base", result, parsed)
    assert first.is_ambiguous is False
    assert first.snapshot is not None
    assert len(first.snapshot.blockers) == 3
    split_id = first.blocker_for_finding[0][1]
    assert split_id not in original_ids
    assert first.already_rooted_blocker_ids == (split_id,)
    assert first.unrooted_blocker_ids == ()
    for blocker_id in original_ids:
        assert first.snapshot.get_blocker(blocker_id) == before.get_blocker(blocker_id)
    assert all(b.disposition == BlockerDisposition.OPEN for b in first.snapshot.blockers)
    events = [event for event in collector.get_snapshot(repository=REPO, item_type="pr", item_number=PR_NUMBER).events if event.execution_id == handle.scope.execution_id and event.kind == EventKind.STAGE_RESULT.value]
    assert len(events) == 1
    assert events[0].stage_id == "pr.adversarial-validation"
    assert events[0].outcome == Outcome.COMPLETED.value
    assert events[0].facts["phase"] == "reconciliation-split"
    assert split_id in events[0].facts["reason"]
    assert "4176063590" in events[0].facts["reason"]

    repeated = reconcile_pr_findings_before_publication(CanonicalPRBlockerLedger(db_path=db_path), API_ORIGIN, REPO, PR_NUMBER, 2137, "later-head", "base", result, parsed)
    assert repeated.is_ambiguous is False
    assert repeated.already_rooted_blocker_ids == (split_id,)
    assert repeated.snapshot is not None
    assert len(repeated.snapshot.blockers) == 3
    assert repeated.unrooted_blocker_ids == ()


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


@pytest.mark.parametrize("gap_fields", [True, False])
def test_strong_regression_root_category_uses_declared_oracle_fields(gap_fields: bool) -> None:
    body = "### fallback-baseline-regression-gap\n\n**Requirements:** #2422/REQ-002\n\n**Affected boundary:** replacement clone\n\n**Actual:** Missing fallback coverage.\n\n**Expected:** Preserve designation across fallback.\n"
    if gap_fields:
        body += "\n**Incorrect implementation admitted by tests:** Exempt only the first attempt.\n\n**Why tests admit it:** No replacement attempt is exercised.\n"
    parsed = parse_historical_pr_review_roots([{"id": 4178383641, "body": body}])
    assert len(parsed.corrections) == 1
    assert parsed.corrections[0].category == ("TEST_ORACLE" if gap_fields else "IMPLEMENTATION")
    assert parsed.corrections[0].authoritative_boundary == "replacement clone"


def _incident_observations() -> dict[str, object]:
    return json.loads((Path(__file__).parent / "fixtures/review_reconciliation/outliner_5746_join_on.json").read_text())


def _incident_finding(observation: dict[str, object]) -> AdversarialValidationFinding:
    return AdversarialValidationFinding(
        requirement_id="#5729/REQ-004",
        finding_identity="join-on-child-binding",
        correction_identity="rewrite-child-before-join-alias",
        anchor_path="client/src/services/yjstable/tableSqlRewrite.ts",
        anchor_line=1,
        actual_behavior=str(observation["actual_behavior"]),
        required_behavior=str(observation["required_behavior"]),
        evidence=str(observation["evidence"]),
        counterexample="The renamed JOIN child cannot bind ON tasks.id.",
        reachability="Object Manager duplicateSelectedObjects -> rewriteTableQuerySql",
        violated_requirement="Preserve bindings to mapped physical relations.",
    )


def _reference_result(ledger: CanonicalPRBlockerLedger, finding: AdversarialValidationFinding, blocker_id: str, *, head: str = "head", base: str = "base") -> AdversarialValidationResult:
    from auto_coder.ordinary_correction_references import build_correction_reference_context

    snapshot = ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    context = build_correction_reference_context(snapshot, head, base)
    reference = next(item for item in context.references if item.blocker_id == blocker_id)
    finding.correction_identity = reference.correction_identity
    finding.correction_scope_fingerprint = reference.scope_fingerprint
    finding.correction_context_fingerprint = context.fingerprint
    return AdversarialValidationResult(result="NEEDS_FIX", findings=[finding], correction_reference_context=context)


def _admit_incident(ledger: CanonicalPRBlockerLedger) -> tuple[str, AdversarialValidationFinding]:
    first = _incident_finding(_incident_observations()["observations"][0])
    result = reconcile_pr_findings_before_publication(ledger, API_ORIGIN, REPO, PR_NUMBER, 5729, "head", "base", AdversarialValidationResult(result="NEEDS_FIX", findings=[first]), HistoricalRootParseResult(), attempt_id="first")
    assert not result.is_ambiguous
    return result.blocker_for_finding[0][1], first


def test_incident_nine_canonical_restatements_keep_one_published_root_after_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    """The actual nine descriptions route to one supplied immutable correction."""
    from auto_coder.ordinary_closure_evidence import deserialize_ordinary_result, serialize_ordinary_result

    class IncidentClient(RecordingClient):
        def __init__(self) -> None:
            super().__init__([])
            self.reviews: dict[int, dict[str, object]] = {}
            self.roots: list[dict[str, object]] = []

        def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
            self.requests.append(httpx.Request(method, url, **kwargs))
            if url.endswith("/installation"):
                return resp(200, {"id": 1})
            if url.endswith("/access_tokens"):
                return resp(201, {"token": "fake-token", "expires_at": "2099-01-01T00:00:00Z"})
            if url.endswith(f"/pulls/{PR_NUMBER}"):
                return resp(200, {"head": {"sha": "head"}})
            if f"/pulls/{PR_NUMBER}/comments?" in url:
                return resp(200, self.roots)
            if "/files?" in url:
                return resp(200, [{"filename": "client/src/services/yjstable/tableSqlRewrite.ts", "patch": "@@ -1 +1 @@\n-old\n+new"}])
            if method == "POST" and url.endswith("/reviews"):
                payload = kwargs["json"]
                assert isinstance(payload, dict)
                review_id = len(self.reviews) + 1
                self.reviews[review_id] = dict(payload, id=review_id, state="CHANGES_REQUESTED", user={"login": "auto-coder-reviewer[bot]"})
                self.roots.extend(dict(comment, id=700 + len(self.roots), pull_request_review_id=review_id, user={"login": "auto-coder-reviewer[bot]"}) for comment in payload.get("comments", []))
                return resp(200, {"id": review_id})
            if "/reviews/" in url:
                suffix = url.split("/reviews/", 1)[1]
                review_id = int(suffix.split("/", 1)[0])
                if "/comments?" in suffix:
                    return resp(200, [root for root in self.roots if root["pull_request_review_id"] == review_id])
                return resp(200, self.reviews[review_id])
            raise AssertionError(f"Unexpected request {method} {url}")

    fixture = _incident_observations()
    observations = fixture["observations"]
    assert len(observations) == 9
    assert len({item["actual_behavior"] for item in observations}) == 9
    client = IncidentClient()
    owner = ""
    original_scope = None
    original_outcome = ""
    for index, observation in enumerate(observations):
        ledger = CanonicalPRBlockerLedger(db_path=db_path)
        finding = _incident_finding(observation)
        result = _reference_result(ledger, finding, owner) if owner else AdversarialValidationResult(result="NEEDS_FIX", findings=[finding])
        result.attempt_id = f"incident-{index}"
        result = deserialize_ordinary_result(serialize_ordinary_result(result))
        reviewer = make_reviewer(tmp_path, client, monkeypatch, ledger)
        publication = reviewer.publish(REPO, PR_NUMBER, "head", result, operation_id=f"incident-publication-{index}")
        assert publication.success, publication.reason
        snapshot = ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
        assert len(snapshot.blockers) == 1
        blocker = snapshot.blockers[0]
        if index == 0:
            owner, original_scope, original_outcome = blocker.blocker_id, blocker.accepted_scope, blocker.required_correction_outcome
        assert blocker.blocker_id == owner
        assert blocker.accepted_scope == original_scope
        assert blocker.required_correction_outcome == original_outcome
        assert blocker.disposition == BlockerDisposition.OPEN
        assert blocker.get_root_comment_ids() == (700,)
        assert ordinary_observation_owners(snapshot, REPO, PR_NUMBER, "head", "base" if result.correction_reference_context else "", result) == (owner,)
        assert len(client.roots) == 1
    assert len(client.reviews) == 9
    assert len(snapshot.blockers[0].reconciliations) == 8
    assert str(observations[-1]["evidence"]) in snapshot.blockers[0].transitions[-1].evidence


@pytest.mark.parametrize("mismatch", ["unknown", "scope", "context", "requirements", "boundary", "pr", "head", "base"])
def test_explicit_correction_reference_mismatch_never_creates_owner(ledger: CanonicalPRBlockerLedger, mismatch: str) -> None:
    from dataclasses import replace

    owner, finding = _admit_incident(ledger)
    result = _reference_result(ledger, finding, owner)
    if mismatch == "unknown":
        finding.correction_identity = "canonical:blk_unknown"
    elif mismatch == "scope":
        finding.correction_scope_fingerprint = "changed-scope"
    elif mismatch == "context":
        finding.correction_context_fingerprint = "another-invocation"
    elif mismatch == "requirements":
        finding.requirement_id = "#9999/REQ-004"
    elif mismatch == "boundary":
        finding.anchor_path = "client/src/another-owner.ts"
    else:
        change = {"pr": {"pr_number": PR_NUMBER + 1}, "head": {"head_sha": "other-head"}, "base": {"base_sha": "other-base"}}[mismatch]
        result.correction_reference_context = replace(result.correction_reference_context, **change)
        finding.correction_context_fingerprint = result.correction_reference_context.fingerprint
    before = ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    with pytest.raises(AssociationAmbiguityError) as exc:
        ordinary_observation_owners(before, REPO, PR_NUMBER, "head", "base", result)
    assert not isinstance(exc.value, UnrecordedOrdinaryObservationError)
    reconciled = reconcile_pr_findings_before_publication(ledger, API_ORIGIN, REPO, PR_NUMBER, 5729, "head", "base", result, HistoricalRootParseResult(), attempt_id="mismatch")
    assert reconciled.is_ambiguous
    assert reconciled.ambiguity_reason
    assert ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER) == before


def test_referenced_test_gap_preserves_implementation_owner_and_independent_gap(ledger: CanonicalPRBlockerLedger) -> None:
    from auto_coder.bounded_repair_bundle import build_repair_handoff_bundle

    owner, finding = _admit_incident(ledger)
    reference_result = _reference_result(ledger, finding, owner)
    gap = TestOracleGap(
        gap_id="TOG-JOIN-BINDING",
        correction_identity=finding.correction_identity,
        correction_scope_fingerprint=finding.correction_scope_fingerprint,
        correction_context_fingerprint=finding.correction_context_fingerprint,
        requirement_id=finding.requirement_id,
        authoritative_boundary=finding.anchor_path,
        invariant="Regression omits a child qualifier in the ON expression.",
        focused_regression_scenario="Execute the joined-alias counterexample.",
    )
    independent = TestOracleGap(gap_id="TOG-INDEPENDENT-DELETE", requirement_id=finding.requirement_id, authoritative_boundary=finding.anchor_path, invariant="Delete throws away unrelated relation data", focused_regression_scenario="Preserve unrelated rows after rollback")
    reference_result.findings = []
    reference_result.test_oracle_gaps = [gap, independent]
    result = reconcile_pr_findings_before_publication(ledger, API_ORIGIN, REPO, PR_NUMBER, 5729, "head", "base", reference_result, HistoricalRootParseResult(), attempt_id="gap-reference")
    assert not result.is_ambiguous
    assert dict(result.blocker_for_gap)[gap.gap_id] == owner
    assert dict(result.blocker_for_gap)[independent.gap_id] != owner
    assert len(result.snapshot.blockers) == 2
    assert result.snapshot.get_blocker(owner).category == "IMPLEMENTATION"
    assert result.snapshot.get_blocker(owner).required_correction_outcome == finding.required_behavior
    bundle = build_repair_handoff_bundle(result.snapshot, REPO, PR_NUMBER, "feature", "main", "head", "contract", api_origin=API_ORIGIN, target_blocker_ids=[dict(result.blocker_for_gap)[gap.gap_id]])
    assert len(bundle.blockers) == 1
    assert bundle.blockers[0].blocker_id == owner
    assert bundle.blockers[0].category == "IMPLEMENTATION"
    assert bundle.blockers[0].required_corrective_outcome == finding.required_behavior


def test_exact_observation_replay_keeps_revision_across_unrelated_mutation_and_reload(ledger: CanonicalPRBlockerLedger, db_path: Path) -> None:
    owner, finding = _admit_incident(ledger)
    result = AdversarialValidationResult(result="NEEDS_FIX", findings=[finding])
    before = ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    _, unrelated = ledger.admit_blocker(
        API_ORIGIN, REPO, PR_NUMBER, operation_id="unrelated", expected_ledger_revision=before.ledger_revision, payload=BlockerAdmissionPayload(category="IMPLEMENTATION", authoritative_boundary="different.py", accepted_scope=CorrectionScope(description="Unrelated correction"))
    )
    replay = reconcile_pr_findings_before_publication(CanonicalPRBlockerLedger(db_path=db_path), API_ORIGIN, REPO, PR_NUMBER, 5729, "head", "base", result, HistoricalRootParseResult(), attempt_id="first")
    assert not replay.is_ambiguous
    assert replay.blocker_for_finding == ((0, owner),)
    assert replay.snapshot.ledger_revision == unrelated.ledger_revision
    assert replay.snapshot.get_blocker(owner) == before.get_blocker(owner)


def test_ordinary_owner_lookup_restores_finding_and_gap_without_roots_or_mutation(ledger: CanonicalPRBlockerLedger, db_path: Path) -> None:
    from auto_coder.ordinary_closure_evidence import deserialize_ordinary_result, serialize_ordinary_result

    result = AdversarialValidationResult(
        result="NEEDS_FIX",
        attempt_id="ordinary-owner-read",
        findings=[_incident_finding(_incident_observations()["observations"][0])],
        test_oracle_gaps=[TestOracleGap(gap_id="TOG-INDEPENDENT", requirement_id="#5729/REQ-004", authoritative_boundary="deletion.py", invariant="Failed delete must retain rows", focused_regression_scenario="Rollback leaves relation data intact")],
    )
    reconciled = reconcile_pr_findings_before_publication(ledger, API_ORIGIN, REPO, PR_NUMBER, 5729, "head", "base", result, HistoricalRootParseResult(), attempt_id=result.attempt_id)
    assert not reconciled.is_ambiguous
    expected = (reconciled.blocker_for_finding[0][1], reconciled.blocker_for_gap[0][1])
    assert expected[0] != expected[1]
    restored = deserialize_ordinary_result(serialize_ordinary_result(result))
    reloaded = CanonicalPRBlockerLedger(db_path=db_path)
    snapshot = reloaded.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    assert all(not blocker.get_root_comment_ids() for blocker in snapshot.blockers)
    assert ordinary_observation_owners(snapshot, REPO, PR_NUMBER, "head", "base", restored) == expected
    restored.findings.append(restored.findings[0])
    assert ordinary_observation_owners(snapshot, REPO, PR_NUMBER, "head", "base", restored) == expected
    assert reloaded.get_snapshot(API_ORIGIN, REPO, PR_NUMBER) == snapshot


@pytest.mark.parametrize("mismatch", ["repository", "pr", "head", "base", "attempt", "evidence", "closed", "ambiguous"])
def test_ordinary_owner_lookup_rejects_unbound_or_ambiguous_scope(ledger: CanonicalPRBlockerLedger, mismatch: str) -> None:
    from dataclasses import replace

    owner, finding = _admit_incident(ledger)
    result = AdversarialValidationResult(result="NEEDS_FIX", attempt_id="first", findings=[finding])
    snapshot = ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    blocker = snapshot.get_blocker(owner)
    assert blocker is not None
    repo, pr, head, base = REPO, PR_NUMBER, "head", "base"
    if mismatch == "repository":
        repo = "someone/another"
    elif mismatch == "pr":
        pr += 1
    elif mismatch == "head":
        head = "another-head"
    elif mismatch == "base":
        base = "another-base"
    elif mismatch == "attempt":
        result.attempt_id = "another-attempt"
    elif mismatch == "evidence":
        finding.evidence += " A changed counterexample."
    elif mismatch == "closed":
        snapshot = replace(snapshot, blockers=(replace(blocker, disposition=BlockerDisposition.VERIFIED_CORRECTION),))
    else:
        duplicate = replace(blocker, blocker_id="blk_conflicting")
        snapshot = replace(snapshot, blockers=(blocker, duplicate), aliases=snapshot.aliases + tuple(replace(alias, blocker_id=duplicate.blocker_id) for alias in snapshot.aliases))
    with pytest.raises(AssociationAmbiguityError) as exc:
        ordinary_observation_owners(snapshot, repo, pr, head, base, result)
    assert isinstance(exc.value, UnrecordedOrdinaryObservationError) == (mismatch in {"head", "base", "attempt", "evidence"})


def test_missing_ordinary_alias_does_not_mask_later_invalid_reference(ledger: CanonicalPRBlockerLedger) -> None:
    owner, finding = _admit_incident(ledger)
    result = _reference_result(ledger, finding, owner)
    result.findings.insert(0, AdversarialValidationFinding(finding_identity="not-yet-admitted", anchor_path="new.py", actual_behavior="Another violation", required_behavior="A different correction"))
    finding.correction_identity = "canonical:blk_unknown"
    snapshot = ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    with pytest.raises(AssociationAmbiguityError) as exc:
        ordinary_observation_owners(snapshot, REPO, PR_NUMBER, "head", "base", result)
    assert not isinstance(exc.value, UnrecordedOrdinaryObservationError)
    assert ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER) == snapshot


@pytest.mark.parametrize("binding_field", ["correction_identity", "correction_scope_fingerprint", "correction_context_fingerprint"])
@pytest.mark.parametrize("reverse_order", [False, True])
def test_recorded_gap_compaction_rejects_every_conflicting_canonical_binding(binding_field: str, reverse_order: bool) -> None:
    from auto_coder.adversarial_validator import parse_adversarial_validation_response

    recorded = TestOracleGap(gap_id="TOG-retained", requirement_id="#5729/REQ-004", authoritative_boundary="rewrite.py", invariant="The ON child must remain bound")
    valid = {
        "gap_id": recorded.gap_id,
        "status": "OPEN",
        "correction_identity": "canonical:blk_retained",
        "correction_scope_fingerprint": "supplied-scope",
        "correction_context_fingerprint": "supplied-context",
    }
    same = parse_adversarial_validation_response(json.dumps({"result": "NEEDS_TESTS", "test_oracle_gaps": [valid, valid]}), recorded_test_oracle_gaps=[recorded])
    assert same.result == "NEEDS_TESTS"
    assert len(same.test_oracle_gaps) == 1
    conflicting = dict(valid, **{binding_field: "canonical:blk_unknown" if binding_field == "correction_identity" else "contradictory-binding"})
    entries = [conflicting, valid] if reverse_order else [valid, conflicting]
    parsed = parse_adversarial_validation_response(json.dumps({"result": "NEEDS_TESTS", "test_oracle_gaps": entries}), recorded_test_oracle_gaps=[recorded])
    assert parsed.result == "ERROR"
    assert parsed.diagnostic_category == "correction_reference_invalid"
    assert not parsed.test_oracle_gaps


@pytest.mark.parametrize("invalid_reference", [False, True])
def test_production_ordinary_prompt_supplies_references_and_validates_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_reference: bool) -> None:
    from dataclasses import asdict

    from auto_coder.accepted_finding_bridge import AcceptedFindingBridge
    from auto_coder.adversarial_validator import AdversarialValidationContext, IssueRequirement, run_adversarial_validation
    from auto_coder.automation_config import AutomationConfig
    from auto_coder.pr_review_cycle import PrReviewCycleRepository
    from auto_coder.reviewer_session_registry import ReviewerSessionRegistry

    # The real validator uses GitHub's default API origin for its bridge.
    origin = "https://api.github.com"
    ledger = CanonicalPRBlockerLedger(db_path=tmp_path / "prompt-ledger.db")
    finding = _incident_finding(_incident_observations()["observations"][0])
    admitted = reconcile_pr_findings_before_publication(ledger, origin, REPO, PR_NUMBER, 5729, "head", "base", AdversarialValidationResult(result="NEEDS_FIX", findings=[finding]), HistoricalRootParseResult(), attempt_id="first")
    owner = admitted.blocker_for_finding[0][1]
    context = AdversarialValidationContext(
        repo_name=REPO,
        pr_number=PR_NUMBER,
        pr_diff="diff --git a/client/src/services/yjstable/tableSqlRewrite.ts b/client/src/services/yjstable/tableSqlRewrite.ts\n+binding = child",
        all_changed_files=[finding.anchor_path],
        issue_context="Issue #5729 requires preserving relation bindings.",
        issue_requirements=[IssueRequirement("#5729/REQ-004", "Preserve bindings to mapped physical relations.")],
    )
    monkeypatch.setattr("auto_coder.adversarial_validator.build_adversarial_validation_context", lambda *args, **kwargs: context)
    prompts: list[str] = []

    def model_response(prompt: str, **kwargs: object) -> str:
        prompts.append(prompt)
        encoded = prompt.split("CONTROLLER CORRECTION REFERENCES:\n", 1)[1].split("\nThese references", 1)[0]
        supplied = json.loads(encoded)
        assert supplied["repository"] == REPO
        assert supplied["pr_number"] == PR_NUMBER
        assert supplied["head_sha"] == "head"
        reference = supplied["references"][0]
        assert reference["correction_identity"] == f"canonical:{owner}"
        assert reference["required_outcome"] == finding.required_behavior
        restatement = asdict(finding)
        restatement.update(
            correction_identity=reference["correction_identity"],
            correction_scope_fingerprint="contradictory-scope" if invalid_reference else reference["scope_fingerprint"],
            correction_context_fingerprint=supplied["context_fingerprint"],
            actual_behavior="The JOIN child binding disappears before its ON qualifier is visited.",
        )
        independent = asdict(finding)
        independent.update(finding_identity="independent-delete", correction_identity="retain-delete-rows", actual_behavior="Delete loses independent destination rows", required_behavior="Retain unrelated destination rows during rollback")
        return json.dumps({"result": "NEEDS_FIX", "summary": "Two independent current corrections.", "findings": [restatement, independent], "requirement_coverage": [{"requirement_id": finding.requirement_id, "status": "VIOLATED", "evidence": "Current code exhibits both counterexamples."}]})

    monkeypatch.setattr("auto_coder.adversarial_validator.run_llm_prompt", model_response)
    manager = MagicMock()
    manager.get_current_backend_identity.return_value = ("ordinary", "codex", "model")
    result = run_adversarial_validation(
        REPO,
        {"number": PR_NUMBER, "head": {"sha": "head"}, "base": {"sha": "base"}},
        AutomationConfig(),
        backend_manager=manager,
        session_registry=ReviewerSessionRegistry(tmp_path / "reviewer-sessions.json"),
        accepted_finding_bridge=AcceptedFindingBridge(PrReviewCycleRepository(REPO, tmp_path / "cycle.json"), ledger),
    )
    assert len(prompts) == 1
    assert result.correction_reference_context == context.correction_reference_context
    assert len(result.findings) == 2
    if invalid_reference:
        assert result.result == "BLOCKED"
        assert result.diagnostic_category == "correction_reference_invalid"
        assert len(ledger.get_snapshot(origin, REPO, PR_NUMBER).blockers) == 1
    else:
        assert result.result == "NEEDS_FIX"
        reconciled = reconcile_pr_findings_before_publication(ledger, origin, REPO, PR_NUMBER, 5729, "head", "base", result, HistoricalRootParseResult(), attempt_id="ordinary")
        assert not reconciled.is_ambiguous
        assert reconciled.blocker_for_finding[0] == (0, owner)
        assert reconciled.blocker_for_finding[1][1] != owner
        assert len(reconciled.snapshot.blockers) == 2


@pytest.mark.parametrize("asserts_reference", [False, True])
def test_first_ordinary_review_retains_input_binding_without_supplying_canonical_owners(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, asserts_reference: bool) -> None:
    from dataclasses import asdict

    from auto_coder.accepted_finding_bridge import AcceptedFindingBridge
    from auto_coder.adversarial_validator import run_adversarial_validation
    from auto_coder.automation_config import AutomationConfig
    from auto_coder.canonical_pr_blocker_ledger import BlockerLedgerUnavailableError
    from auto_coder.ordinary_closure_evidence import deserialize_ordinary_result, serialize_ordinary_result
    from auto_coder.pr_review_cycle import PrReviewCycleRepository
    from auto_coder.reviewer_session_registry import ReviewerSessionRegistry

    finding = _incident_finding(_incident_observations()["observations"][0])

    class InputClient:
        def get_pr_diff(self, repo_name: str, pr_number: int) -> str:
            path = finding.anchor_path
            return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+binding = child\n"

        def get_pr_changed_file_count(self, repo_name: str, pr_number: int) -> int:
            return 1

        def get_pr_changed_files(self, repo_name: str, pr_number: int) -> list[dict[str, object]]:
            raise AssertionError("The complete diff already supplies the changed file")

        def get_issue_strict(self, repo_name: str, issue_number: int) -> dict[str, object]:
            assert issue_number == 5729
            return {"title": "Preserve relation bindings", "body": "## Requirements\n- REQ-004: Preserve bindings to mapped physical relations.\n"}

        def get_parent_issue_details(self, repo_name: str, issue_number: int) -> None:
            return None

    supplied_contexts: list[dict[str, object]] = []

    def model_response(prompt: str, **kwargs: object) -> str:
        encoded = prompt.split("CONTROLLER CORRECTION REFERENCES:\n", 1)[1].split("\nThese references", 1)[0]
        supplied = json.loads(encoded)
        supplied_contexts.append(supplied)
        assert supplied["references"] == []
        assert supplied["validation_snapshot"] and supplied["requirement_manifest_identity"]
        observed = asdict(finding)
        if asserts_reference:
            observed.update(correction_identity="canonical:blk_not_supplied", correction_scope_fingerprint="invented-scope", correction_context_fingerprint=supplied["context_fingerprint"])
        return json.dumps({"result": "NEEDS_FIX", "summary": "The child binding is lost.", "findings": [observed], "requirement_coverage": [{"requirement_id": finding.requirement_id, "status": "VIOLATED", "evidence": finding.evidence}]})

    ledger = CanonicalPRBlockerLedger(db_path=tmp_path / "empty-ledger.db")
    monkeypatch.setattr("auto_coder.adversarial_validator.run_llm_prompt", model_response)
    manager = MagicMock()
    manager.get_current_backend_identity.return_value = ("ordinary", "codex", "model")
    result = run_adversarial_validation(
        REPO,
        {"number": PR_NUMBER, "body": "Fixes #5729", "head": {"sha": "head"}, "base": {"sha": "base"}},
        AutomationConfig(),
        github_client=InputClient(),
        backend_manager=manager,
        session_registry=ReviewerSessionRegistry(tmp_path / "reviewer-sessions.json"),
        accepted_finding_bridge=AcceptedFindingBridge(PrReviewCycleRepository(REPO, tmp_path / "cycle.json"), ledger),
    )
    assert len(supplied_contexts) == 1
    retained = result.correction_reference_context
    assert retained is not None and retained.references == ()
    assert (retained.api_origin, retained.repository, retained.pr_number, retained.head_sha, retained.base_sha) == ("https://api.github.com", REPO, PR_NUMBER, "head", "base")
    assert retained.validation_snapshot == supplied_contexts[0]["validation_snapshot"]
    assert retained.requirement_manifest_identity == supplied_contexts[0]["requirement_manifest_identity"]
    assert deserialize_ordinary_result(serialize_ordinary_result(result)).correction_reference_context == retained
    assert result.result == ("BLOCKED" if asserts_reference else "NEEDS_FIX")
    if asserts_reference:
        assert result.diagnostic_category == "correction_reference_invalid"
    else:
        assert [(entry.requirement_id, entry.status) for entry in result.requirement_coverage] == [(finding.requirement_id, "VIOLATED")]
        assert [item.finding_identity for item in result.findings] == [finding.finding_identity]
    with pytest.raises(BlockerLedgerUnavailableError):
        ledger.get_snapshot("https://api.github.com", REPO, PR_NUMBER)


def test_normalized_test_restatement_preserves_reference_beside_independent_same_requirement_gap(ledger: CanonicalPRBlockerLedger) -> None:
    from auto_coder.adversarial_validator import AdversarialValidationContext, IssueRequirement, RequirementCoverageEntry, _apply_coverage_and_verdict_precedence

    owner, finding = _admit_incident(ledger)
    result = _reference_result(ledger, finding, owner)
    finding.actual_behavior = "No test resolves the child qualifier inside JOIN ON."
    finding.required_behavior = "Add regression test executing original and rewritten JOIN queries."
    finding.test_gap = "No test asserts the renamed child binding."
    finding.suggested_regression_scenario = "Execute the joined-alias SQL and compare rows."
    independent = TestOracleGap(
        gap_id="TOG-INDEPENDENT-DELETE", requirement_id=finding.requirement_id, authoritative_boundary=finding.anchor_path, anchor_path=finding.anchor_path, invariant="Delete loses unrelated rows during rollback", focused_regression_scenario="Verify unrelated rows remain after a failed transaction"
    )
    result.test_oracle_gaps = [independent]
    result.requirement_coverage = [RequirementCoverageEntry(finding.requirement_id, "VIOLATED", "Missing regression protection.")]
    context = AdversarialValidationContext(all_changed_files=[finding.anchor_path], issue_requirements=[IssueRequirement(finding.requirement_id, "Preserve bindings to mapped physical relations.")], correction_reference_context=result.correction_reference_context)
    normalized = _apply_coverage_and_verdict_precedence(result, context)
    assert normalized.result == "NEEDS_TESTS"
    assert not normalized.findings
    assert len(normalized.open_test_oracle_gaps) == 2
    referenced_gap = next(gap for gap in normalized.open_test_oracle_gaps if gap.correction_identity)
    assert referenced_gap.correction_identity == f"canonical:{owner}"
    assert referenced_gap.authoritative_boundary == finding.anchor_path
    reconciled = reconcile_pr_findings_before_publication(ledger, API_ORIGIN, REPO, PR_NUMBER, 5729, "head", "base", normalized, HistoricalRootParseResult(), attempt_id="normalized-gap")
    assert not reconciled.is_ambiguous
    assert dict(reconciled.blocker_for_gap)[referenced_gap.gap_id] == owner
    assert dict(reconciled.blocker_for_gap)[independent.gap_id] != owner
    assert reconciled.snapshot.get_blocker(owner).category == "IMPLEMENTATION"
