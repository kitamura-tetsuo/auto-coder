"""Durable retention and cycle acceptance of ordinary closure evidence.

Every scenario drives the production ordinary assessment producer, a real
registered ordinary attempt, a real review-cycle store, and a real temporary
Git repository. Stores are reconstructed from disk wherever a restart matters.
"""

from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

import pytest

from auto_coder.adversarial_validation_attempts import AdversarialValidationAttemptRepository
from auto_coder.adversarial_validator import AdversarialValidationResult, parse_adversarial_validation_response
from auto_coder.ordinary_closure_evidence import (
    ApplicationStatus,
    EvidenceState,
    ObservedTarget,
    OrdinaryClosureEvidence,
    OrdinaryClosureEvidenceRepository,
)
from auto_coder.pr_review_cycle import (
    FIXED,
    PHASE_COMPLETE,
    PHASE_ORDINARY_CLOSURE,
    PHASE_STRONG_PENDING,
    VERDICT_FINDINGS,
    ContractSnapshot,
    Finding,
    PrReviewCycleRepository,
    RoundProvenance,
    StrongPolicyIdentity,
)
from auto_coder.pr_review_execution import ReviewExecutionInput, ReviewMode
from auto_coder.two_tier_pr_gate import TwoTierPrGate

REPO = "owner/repo"
PR = 77
CONTRACT = ContractSnapshot(("#2406",), "Issue #2406 REQ-001: retain closure evidence\nIssue #2406 REQ-002: apply it once")
POLICY = StrongPolicyIdentity("strong-route", "model-1", "v1")


