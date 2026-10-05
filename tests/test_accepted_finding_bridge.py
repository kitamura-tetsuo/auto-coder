"""Accepted Strong findings must survive ordinary rereview independently of reviewer sessions.

Every scenario starts at the production Strong result-acceptance path
(``_execute_pending_strong_audit`` with the real ``parse_review_result``), then
observes publication roots through the real ``GitHubAppReviewer`` adapter with
controlled GitHub transport responses, and finally runs the real ordinary
``run_adversarial_validation`` with a controlled model response.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from unittest.mock import MagicMock, patch

import httpx
import pytest

from auto_coder.accepted_finding_bridge import (
    ASSOCIATED,
    BINDING_RECONCILIATION_REQUIRED,
    CURRENCY_CURRENT_HEAD,
    CURRENCY_HISTORICAL,
    NOT_PUBLISHED,
    OUTCOME_ACCEPTED_CLOSURE_RETAINED,
    OUTCOME_CLOSURE_PROPOSAL_NOT_ACCEPTED,
    OUTCOME_STILL_VALID_OBSERVED,
    OUTCOME_UNRECOGNIZED,
    OUTCOME_UNRESOLVED_RETAINED,
    UNAVAILABLE,
    UNKNOWN,
    AcceptedFindingBridge,
    AcceptedFindingProjection,
    ObservedRoot,
    OrdinaryDisposition,
    ProjectionTarget,
    RootObservation,
)
from auto_coder.adversarial_validator import AdversarialValidationContext, IssueRequirement, _reconcile_test_oracle_gap_lifecycle, parse_adversarial_validation_response, run_adversarial_validation
from auto_coder.automation_config import AutomationConfig
from auto_coder.canonical_pr_blocker_ledger import BlockerDisposition, CanonicalPRBlockerLedger
from auto_coder.cli_helpers import AdversarialValidationAvailability
from auto_coder.github_app_reviewer import GitHubAppReviewer, ReviewerAppConfig
from auto_coder.pr_processor import TwoTierGateInputs, _execute_pending_strong_audit, _GitHubReviewEffectTransport, _retain_accepted_finding_roots
from auto_coder.pr_review_cycle import FIXED, INVALID, OPEN, ContractSnapshot, FindingDisposition, PrReviewCycleRepository, RoundProvenance, StrongPolicyIdentity
from auto_coder.pr_review_effects import AcceptedReviewPayload
from auto_coder.pr_review_execution import parse_review_result
from auto_coder.review_thread_validation import ClaimedReviewThread
from auto_coder.reviewer_session_registry import ReviewerSession, ReviewerSessionRegistry, TestOracleGap
from auto_coder.two_tier_pr_gate import TwoTierPrGate
from auto_coder.utils import CommandResult

REPO = "owner/repo"
CONTRACT = ContractSnapshot(("#2401",), "Issue #2401 REQ-001: Preserve accepted findings.")
POLICY = StrongPolicyIdentity("backend_strong_pr_adversarial_validation", "model=strong", "v1")


def _commit(_worktree: Path, label: str) -> str:
    """A distinct synthetic head; the acceptance path under test never shells out to git itself."""
    return hashlib.sha1(label.encode()).hexdigest()


@dataclass
class Env:
    tmp: Path
    worktree: Path
    base: str
    head: str
    cycle: PrReviewCycleRepository
    ledger: CanonicalPRBlockerLedger
    registry: ReviewerSessionRegistry

    def bridge(self, checkpoint: Optional[Callable[[str], None]] = None) -> AcceptedFindingBridge:
        return AcceptedFindingBridge(self.cycle, self.ledger, checkpoint)

    def reconstructed(self) -> "Env":
        """Fresh store/service objects over the same persisted files."""
        return Env(self.tmp, self.worktree, self.base, self.head, PrReviewCycleRepository(REPO, self.cycle.storage_path), CanonicalPRBlockerLedger(self.ledger._db_path), ReviewerSessionRegistry(self.registry.path))

    def target(self, pr: int, head: Optional[str] = None, **kwargs: str) -> ProjectionTarget:
        return ProjectionTarget(repository=REPO, pr_number=pr, head_sha=head or self.head, base_sha=self.base, **kwargs)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setenv("HOME", str(tmp_path))
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    base = _commit(worktree, "base")
    head = _commit(worktree, "head")
    return Env(tmp_path, worktree, base, head, PrReviewCycleRepository(REPO), CanonicalPRBlockerLedger(tmp_path / "ledger.db"), ReviewerSessionRegistry(tmp_path / "sessions.json"))


def finding_json(finding_id: str, *, gap: bool = True, boundary: str = "src/state.py:delete_two", scenario: str = "Exercise both delete paths.", requirement: str = "#2401/REQ-001") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "finding_id": finding_id,
        "requirement_ids": [requirement],
        "requirement_texts": ["Preserve accepted findings."],
        "counterexample": f"Counterexample for {finding_id}",
        "expected_behavior": f"Expected behavior for {finding_id}",
        "actual_behavior": f"Actual behavior for {finding_id}",
        "evidence": "src/state.py:40",
        "affected_boundary": boundary,
        "focused_regression_scenario": scenario,
        "is_regression_gap": gap,
    }
    if gap:
        payload.update(plausible_incorrect_implementation="Drop the guard.", why_tests_admit_it="Only helpers are tested.", material_consequence="State is lost.")
    return payload


def accept_strong(env: Env, pr: int, findings: list[dict[str, Any]], *, head: Optional[str] = None):
    """Run the production Strong acceptance path with a controlled backend payload."""
    head = head or env.head
    gate = TwoTierPrGate(REPO, env.cycle)
    gate.ordinary_pass(pr, head, env.base, CONTRACT)
    inputs = TwoTierGateInputs(gate, CONTRACT, POLICY, head, env.base)
    manager = MagicMock()
    manager.get_current_backend_identity.return_value = ("strong", "codex", "model")

    @contextlib.contextmanager
    def worktree(*args: object, **kwargs: object):
        yield str(env.worktree)

    def transport(review_input, backend_manager, cwd):
        payload = {
            "round_id": review_input.round_id,
            "attempt_id": review_input.attempt_id,
            "head_sha": review_input.head_sha,
            "base_sha": review_input.base_sha,
            "contract_identity": review_input.contract.identity,
            "policy_identity": review_input.policy.identity,
            "finding_set_revision": review_input.finding_set_revision,
            "result": "FINDINGS",
            "findings": findings,
        }
        return parse_review_result(json.dumps(payload), review_input, "strong/codex/model")

    with (
        patch("auto_coder.pr_processor.isolated_pr_head_worktree", worktree),
        patch("auto_coder.cli_helpers.resolve_adversarial_validation_availability", return_value=AdversarialValidationAvailability(backend_manager=manager)),
        patch("auto_coder.pr_processor.CommandExecutor.run_command", side_effect=[CommandResult(True, "", "", 0), CommandResult(True, "-base\n+head\n", "", 0), CommandResult(True, "contract.txt\n", "", 0)]),
        patch("auto_coder.pr_processor.execute_review", side_effect=transport),
    ):
        accepted, reason = _execute_pending_strong_audit(REPO, pr, inputs)
    assert accepted, reason
    return inputs


# -- GitHub transport -------------------------------------------------------


class RecordingClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses

    def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        return self.responses.pop(0)


def _response(data: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=data, request=httpx.Request("GET", "https://api.github.test"))


def reviewer_with(tmp: Path, monkeypatch: pytest.MonkeyPatch, reviews: list[dict[str, Any]], comments_by_review: dict[int, list[dict[str, Any]]]) -> GitHubAppReviewer:
    key = tmp / "reviewer.pem"
    key.write_text("fake", encoding="utf-8")
    monkeypatch.setattr("auto_coder.github_app_reviewer.jwt.encode", lambda *a, **k: "jwt")
    responses = [_response({"slug": "auto-coder-reviewer", "id": 4765828}), _response({"id": 77}), _response({"token": "t", "expires_at": "2099-01-01T00:00:00Z"}), _response(reviews)]
    responses.extend(_response(comments_by_review[review["id"]]) for review in reviews if review["id"] in comments_by_review)
    return GitHubAppReviewer(ReviewerAppConfig("4765828", "client", key), api_url="https://api.github.test", client=RecordingClient(responses), clock=lambda: 1000.0)


def published_roots(env: Env, pr: int, monkeypatch: pytest.MonkeyPatch, *, root_ids: Optional[dict[str, int]] = None, root_author: str = "auto-coder-reviewer[bot]", extra_roots: tuple[dict[str, Any], ...] = ()) -> RootObservation:
    """Render the exact production root comments and observe them through the real adapter."""
    snapshot = env.cycle.snapshot(pr)
    strong = snapshot.accepted_strong_round
    assert strong is not None
    payload = AcceptedReviewPayload.strong(REPO, pr, strong, snapshot.findings)
    transport = _GitHubReviewEffectTransport(MagicMock(), payload, lambda: True)
    roots = []
    for index, (finding, comment) in enumerate(zip(payload.findings, transport.comments)):
        roots.append({"id": (root_ids or {}).get(finding.finding_id, 9000 + index), "pull_request_review_id": 55, "body": comment.body, "user": {"login": root_author}})
    roots.extend(extra_roots)
    reviewer = reviewer_with(env.tmp, monkeypatch, [{"id": 55, "commit_id": strong.head_sha, "user": {"login": "auto-coder-reviewer[bot]"}}], {55: roots})
    return reviewer.observe_authenticated_review_roots(REPO, pr, strong.head_sha)


# -- ordinary rereview ------------------------------------------------------


def _context() -> AdversarialValidationContext:
    return AdversarialValidationContext(
        pr_diff="diff --git a/src/state.py b/src/state.py\n+guard = True",
        all_changed_files=["src/state.py"],
        issue_context="Issue #2401 requires preserving accepted findings.",
        issue_requirements=[IssueRequirement("#2401/REQ-001", "Preserve accepted findings.")],
    )


def _ordinary_response(gaps: list[dict[str, Any]], result: str = "PASS", threads: Optional[list[dict[str, Any]]] = None) -> str:
    return json.dumps(
        {
            "result": result,
            "summary": "Production behavior is correct.",
            "requirement_coverage": [{"requirement_id": "#2401/REQ-001", "status": "VERIFIED", "evidence": "The guard enforces the requirement."}],
            "findings": [],
            "test_oracle_gaps": gaps,
            "thread_dispositions": threads or [],
        }
    )


def run_ordinary(env: Env, pr: int, response: str, *, head: Optional[str] = None, identity: tuple[str, str, str] = ("reviewer", "codex", "strong"), session_id: str = "", claimed: tuple[ClaimedReviewThread, ...] = (), bridge: Optional[AcceptedFindingBridge] = None):
    manager = MagicMock()
    manager.get_current_backend_identity.return_value = identity
    manager._last_session_id = session_id or "provider-session"
    manager._last_continue_session_resumed = True
    manager.continue_session.return_value = response
    head = head or env.head
    with (
        patch("auto_coder.adversarial_validator.build_adversarial_validation_context", return_value=_context()),
        patch("auto_coder.adversarial_validator.run_llm_prompt", return_value=response) as fresh,
    ):
        result = run_adversarial_validation(
            REPO,
            {"number": pr, "head": {"sha": head}, "base": {"sha": env.base}},
            AutomationConfig(),
            backend_manager=manager,
            session_registry=env.registry,
            claimed_review_threads=claimed,
            accepted_finding_bridge=bridge or env.bridge(),
        )
    prompt = manager.continue_session.call_args.args[1] if manager.continue_session.called else fresh.call_args.args[0]
    return result, prompt


def save_empty_session(env: Env, pr: int, head: Optional[str] = None) -> None:
    env.registry.save(ReviewerSession(repository=REPO, pr_number=pr, backend_name="reviewer", backend_type="codex", model_name="strong", session_id="session-1", last_head_sha=head or env.head))


def _only(projection: AcceptedFindingProjection, finding_id: str):
    matches = [record for record in projection.records if record.finding_id == finding_id]
    assert len(matches) == 1
    return matches[0]


# -- AS-001 -----------------------------------------------------------------


@pytest.mark.parametrize("pr", [5438, 917])
def test_accepted_finding_is_retained_before_publication_and_known_to_ordinary_rereview(env: Env, monkeypatch: pytest.MonkeyPatch, pr: int) -> None:
    save_empty_session(env, pr)
    inputs = accept_strong(env, pr, [finding_json("finding-a")])
    strong = env.cycle.snapshot(pr).accepted_strong_round
    assert strong is not None

    # Publication absence is not absence of the finding.
    before = env.bridge().project(env.target(pr))
    record = _only(before, "finding-a")
    assert record.source_identity == f"{strong.round_id}:finding-a"
    assert record.canonical_blocker_id and record.association == NOT_PUBLISHED and before.complete
    assert record.qualified_requirements[0].issue_number == 2401 and record.requirement_ids == ("#2401/REQ-001",)
    assert (record.originating_head_sha, record.originating_base_sha, record.contract_identity, record.policy_identity) == (env.head, env.base, CONTRACT.identity, POLICY.identity)
    assert record.category == "REGRESSION_GAP" and record.accepted_state == OPEN

    inputs.gate.state.acknowledge_publication(pr, strong.round_id)
    observation = published_roots(env, pr, monkeypatch, root_ids={"finding-a": 5391105725})
    assert observation.complete and observation.roots[0].authenticated
    associated = env.bridge().project(env.target(pr), observation)
    record = _only(associated, "finding-a")
    assert record.association == ASSOCIATED and record.root_comment_ids == (5391105725,) and associated.complete
    assert record.canonical_blocker_id == _only(before, "finding-a").canonical_blocker_id

    gap_id = record.known_gap_id
    result, prompt = run_ordinary(env, pr, _ordinary_response([{"gap_id": gap_id, "status": "OPEN"}]))
    assert record.source_identity in prompt and "#2401/REQ-001" in prompt and "Counterexample for finding-a" in prompt
    assert result.result == "NEEDS_TESTS"
    assert [gap.gap_id for gap in result.open_test_oracle_gaps] == [gap_id]
    projection = result.accepted_finding_projection
    assert projection is not None and _only(projection, "finding-a").canonical_blocker_id == record.canonical_blocker_id
    assert len(env.ledger.get_snapshot("https://api.github.com", REPO, pr).blockers) == 1


@pytest.mark.parametrize("variant", ["omitted", "new_label", "model_resolved"])
def test_tog_representation_variants_never_erase_or_duplicate_the_known_finding(env: Env, variant: str) -> None:
    pr = 31
    save_empty_session(env, pr)
    accept_strong(env, pr, [finding_json("finding-a")])
    record = _only(env.bridge().project(env.target(pr)), "finding-a")
    gap_id = record.known_gap_id
    new_gap = {
        "gap_id": "TOG-model-invented",
        "requirement_id": "#2401/REQ-001",
        "authoritative_boundary": "src/state.py:delete_two",
        "invariant": "A relabelled duplicate.",
        "plausible_incorrect_implementation": "x",
        "why_tests_still_pass": "y",
        "material_consequence": "z",
        "focused_regression_scenario": "w",
        "anchor_path": "src/state.py",
        "status": "OPEN",
    }
    gaps = {
        "omitted": [],
        "new_label": [],
        "model_resolved": [{"gap_id": gap_id, "status": "RESOLVED", "resolution_evidence": "A boundary test now asserts the invariant."}],
    }[variant]
    response = _ordinary_response(gaps)
    if variant == "new_label":
        with patch("auto_coder.adversarial_validator.build_adversarial_validation_context", return_value=_context()):
            parsed = parse_adversarial_validation_response(_ordinary_response([new_gap]), [])
        reconciled = _reconcile_test_oracle_gap_lifecycle(parsed, ReviewerSession(repository=REPO, pr_number=pr, last_head_sha=env.head, test_oracle_gaps=[TestOracleGap(gap_id=gap_id, requirement_id="#2401/REQ-001", status="OPEN")]), env.head, None, frozenset({gap_id}))
        assert [gap.gap_id for gap in reconciled.test_oracle_gaps] == [gap_id]
    result, _ = run_ordinary(env, pr, response)
    assert result.result == "NEEDS_TESTS"
    assert [gap.gap_id for gap in result.open_test_oracle_gaps] == [gap_id]
    assert result.open_test_oracle_gaps[0].resolution_evidence == ""
    assert len(env.ledger.get_snapshot("https://api.github.com", REPO, pr).blockers) == 1


# -- AS-002 -----------------------------------------------------------------


def test_session_change_and_reconstruction_preserve_identity_without_new_audit(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    pr = 77
    save_empty_session(env, pr)
    inputs = accept_strong(env, pr, [finding_json("finding-a")])
    strong = env.cycle.snapshot(pr).accepted_strong_round
    assert strong is not None
    inputs.gate.state.acknowledge_publication(pr, strong.round_id)
    first = _only(env.bridge().project(env.target(pr), published_roots(env, pr, monkeypatch, root_ids={"finding-a": 4242})), "finding-a")

    fresh = env.reconstructed()
    (env.tmp / "sessions.json").unlink()  # missing ordinary session file
    result, prompt = run_ordinary(fresh, pr, _ordinary_response([{"gap_id": first.known_gap_id, "status": "OPEN"}]), identity=("other", "claude", "opus"), bridge=fresh.bridge())
    record = _only(result.accepted_finding_projection, "finding-a")
    assert (record.canonical_blocker_id, record.root_comment_ids, record.original_scope) == (first.canonical_blocker_id, (4242,), first.original_scope)
    assert first.source_identity in prompt and [gap.gap_id for gap in result.open_test_oracle_gaps] == [first.known_gap_id]
    assert len(fresh.cycle.snapshot(pr).strong_rounds) == 1
    assert len(fresh.ledger.get_snapshot("https://api.github.com", REPO, pr).blockers) == 1

    h2 = _commit(env.worktree, "head2")
    later = fresh.bridge().project(fresh.target(pr, h2))
    moved = _only(later, "finding-a")
    assert moved.canonical_blocker_id == first.canonical_blocker_id and moved.accepted_state == OPEN
    assert moved.evidence_currency == CURRENCY_HISTORICAL and _only(fresh.bridge().project(fresh.target(pr)), "finding-a").evidence_currency == CURRENCY_CURRENT_HEAD
    assert moved.originating_head_sha == env.head


# -- AS-003 -----------------------------------------------------------------


def test_legacy_state_is_reconstructed_and_uncertainty_is_not_success(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    pr = 12
    inputs = accept_strong(env, pr, [finding_json("finding-a")])
    strong = env.cycle.snapshot(pr).accepted_strong_round
    assert strong is not None
    inputs.gate.state.acknowledge_publication(pr, strong.round_id)
    observation = published_roots(env, pr, monkeypatch, root_ids={"finding-a": 31337})

    # Ledger unavailable at the association boundary: the obligation is retained, never "no blocker".
    with patch.object(env.ledger, "get_snapshot", side_effect=__import__("auto_coder.canonical_pr_blocker_ledger", fromlist=["x"]).BlockerLedgerUnavailableError("disk unavailable")):
        degraded = env.bridge().project(env.target(pr), observation)
    record = _only(degraded, "finding-a")
    assert not degraded.complete and record.accepted_state == OPEN and record.association == UNAVAILABLE and record.canonical_blocker_id == ""
    assert record.known_gap_id == "" and not degraded.known_gap_records  # no fabricated TOG

    recovered = env.bridge().project(env.target(pr), observation)
    again = env.bridge().project(env.target(pr), observation)
    assert recovered.complete and _only(recovered, "finding-a").root_comment_ids == (31337,)
    assert _only(again, "finding-a").canonical_blocker_id == _only(recovered, "finding-a").canonical_blocker_id
    assert len(env.ledger.get_snapshot("https://api.github.com", REPO, pr).blockers) == 1

    env.cycle.storage_path.write_text("{not json", encoding="utf-8")
    broken = env.bridge().project(env.target(pr))
    assert not broken.complete and broken.records == () and broken.diagnostics[0].code == "source_unavailable"


def test_ambiguous_mapping_never_invents_an_association(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    pr = 13
    inputs = accept_strong(env, pr, [finding_json("finding-a"), finding_json("finding-b")])
    strong = env.cycle.snapshot(pr).accepted_strong_round
    assert strong is not None
    inputs.gate.state.acknowledge_publication(pr, strong.round_id)
    lookalike = {"id": 7001, "pull_request_review_id": 55, "body": "Requirement #2401/REQ-001 at src/state.py:delete_two is broken", "user": {"login": "auto-coder-reviewer[bot]"}}
    forged = {"id": 7002, "pull_request_review_id": 55, "body": f"<!-- auto-coder-two-tier-finding:v1:{'0' * 64}:{hashlib.sha256(b'finding-a').hexdigest()} -->", "user": {"login": "someone-else"}}
    observation = published_roots(env, pr, monkeypatch, root_ids={"finding-a": 1, "finding-b": 2}, root_author="mallory", extra_roots=(lookalike, forged))
    assert not any(root.authenticated for root in observation.roots if root.comment_id in {1, 2, 7002})
    projection = env.bridge().project(env.target(pr), observation)
    assert not projection.complete
    for finding_id in ("finding-a", "finding-b"):
        record = _only(projection, finding_id)
        assert record.association == UNKNOWN and record.root_comment_ids == () and record.canonical_blocker_id
    assert {d.code for d in projection.diagnostics} >= {"unauthenticated_root_ignored", "root_not_observed"}
    assert _only(projection, "finding-a").canonical_blocker_id != _only(projection, "finding-b").canonical_blocker_id


# -- AS-004 / AS-005 --------------------------------------------------------


def _close(env: Env, pr: int, inputs: TwoTierGateInputs, head: str, statuses: dict[str, str]) -> None:
    strong = env.cycle.snapshot(pr).accepted_strong_round
    assert strong is not None
    inputs.gate.state.acknowledge_publication(pr, strong.round_id)
    inputs.gate.ordinary_pass(pr, head, env.base, CONTRACT)
    snapshot = env.cycle.snapshot(pr)
    env.cycle.certify_closure(
        pr,
        RoundProvenance(head, env.base),
        CONTRACT,
        POLICY,
        strong.round_id,
        snapshot.finding_set_revision,
        [FindingDisposition(finding_id, status, f"Independent exact-finding evidence for {finding_id}", head) for finding_id, status in statuses.items()],
        bounded=True,
        bounded_evidence="Only the repair and its regression changed.",
    )


def test_unaccepted_closure_representations_never_close_and_accepted_closure_survives_echoes(env: Env) -> None:
    pr = 88
    inputs = accept_strong(env, pr, [finding_json("finding-a"), finding_json("finding-b", boundary="src/state.py:other", scenario="A different correction.")])
    bridge = env.bridge()
    record = _only(bridge.project(env.target(pr)), "finding-a")
    for status, evidence, expected in (
        ("ADDRESSED", "A boundary test asserts it.", OUTCOME_CLOSURE_PROPOSAL_NOT_ACCEPTED),
        ("INCONCLUSIVE", "", OUTCOME_UNRESOLVED_RETAINED),
        ("STILL_VALID", "", OUTCOME_UNRESOLVED_RETAINED),  # malformed: no current-target evidence
        ("STILL_VALID", "src/state.py:40 still drops state", OUTCOME_STILL_VALID_OBSERVED),
    ):
        projection = bridge.project(env.target(pr), dispositions=[OrdinaryDisposition(status=status, rationale="reasoned" if evidence else "", evidence=evidence, finding_id="finding-a")])
        assert projection.disposition_outcomes[0].outcome == expected
        current = _only(projection, "finding-a")
        assert current.accepted_state == OPEN and current.ledger_disposition == BlockerDisposition.OPEN.value
        assert _only(projection, "finding-b").current_observations == ()
    unknown = bridge.project(env.target(pr), dispositions=[OrdinaryDisposition(status="STILL_VALID", rationale="r", evidence="e", finding_id="no-such-finding")])
    assert unknown.disposition_outcomes[0].outcome == OUTCOME_UNRECOGNIZED and all(r.current_observations == () for r in unknown.records)
    assert env.cycle.snapshot(pr).findings[0].status == OPEN  # nothing mutated the owning lifecycle

    h2 = _commit(env.worktree, "head2")
    _close(env, pr, inputs, h2, {"finding-a": FIXED, "finding-b": INVALID})
    closed = bridge.project(env.target(pr, h2))
    a, b = _only(closed, "finding-a"), _only(closed, "finding-b")
    assert (a.accepted_state, a.ledger_disposition, a.closure_head_sha) == (FIXED, "VERIFIED_CORRECTION", h2)
    assert "Independent exact-finding evidence" in a.closure_evidence and a.evidence_currency == CURRENCY_CURRENT_HEAD
    assert (b.accepted_state, b.ledger_disposition) == (INVALID, "AUTHORIZED_INVALIDATION")
    assert closed.complete and not closed.unresolved and not closed.known_gap_records

    echoed = bridge.project(env.target(pr, h2), dispositions=[OrdinaryDisposition(status="STILL_VALID", rationale="stale", evidence="stale echo", finding_id="finding-a")])
    assert echoed.disposition_outcomes[0].outcome == OUTCOME_ACCEPTED_CLOSURE_RETAINED and _only(echoed, "finding-a").accepted_state == FIXED
    h3 = _commit(env.worktree, "head3")
    historical = _only(bridge.project(env.target(pr, h3)), "finding-a")
    assert historical.accepted_state == FIXED and historical.evidence_currency == CURRENCY_HISTORICAL


def test_ordinary_review_cannot_close_or_reopen_through_model_output(env: Env) -> None:
    pr = 89
    save_empty_session(env, pr)
    accept_strong(env, pr, [finding_json("finding-a")])
    record = _only(env.bridge().project(env.target(pr)), "finding-a")
    thread = ClaimedReviewThread(thread_id="PRRT_1", root_comment_database_id=555, original_finding=f"<!-- auto-coder-two-tier-finding:v1:{'1' * 64}:{'2' * 64} -->", blocker_ids=(), concern_ids=())
    addressed = [{"thread_id": "PRRT_1", "status": "ADDRESSED", "rationale": "Fixed.", "evidence": "tests/test_state.py asserts the invariant"}]
    result, _ = run_ordinary(env, pr, _ordinary_response([{"gap_id": record.known_gap_id, "status": "RESOLVED", "resolution_evidence": "tests/test_state.py asserts it"}], threads=addressed), claimed=(thread,))
    assert result.result == "NEEDS_TESTS" and [g.gap_id for g in result.open_test_oracle_gaps] == [record.known_gap_id]
    assert _only(result.accepted_finding_projection, "finding-a").accepted_state == OPEN
    assert env.cycle.snapshot(pr).findings[0].status == OPEN


def test_shared_requirement_and_path_stay_distinct_and_weaker_scope_is_rejected(env: Env) -> None:
    pr = 90
    save_empty_session(env, pr)
    accept_strong(env, pr, [finding_json("finding-a"), finding_json("finding-b", scenario="A different correction.")])
    projection = env.bridge().project(env.target(pr))
    a, b = _only(projection, "finding-a"), _only(projection, "finding-b")
    assert a.canonical_blocker_id != b.canonical_blocker_id and a.requirement_ids == b.requirement_ids and a.affected_boundary == b.affected_boundary
    weaker = {
        "gap_id": a.known_gap_id,
        "requirement_id": "#2401/REQ-001",
        "authoritative_boundary": "src/state.py",
        "invariant": "Something much weaker.",
        "plausible_incorrect_implementation": "p",
        "why_tests_still_pass": "w",
        "material_consequence": "m",
        "focused_regression_scenario": "f",
        "anchor_path": "src/state.py",
        "status": "OPEN",
    }
    result, _ = run_ordinary(env, pr, _ordinary_response([weaker]))
    kept = {gap.gap_id: gap for gap in result.open_test_oracle_gaps}
    assert set(kept) == {a.known_gap_id, b.known_gap_id}
    assert kept[a.known_gap_id].invariant == a.original_scope and kept[a.known_gap_id].authoritative_boundary == a.affected_boundary


@pytest.mark.parametrize("exception", ["NONE", "CORRECTIVE_DIFF"])
def test_new_gap_admission_is_unchanged_when_no_accepted_source_exists(env: Env, exception: str) -> None:
    pr = 91
    save_empty_session(env, pr)
    gap = {
        "requirement_id": "#2401/REQ-001",
        "authoritative_boundary": "src/other.py",
        "invariant": "Unrelated invariant.",
        "plausible_incorrect_implementation": "p",
        "why_tests_still_pass": "w",
        "material_consequence": "m",
        "focused_regression_scenario": "f",
        "anchor_path": "src/state.py",
        "discovery_phase": "REREVIEW",
        "rereview_exception_reason": exception,
        "rereview_exception_evidence": "The corrective diff changed it." if exception != "NONE" else "",
        "status": "OPEN",
    }
    response = _ordinary_response([gap], result="NEEDS_TESTS")
    with_bridge, _ = run_ordinary(env, pr, response, bridge=env.bridge())
    with patch("auto_coder.adversarial_validator.default_accepted_finding_bridge", return_value=env.bridge()):
        default, _ = run_ordinary(env, pr, response)
    save_empty_session(env, pr)
    plain = _reconcile_test_oracle_gap_lifecycle(parse_adversarial_validation_response(response, []), env.registry.get(REPO, pr, "reviewer", "codex", "strong"), env.head)
    shape = lambda r: (r.result, [(g.gap_id, g.status) for g in r.test_oracle_gaps])  # noqa: E731
    assert shape(with_bridge) == shape(default) == shape(plain)
    assert with_bridge.accepted_finding_projection is not None and with_bridge.accepted_finding_projection.records == ()


# -- AS-006 -----------------------------------------------------------------


def test_contested_closure_is_not_overwritten_by_a_stale_participant(env: Env) -> None:
    pr = 61
    inputs = accept_strong(env, pr, [finding_json("finding-a")])
    env.bridge().project(env.target(pr))  # ledger now mirrors accepted OPEN
    h2 = _commit(env.worktree, "head2")
    competing: list[AcceptedFindingProjection] = []

    def hook(name: str) -> None:
        if name == "after_source_read" and not competing:
            _close(env, pr, inputs, h2, {"finding-a": FIXED})
            competing.append(env.reconstructed().bridge().project(env.target(pr, h2)))
            # The competing transition really completed before the paused participant resumes.
            assert env.cycle.snapshot(pr).findings[0].status == FIXED
            assert _only(competing[0], "finding-a").ledger_disposition == "VERIFIED_CORRECTION"

    result = env.bridge(hook).project(env.target(pr, h2))
    record = _only(result, "finding-a")
    assert competing and (record.accepted_state, record.ledger_disposition) == (FIXED, "VERIFIED_CORRECTION") and result.complete
    blocker = env.ledger.get_snapshot("https://api.github.com", REPO, pr).blockers[0]
    assert blocker.disposition is BlockerDisposition.VERIFIED_CORRECTION and not blocker.get_root_comment_ids()


def test_stale_ledger_revision_retries_and_failed_write_keeps_prior_state(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    pr = 62
    inputs = accept_strong(env, pr, [finding_json("finding-a")])
    strong = env.cycle.snapshot(pr).accepted_strong_round
    assert strong is not None
    inputs.gate.state.acknowledge_publication(pr, strong.round_id)
    first = _only(env.bridge().project(env.target(pr)), "finding-a")
    observation = published_roots(env, pr, monkeypatch, root_ids={"finding-a": 808})

    bumped: list[str] = []

    def hook(name: str) -> None:
        if name == "before_association_commit" and not bumped:
            revision = env.ledger.get_snapshot("https://api.github.com", REPO, pr).ledger_revision
            env.ledger.record_evidence("https://api.github.com", REPO, pr, "competing-observation", revision, first.canonical_blocker_id, __import__("auto_coder.canonical_pr_blocker_ledger", fromlist=["x"]).EvidenceAvailability.KNOWN, "competing newer association")
            bumped.append("done")

    env.ledger._simulate_failure_before_commit = True
    failed = env.bridge().project(env.target(pr), observation)
    env.ledger._simulate_failure_before_commit = False
    assert not failed.complete and any(d.code == "association_write_failed" for d in failed.diagnostics)
    assert _only(failed, "finding-a").accepted_state == OPEN and env.cycle.snapshot(pr).findings[0].status == OPEN
    assert not env.ledger.get_snapshot("https://api.github.com", REPO, pr).blockers[0].get_root_comment_ids()

    retried = env.bridge(hook).project(env.target(pr), observation)
    assert bumped and retried.complete and _only(retried, "finding-a").root_comment_ids == (808,)
    assert _only(retried, "finding-a").canonical_blocker_id == first.canonical_blocker_id
    assert len(env.ledger.get_snapshot("https://api.github.com", REPO, pr).blockers) == 1


# -- boundaries --------------------------------------------------------------


def test_prs_and_reopen_epochs_do_not_leak_identity_or_state(env: Env) -> None:
    accept_strong(env, 1, [finding_json("finding-a")])
    other = env.bridge().project(env.target(2))
    assert other.complete and other.records == ()
    projection = env.bridge().project(env.target(1))
    original = _only(projection, "finding-a")
    env.cycle.mark_closed(1)
    env.cycle.mark_reopened(1)
    reopened = env.bridge().project(env.target(1))
    assert reopened.records and not reopened.complete and any(d.code == "epoch_reconciliation_required" for d in reopened.diagnostics)
    stale = _only(reopened, "finding-a")
    assert stale.target_binding == BINDING_RECONCILIATION_REQUIRED and reopened.known_gap_records == ()  # not reused as current authority
    assert stale.canonical_blocker_id == original.canonical_blocker_id and len(env.ledger.get_snapshot("https://api.github.com", REPO, 1).blockers) == 1
    assert env.bridge().project(env.target(2)).records == ()  # the other PR never sees PR #1 state


def test_incompatible_contract_requires_reconciliation_and_empty_store_reads_complete(env: Env) -> None:
    empty = env.bridge().project(env.target(3))
    assert empty.complete and empty.records == () and empty.source_revision == 0
    accept_strong(env, 4, [finding_json("finding-a")])
    changed = env.bridge().project(env.target(4, contract_identity="a-different-contract"))
    record = _only(changed, "finding-a")
    assert record.target_binding == BINDING_RECONCILIATION_REQUIRED and not changed.complete and changed.known_gap_records == ()
    assert _only(env.bridge().project(env.target(4, contract_identity=CONTRACT.identity, policy_identity=POLICY.identity)), "finding-a").target_binding == "CURRENT"


def test_publication_hook_retains_identity_and_roots_through_the_real_adapter(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    pr = 14
    inputs = accept_strong(env, pr, [finding_json("finding-a")])
    strong = env.cycle.snapshot(pr).accepted_strong_round
    assert strong is not None
    inputs.gate.state.acknowledge_publication(pr, strong.round_id)
    snapshot = env.cycle.snapshot(pr)
    payload = AcceptedReviewPayload.strong(REPO, pr, strong, snapshot.findings)
    body = _GitHubReviewEffectTransport(MagicMock(), payload, lambda: True).comments[0].body
    reviewer = reviewer_with(env.tmp, monkeypatch, [{"id": 55, "commit_id": env.head, "user": {"login": "auto-coder-reviewer[bot]"}}], {55: [{"id": 6001, "pull_request_review_id": 55, "body": body, "user": {"login": "auto-coder-reviewer[bot]"}}]})
    with patch("auto_coder.pr_processor.CanonicalPRBlockerLedger", return_value=env.ledger):
        _retain_accepted_finding_roots(REPO, pr, inputs, reviewer, env.head)
    assert _only(env.bridge().project(env.target(pr)), "finding-a").root_comment_ids == (6001,)

    broken = reviewer_with(env.tmp, monkeypatch, [], {})
    broken._client = RecordingClient([_response({}, 500)])  # type: ignore[assignment]
    observation = broken.observe_authenticated_review_roots(REPO, pr, env.head)
    assert not observation.complete and observation.reason and observation.roots == ()


def test_unobserved_roots_for_untouched_ordinary_review_do_not_create_authority(env: Env) -> None:
    pr = 15
    accept_strong(env, pr, [finding_json("finding-a")])
    stray = ObservedRoot(comment_id=1, body="A human comment mentioning finding-a", authenticated=False)
    projection = env.bridge().project(env.target(pr), RootObservation(roots=(stray,), complete=True))
    assert _only(projection, "finding-a").root_comment_ids == () and any(d.code == "unauthenticated_root_ignored" for d in projection.diagnostics)


@pytest.mark.parametrize("closed", [False, True])
def test_shared_root_non_strong_owner_does_not_make_accepted_disposition_ambiguous(env: Env, closed: bool) -> None:
    from auto_coder.canonical_pr_blocker_ledger import BlockerAdmissionPayload, BlockerAlias, CorrectionScope

    pr = 101
    inputs = accept_strong(env, pr, [finding_json("finding-a")])
    bridge = env.bridge()
    record = _only(bridge.project(env.target(pr)), "finding-a")
    snapshot = env.ledger.get_snapshot("https://api.github.com", REPO, pr)
    snapshot = env.ledger.add_alias("https://api.github.com", REPO, pr, operation_id="accepted-root", expected_ledger_revision=snapshot.ledger_revision, blocker_id=record.canonical_blocker_id, alias_type="github_root_comment", alias_value="555")
    other_id, snapshot = env.ledger.admit_blocker(
        "https://api.github.com",
        REPO,
        pr,
        operation_id="historical-owner",
        expected_ledger_revision=snapshot.ledger_revision,
        payload=BlockerAdmissionPayload(
            category="IMPLEMENTATION",
            authoritative_boundary="src/state.py:delete_two",
            incorrect_behavior_or_missing_invariant="Historical correction",
            required_correction_outcome="Keep independent scope",
            evidence_needed="Independent evidence",
            accepted_scope=CorrectionScope(description="Historical correction", concern_ids=("historical",)),
            aliases=(BlockerAlias(alias_type="github_root_comment", alias_value="555"),),
        ),
    )
    if closed:
        _close(env, pr, inputs, env.head, {"finding-a": FIXED})
    projection = bridge.project(env.target(pr), dispositions=[OrdinaryDisposition(status="ADDRESSED", rationale="Regression added", evidence="tests/test_state.py:40", root_comment_id=555, thread_id="PRRT_shared")])
    assert projection.complete
    assert [(outcome.source_identity, outcome.outcome) for outcome in projection.disposition_outcomes] == [(record.source_identity, OUTCOME_ACCEPTED_CLOSURE_RETAINED if closed else OUTCOME_CLOSURE_PROPOSAL_NOT_ACCEPTED)]
    current = _only(projection, "finding-a")
    assert current.accepted_state == (FIXED if closed else OPEN)
    snapshot = env.ledger.get_snapshot("https://api.github.com", REPO, pr)
    assert snapshot.get_blocker(other_id).disposition is BlockerDisposition.OPEN
    assert len(snapshot.get_blockers_for_alias("github_root_comment", "555")) == 2
    assert current.requirement_ids == ("#2401/REQ-001",)


def test_shared_root_multiple_strong_owners_remains_ambiguous(env: Env) -> None:
    from auto_coder.accepted_finding_bridge import OUTCOME_AMBIGUOUS

    pr = 102
    accept_strong(env, pr, [finding_json("finding-a"), finding_json("finding-b", boundary="src/other.py")])
    bridge = env.bridge()
    projection = bridge.project(env.target(pr))
    snapshot = env.ledger.get_snapshot("https://api.github.com", REPO, pr)
    for record in projection.records:
        snapshot = env.ledger.add_alias("https://api.github.com", REPO, pr, operation_id=f"root-{record.finding_id}", expected_ledger_revision=snapshot.ledger_revision, blocker_id=record.canonical_blocker_id, alias_type="github_root_comment", alias_value="555")
    projection = bridge.project(env.target(pr), dispositions=[OrdinaryDisposition(status="ADDRESSED", rationale="Fixed", evidence="tests/test_state.py:40", root_comment_id=555)])
    assert projection.disposition_outcomes[0].outcome == OUTCOME_AMBIGUOUS
    assert projection.disposition_outcomes[0].source_identity == ""
    assert all(record.accepted_state == OPEN for record in projection.records)