class SimulatedCrash(BaseException):
    """Process death: not catchable by ordinary exception handling."""


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def _commit(cwd: Path, name: str) -> str:
    (cwd / name).write_text(name)
    _git(cwd, "add", name)
    _git(cwd, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-m", name)
    return _git(cwd, "rev-parse", "HEAD")


def _finding(identifier: str, origin: str) -> Finding:
    return Finding(
        finding_id=identifier,
        origin_round_id=origin,
        requirement_ids=("#2406/REQ-004",),
        requirement_texts=("REQ-004: certify bounded closure",),
        counterexample=f"{identifier} counterexample",
        expected_behavior="expected",
        actual_behavior="actual",
        evidence="evidence",
        affected_boundary="boundary",
        focused_regression_scenario="scenario",
    )


@dataclass
class World:
    root: Path
    git_dir: Path
    base: str
    h0: str
    h2: str
    round_id: str

    def cycle(self) -> PrReviewCycleRepository:
        return PrReviewCycleRepository(REPO, self.root / "cycle.json")

    def attempts(self) -> AdversarialValidationAttemptRepository:
        return AdversarialValidationAttemptRepository(REPO, self.root / "attempts.json")

    def store(self) -> OrdinaryClosureEvidenceRepository:
        return OrdinaryClosureEvidenceRepository(REPO, self.root / "evidence.json")

    def service(self) -> OrdinaryClosureEvidence:
        """Reconstruct every owning store from disk, as after a restart."""
        return OrdinaryClosureEvidence(self.store(), self.cycle(), self.attempts())

    def observe(self, contract: ContractSnapshot = CONTRACT, policy: StrongPolicyIdentity = POLICY) -> Callable[[], ObservedTarget]:
        def observer() -> ObservedTarget:
            return ObservedTarget(_git(self.git_dir, "rev-parse", "HEAD"), _git(self.git_dir, "rev-parse", "base"), contract, policy)

        return observer


@pytest.fixture
def world(tmp_path: Path, _use_real_commands: None) -> World:
    git_dir = tmp_path / "repo"
    git_dir.mkdir()
    _git(git_dir, "init", "-q")
    base = _commit(git_dir, "base.txt")
    _git(git_dir, "tag", "base")
    h0 = _commit(git_dir, "h0.txt")
    h2 = _commit(git_dir, "h2.txt")
    _git(git_dir, "checkout", "-q", h0)
    cycle = PrReviewCycleRepository(REPO, tmp_path / "cycle.json")
    cycle.mark_reopened(PR)  # open epoch 1
    cycle.record_ordinary_pass(PR, RoundProvenance(h0, base), CONTRACT)
    claim = cycle.claim_strong_audit(PR, RoundProvenance(h0, base), CONTRACT, POLICY)
    strong = cycle.record_strong_result(PR, claim.claim_id, VERDICT_FINDINGS, "codex/strong", [_finding("f1", claim.claim_id), _finding("f2", claim.claim_id)])
    cycle.acknowledge_publication(PR, strong.round_id)
    _git(git_dir, "checkout", "-q", h2)
    return World(tmp_path, git_dir, base, h0, h2, strong.round_id)


def _produce(
    world: World, *, scope: str = "BOUNDED", statuses: Optional[dict[str, str]] = None, verdict: str = "PASS", ordinary: str = "PASS", coverage: str = "VERIFIED", extra: Optional[dict] = None, coverage_ids: tuple[str, ...] = ("REQ-001", "REQ-002"), evidence: str = "verified"
) -> AdversarialValidationResult:
    """Run the production ordinary producer for a real registered attempt."""
    attempts = world.attempts()
    attempt = attempts.start(PR, world.h2)
    snapshot = world.cycle().snapshot(PR)
    closure_input = ReviewExecutionInput(
        mode=ReviewMode.ORDINARY_CLOSURE,
        round_id=world.round_id,
        attempt_id=attempt.attempt_id,
        head_sha=world.h2,
        base_sha=world.base,
        contract=CONTRACT,
        policy=POLICY,
        repository_evidence="tracked paths",
        diff_evidence="complete H0-to-H2 diff",
        repository=REPO,
        pr_number=PR,
        open_epoch=snapshot.open_epoch,
        attempt_sequence=attempt.sequence,
        finding_set_revision=snapshot.finding_set_revision,
        findings=snapshot.open_findings,
        audited_head_sha=world.h0,
    )
    statuses = statuses or {"f1": "FIXED", "f2": "INVALID"}
    assessment = {
        "verdict": verdict,
        "findings": [],
        "dispositions": [{"finding_id": key, "status": value, "evidence": f"{key} evidence at H2"} for key, value in statuses.items()],
        "scope": scope,
        "scope_evidence": "complete cumulative diff inspected",
    }
    assessment.update(extra or {})
    payload = {
        "result": ordinary,
        "summary": "ordinary summary",
        "findings": [],
        "requirement_coverage": [{"requirement_id": identity, "status": coverage, "evidence": evidence} for identity in coverage_ids],
        "closure_assessment": assessment,
    }
    result = parse_adversarial_validation_response(json.dumps(payload), closure_input=closure_input, reviewer_provenance="codex/ordinary-model")
    result.attempt_id = attempt.attempt_id
    result.attempt_sequence = attempt.sequence
    return result


@pytest.fixture(autouse=True)
def no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("closure application must not invoke a model")

    monkeypatch.setattr("auto_coder.pr_review_execution.run_llm_prompt", forbidden)
    monkeypatch.setattr("auto_coder.adversarial_validator.run_llm_prompt", forbidden, raising=False)


def _retain(world: World, **kwargs: object) -> str:
    view = world.service().retain(_produce(world, **kwargs))  # type: ignore[arg-type]
    assert view.state is EvidenceState.RETAINED, view.reason
    assert view.record is not None
    return view.record.source_id


# -- AS-001 -------------------------------------------------------------


def test_retained_assessment_survives_reconstruction_and_certifies_without_a_model(world: World) -> None:
    source_id = _retain(world)

    recovered = world.service().inspect(source_id)  # all in-memory results are gone
    assert recovered.state is EvidenceState.RETAINED and recovered.record is not None
    record = recovered.record
    assert (record.scope, record.verdict, record.reviewer_provenance) == ("BOUNDED", "PASS", "codex/ordinary-model")
    assert (record.head_sha, record.base_sha, record.audited_head_sha) == (world.h2, world.base, world.h0)
    assert {(item.finding_id, item.status) for item in record.dispositions} == {("f1", "FIXED"), ("f2", "INVALID")}
    assert record.ordinary.is_semantic_pass and record.attempt_sequence == 1

    outcome = world.service().apply(source_id, world.observe())

    assert outcome.status is ApplicationStatus.ACCEPTED and outcome.cycle_committed
    assert outcome.pending_publication == "CLOSURE_PUBLICATION" and not outcome.merge_authorized
    assert (outcome.reviewer_provenance, outcome.attempt_sequence) == ("codex/ordinary-model", 1)
    snapshot = world.cycle().snapshot(PR)
    assert snapshot.open_findings == () and {item.status for item in snapshot.findings} == {FIXED, "INVALID"}
    assert snapshot.accepted_closure is not None and snapshot.accepted_closure.source_identity == source_id
    assert snapshot.phase == PHASE_ORDINARY_CLOSURE and snapshot.completion is None
    gate = TwoTierPrGate(REPO, world.cycle())
    assert not gate.authorize_merge(PR, current_head_sha=world.h2, current_base_sha=world.base, current_contract=CONTRACT, current_policy=POLICY)
    assert world.service().inspect(source_id).state is EvidenceState.ACCEPTED


def test_gate_binds_evidence_to_its_review_cycle_owner(world: World) -> None:
    gate = TwoTierPrGate(REPO, world.cycle())
    service = gate.closure_evidence(world.attempts(), world.store())
    source_id = service.retain(_produce(world)).record.source_id  # type: ignore[union-attr]

    assert service.apply(source_id, world.observe()).status is ApplicationStatus.ACCEPTED
    assert gate.state.snapshot(PR).accepted_closure is not None


def test_replay_is_idempotent_and_keeps_one_closure_identity(world: World) -> None:
    source_id = _retain(world)
    first = world.service().apply(source_id, world.observe())
    version = world.cycle().snapshot(PR).transition_version
    second = world.service().apply(source_id, world.observe())

    assert (second.status, second.closure_id) == (ApplicationStatus.ACCEPTED, first.closure_id)
    snapshot = world.cycle().snapshot(PR)
    assert snapshot.transition_version == version and [item.source_identity for item in snapshot.closures] == [source_id]


# -- AS-002 -------------------------------------------------------------


def test_bare_pass_and_audit_prose_cannot_fabricate_authority(world: World) -> None:
    world.cycle().record_ordinary_pass(PR, RoundProvenance(world.h2, world.base), CONTRACT)  # legacy ordinary PASS tuple
    bare = _produce(world)
    bare.closure_assessment = None
    bare.closure_assessment_diagnostic = "Closure assessment is absent or malformed"
    bare.summary = "Everything is fixed; closure is certainly approved."

    view = world.service().retain(bare)

    assert view.state is EvidenceState.MISSING
    assert world.service().apply("no-such-source", world.observe()).status is ApplicationStatus.MISSING
    assert world.cycle().snapshot(PR).accepted_closure is None and len(world.cycle().snapshot(PR).open_findings) == 2


def test_failed_write_and_corrupt_record_are_distinct_unavailable_states(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _produce(world)

    def failing_write(self: OrdinaryClosureEvidenceRepository, state: dict) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patched:
        patched.setattr(OrdinaryClosureEvidenceRepository, "_write", failing_write)
        failed = world.service().retain(result)
    assert failed.state is EvidenceState.UNAVAILABLE
    assert world.store().pending_for_pr(PR) == ()

    source_id = world.service().retain(result).record.source_id  # type: ignore[union-attr]
    (world.root / "evidence.json").write_text("{not json")
    assert world.service().inspect(source_id).state is EvidenceState.UNAVAILABLE
    outcome = world.service().apply(source_id, world.observe())
    assert outcome.status is ApplicationStatus.UNAVAILABLE
    assert world.cycle().snapshot(PR).accepted_closure is None
    assert world.service().retain(result).state is EvidenceState.UNAVAILABLE  # corrupt store is never silently replaced


def test_existing_strong_pass_completion_remains_readable(tmp_path: Path) -> None:
    cycle = PrReviewCycleRepository(REPO, tmp_path / "cycle.json")
    gate = TwoTierPrGate(REPO, cycle)
    gate.ordinary_pass(5, "h", "b", CONTRACT)
    claim = cycle.claim_strong_audit(5, RoundProvenance("h", "b"), CONTRACT, POLICY)
    strong = cycle.record_strong_result(5, claim.claim_id, "PASS", "codex/strong")
    cycle.acknowledge_publication(5, strong.round_id)
    cycle.accept_strong_pass_completion(5, strong.round_id)

    assert gate.authorize_merge(5, current_head_sha="h", current_base_sha="b", current_contract=CONTRACT, current_policy=POLICY)
    assert cycle.snapshot(5).phase == PHASE_COMPLETE


# -- AS-003 -------------------------------------------------------------


@pytest.mark.parametrize("scope", ["EXPANDED", "UNKNOWN"])
def test_complete_nonbounded_scope_requires_renewed_strong_audit(world: World, scope: str) -> None:
    source_id = _retain(world, scope=scope)

    outcome = world.service().apply(source_id, world.observe())

    assert outcome.status is ApplicationStatus.ACCEPTED and outcome.requires_strong_audit
    snapshot = world.cycle().snapshot(PR)
    assert snapshot.requires_new_strong_round and snapshot.accepted_closure is None
    assert snapshot.phase == PHASE_STRONG_PENDING and snapshot.open_findings == ()
    assert snapshot.closures[0].bounded is False and snapshot.closures[0].source_identity == source_id
    gate = TwoTierPrGate(REPO, world.cycle())
    assert not gate.authorize_merge(PR, current_head_sha=world.h2, current_base_sha=world.base, current_contract=CONTRACT, current_policy=POLICY)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"statuses": {"f1": "FIXED", "f2": "INCONCLUSIVE"}, "verdict": "INCONCLUSIVE"},
        {"statuses": {"f1": "FIXED", "f2": "OPEN"}, "verdict": "FINDINGS"},
        {"statuses": {"f1": "FIXED"}},  # omitted finding: producer reports an incomplete assessment
        {"ordinary": "INCONCLUSIVE"},
        {"coverage": "UNVERIFIED"},
        {"scope": "BOUNDED", "extra": {"scope_evidence": ""}},  # cumulative diff could not be assessed
    ],
)
def test_incomplete_or_blocked_evidence_never_certifies(world: World, kwargs: dict) -> None:
    result = _produce(world, **kwargs)
    if kwargs.get("ordinary") == "INCONCLUSIVE":
        assert result.closure_assessment is not None  # favorable closure text beside a nonpassing result
    view = world.service().retain(result)
    if view.record is None:
        assert view.state is EvidenceState.MISSING
        return

    outcome = world.service().apply(view.record.source_id, world.observe())

    assert outcome.status is ApplicationStatus.NON_AUTHORIZING
    snapshot = world.cycle().snapshot(PR)
    assert snapshot.accepted_closure is None and snapshot.closures == () and len(snapshot.open_findings) == 2
    assert world.service().inspect(view.record.source_id).state is EvidenceState.RETAINED


def test_new_independent_defect_remains_blocking(world: World) -> None:
    new = {
        "finding_id": "f3",
        "requirement_ids": ["#2406/REQ-004"],
        "requirement_texts": ["REQ-004: certify bounded closure"],
        "counterexample": "c",
        "expected_behavior": "e",
        "actual_behavior": "a",
        "evidence": "v",
        "affected_boundary": "b",
        "focused_regression_scenario": "s",
    }
    source_id = _retain(world, verdict="FINDINGS", extra={"findings": [new]})

    assert world.service().apply(source_id, world.observe()).status is ApplicationStatus.NON_AUTHORIZING
    assert world.cycle().snapshot(PR).accepted_closure is None


# -- AS-004 -------------------------------------------------------------


def _mutations(world: World) -> dict[str, Callable[[], Callable[[], ObservedTarget]]]:
    def with_target(**changes: object) -> Callable[[], Callable[[], ObservedTarget]]:
        def build() -> Callable[[], ObservedTarget]:
            base = world.observe()
            return lambda: replace(base(), **changes)  # type: ignore[arg-type]

        return build

    def newer_round() -> Callable[[], ObservedTarget]:
        cycle = world.cycle()
        cycle.record_ordinary_pass(PR, RoundProvenance("e" * 40, world.base), CONTRACT)
        claim = cycle.claim_strong_audit(PR, RoundProvenance("e" * 40, world.base), CONTRACT, POLICY)
        cycle.record_strong_result(PR, claim.claim_id, VERDICT_FINDINGS, "codex/strong", [_finding("f3", claim.claim_id)])
        return world.observe()

    def newer_claim() -> Callable[[], ObservedTarget]:
        world.cycle().record_ordinary_pass(PR, RoundProvenance("d" * 40, world.base), CONTRACT)
        world.cycle().claim_strong_audit(PR, RoundProvenance("d" * 40, world.base), CONTRACT, POLICY)
        return world.observe()

    def reopened() -> Callable[[], ObservedTarget]:
        world.cycle().mark_reopened(PR)
        return world.observe()

    return {
        "head": with_target(head_sha="a" * 40),
        "base": with_target(base_sha="c" * 40),
        "requirements": with_target(contract=ContractSnapshot(("#2406",), "Issue #2406 REQ-001: changed")),
        "policy": with_target(policy=StrongPolicyIdentity("strong-route", "model-2", "v1")),
        "round-and-finding-revision": newer_round,
        "strong-claim": newer_claim,
        "open-epoch": reopened,
    }


@pytest.mark.parametrize("change", ["head", "base", "requirements", "policy", "round-and-finding-revision", "strong-claim", "open-epoch"])
def test_each_binding_mismatch_rejects_the_stale_source(world: World, change: str) -> None:
    source_id = _retain(world)
    observer = _mutations(world)[change]()

    outcome = world.service().apply(source_id, observer)

    assert outcome.status is ApplicationStatus.REJECTED and not outcome.cycle_committed
    assert world.cycle().snapshot(PR).accepted_closure is None
    assert world.service().inspect(source_id).state is EvidenceState.REJECTED
    assert world.service().apply(source_id, world.observe()).status is ApplicationStatus.REJECTED


def test_unavailable_observation_defers_and_identical_restoration_applies(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    source_id = _retain(world)

    def unavailable() -> ObservedTarget:
        raise ConnectionError("GitHub unavailable")

    assert world.service().apply(source_id, unavailable).status is ApplicationStatus.DEFERRED
    assert world.service().inspect(source_id).state is EvidenceState.RETAINED

    service = world.service()
    original = service.cycle.snapshot

    def flaky(pr_number: int):  # owning-store read unavailable once
        monkeypatch.setattr(service.cycle, "snapshot", original)
        raise OSError("store unavailable")

    monkeypatch.setattr(service.cycle, "snapshot", flaky)
    assert service.apply(source_id, world.observe()).status is ApplicationStatus.DEFERRED
    assert world.service().inspect(source_id).state is EvidenceState.RETAINED
    assert world.service().apply(source_id, world.observe()).status is ApplicationStatus.ACCEPTED  # no new attempt or provider action
    assert world.attempts().latest_sequence(PR, world.h2) == 1


def test_publication_acknowledgement_version_change_does_not_strand_evidence(world: World) -> None:
    source_id = _retain(world)
    before = world.cycle().snapshot(PR).transition_version
    world.cycle().set_finding_delivery_status(PR, "f1", "PENDING")  # bookkeeping-only version bump
    assert world.cycle().snapshot(PR).transition_version > before

    assert world.service().apply(source_id, world.observe()).status is ApplicationStatus.ACCEPTED


# -- AS-005 -------------------------------------------------------------


@pytest.mark.parametrize("newer_ends", ["in-progress", "failed"])
def test_newer_attempt_blocks_uncommitted_proposal(world: World, newer_ends: str) -> None:
    retained, release, outcomes = threading.Event(), threading.Event(), []

    def participant_a() -> None:
        service = world.service()
        view = service.retain(_produce(world))
        retained.set()
        assert release.wait(10)
        assert view.record is not None
        outcomes.append(service.apply(view.record.source_id, world.observe()))

    thread = threading.Thread(target=participant_a)
    thread.start()
    assert retained.wait(10)
    participant_b = world.attempts()
    newer = participant_b.start(PR, world.h2)
    if newer_ends == "failed":
        participant_b.finish(newer.attempt_id, "ERROR")
    assert world.attempts().latest_sequence(PR, world.h2) == newer.sequence == 2  # persisted before A is released
    release.set()
    thread.join(10)

    assert outcomes[0].status is ApplicationStatus.REJECTED
    assert world.cycle().snapshot(PR).accepted_closure is None and len(world.cycle().snapshot(PR).open_findings) == 2


def test_committed_closure_is_not_reopened_but_gates_later_effects(world: World) -> None:
    source_id = _retain(world)
    assert world.service().apply(source_id, world.observe()).status is ApplicationStatus.ACCEPTED
    assert world.service().dependent_effects_allowed(source_id, world.observe()) == (True, "")

    world.attempts().start(PR, world.h2)  # B starts after A committed

    snapshot = world.cycle().snapshot(PR)
    assert snapshot.accepted_closure is not None and snapshot.open_findings == ()
    allowed, reason = world.service().dependent_effects_allowed(source_id, world.observe())
    assert not allowed and "newer ordinary attempt" in reason
    assert world.service().apply(source_id, world.observe()).status is ApplicationStatus.ACCEPTED  # replay stays idempotent


def test_dependent_effects_require_accepted_evidence_and_unchanged_target(world: World) -> None:
    source_id = _retain(world)
    assert world.service().dependent_effects_allowed(source_id, world.observe())[0] is False  # merely retained
    world.service().apply(source_id, world.observe())
    changed = replace(world.observe()(), head_sha="a" * 40)
    assert world.service().dependent_effects_allowed(source_id, lambda: changed)[0] is False


def test_attempt_must_be_registered_for_the_same_pr_and_head(world: World) -> None:
    result = _produce(world)
    assert result.closure_assessment is not None
    result.closure_assessment = replace(result.closure_assessment, attempt_id="made-up", attempt_sequence=9)
    result.attempt_id, result.attempt_sequence = "made-up", 9
    assert world.service().retain(result).state is EvidenceState.REJECTED

    mixed = _produce(world)
    mixed.attempt_id = "another-attempt"  # ordinary PASS from a different attempt than the assessment
    assert world.service().retain(mixed).state is EvidenceState.REJECTED


# -- AS-006 -------------------------------------------------------------


def test_crash_after_retention_recovers_without_a_new_attempt(world: World) -> None:
    source_id = _retain(world)

    outcomes = world.service().reconcile(PR, world.observe())

    assert [item.status for item in outcomes] == [ApplicationStatus.ACCEPTED]
    assert world.attempts().latest_sequence(PR, world.h2) == 1 and len(world.cycle().snapshot(PR).closures) == 1
    assert world.service().inspect(source_id).state is EvidenceState.ACCEPTED


def test_crash_between_convergence_and_certification_finishes_once(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    source_id = _retain(world)

    def crash(*args: object, **kwargs: object) -> None:
        raise SimulatedCrash

    with monkeypatch.context() as patched:
        patched.setattr(PrReviewCycleRepository, "certify_closure", crash)
        with pytest.raises(SimulatedCrash):
            world.service().apply(source_id, world.observe())
    midway = world.cycle().snapshot(PR)
    assert midway.ordinary_pass_head_sha == world.h2 and midway.closures == ()  # convergence recorded, no certificate

    assert world.service().apply(source_id, world.observe()).status is ApplicationStatus.ACCEPTED
    snapshot = world.cycle().snapshot(PR)
    assert [item.source_identity for item in snapshot.closures] == [source_id] and snapshot.open_findings == ()


def test_crash_after_certification_repairs_local_bookkeeping_only(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    source_id = _retain(world)

    def crash(*args: object, **kwargs: object) -> None:
        raise SimulatedCrash

    with monkeypatch.context() as patched:
        patched.setattr(OrdinaryClosureEvidenceRepository, "transition", crash)
        with pytest.raises(SimulatedCrash):
            world.service().apply(source_id, world.observe())
    committed = world.cycle().snapshot(PR)
    assert world.service().inspect(source_id).state is EvidenceState.RETAINED and len(committed.closures) == 1

    outcome = world.service().apply(source_id, world.observe())

    assert outcome.status is ApplicationStatus.ACCEPTED and outcome.closure_id == committed.closures[0].closure_id
    final = world.cycle().snapshot(PR)
    assert len(final.closures) == 1 and final.transition_version == committed.transition_version
    assert final.pending_effect == "CLOSURE_PUBLICATION" and final.accepted_closure is not None
    assert final.accepted_closure.publication_status == "PENDING"  # nothing was acknowledged on GitHub's behalf


def test_committed_write_with_unavailable_acknowledgement_is_reconciled(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    source_id = _retain(world)
    original = OrdinaryClosureEvidenceRepository.transition

    def write_then_fail(self: OrdinaryClosureEvidenceRepository, *args: object, **kwargs: object) -> None:
        original(self, *args, **kwargs)  # type: ignore[arg-type]
        raise OSError("acknowledgement lost")

    with monkeypatch.context() as patched:
        patched.setattr(OrdinaryClosureEvidenceRepository, "transition", write_then_fail)
        uncertain = world.service().apply(source_id, world.observe())
    assert uncertain.status is ApplicationStatus.DEFERRED and uncertain.cycle_committed

    assert world.service().apply(source_id, world.observe()).status is ApplicationStatus.ACCEPTED
    assert len(world.cycle().snapshot(PR).closures) == 1


def test_expanded_replay_after_crash_does_not_duplicate_certificate(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    source_id = _retain(world, scope="EXPANDED")

    def crash(*args: object, **kwargs: object) -> None:
        raise SimulatedCrash

    with monkeypatch.context() as patched:
        patched.setattr(OrdinaryClosureEvidenceRepository, "transition", crash)
        with pytest.raises(SimulatedCrash):
            world.service().apply(source_id, world.observe())

    assert world.service().apply(source_id, world.observe()).requires_strong_audit
    assert len(world.cycle().snapshot(PR).closures) == 1


# -- review findings: complete retention, coverage, committed-effect gates ---


def test_complete_ordinary_result_is_recoverable_after_reconstruction(world: World) -> None:
    result = _produce(world, evidence="distinctive-coverage-evidence-7731")
    view = world.service().retain(result)
    assert view.record is not None

    recovered = world.service().inspect(view.record.source_id).record  # every store reconstructed from disk
    assert recovered is not None
    payload = json.loads(recovered.ordinary.payload)
    assert [(item["requirement_id"], item["evidence"]) for item in payload["requirement_coverage"]] == [("REQ-001", "distinctive-coverage-evidence-7731"), ("REQ-002", "distinctive-coverage-evidence-7731")]
    assert payload["summary"] == "ordinary summary" and payload["raw_response"] == result.raw_response
    for name in ("thread_dispositions", "evidence_recovery", "specification_gaps", "findings", "decision_critical_evidence_gaps"):
        assert name in payload


@pytest.mark.parametrize("coverage_ids", [("REQ-001",), ("REQ-001", "REQ-002", "REQ-999"), ("REQ-001", "#9999/REQ-002")])
def test_ordinary_coverage_must_match_the_observed_requirements(world: World, coverage_ids: tuple[str, ...]) -> None:
    source_id = _retain(world, coverage_ids=coverage_ids)

    outcome = world.service().apply(source_id, world.observe())

    assert outcome.status is ApplicationStatus.NON_AUTHORIZING
    snapshot = world.cycle().snapshot(PR)
    assert snapshot.closures == () and len(snapshot.open_findings) == 2
    assert world.service().inspect(source_id).state is EvidenceState.RETAINED


def test_qualified_coverage_identities_are_accepted(world: World) -> None:
    source_id = _retain(world, coverage_ids=("#2406/REQ-001", "#2406/REQ-002"))

    assert world.service().apply(source_id, world.observe()).status is ApplicationStatus.ACCEPTED


@pytest.mark.parametrize("new_round", [False, True])
def test_committed_closure_effects_are_denied_after_newer_strong_state(world: World, new_round: bool) -> None:
    source_id = _retain(world)
    assert world.service().apply(source_id, world.observe()).status is ApplicationStatus.ACCEPTED
    assert world.service().dependent_effects_allowed(source_id, world.observe())[0] is True

    cycle = world.cycle()
    claim = cycle.claim_strong_audit(PR, RoundProvenance(world.h2, world.base), CONTRACT, POLICY)  # same target, no new ordinary attempt
    allowed, reason = world.service().dependent_effects_allowed(source_id, world.observe())
    assert not allowed and "strong audit" in reason
    if new_round:
        cycle.record_strong_result(PR, claim.claim_id, VERDICT_FINDINGS, "codex/strong", [_finding("f9", claim.claim_id)])
        allowed, reason = world.service().dependent_effects_allowed(source_id, world.observe())
        assert not allowed

    snapshot = cycle.snapshot(PR)
    assert [item.source_identity for item in snapshot.closures] == [source_id]  # historical closure intact
    assert world.service().inspect(source_id).state is EvidenceState.ACCEPTED
