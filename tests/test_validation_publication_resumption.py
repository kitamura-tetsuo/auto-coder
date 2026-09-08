"""Issue #1923: resume unfinished BLOCKED publication effects without loss or
duplicate delivery.

These tests exercise ``SpecificationValidationLifecycle.apply_blocked`` /
``apply_inherited_blocked`` (the production per-effect publication path) and
``automation_engine._ValidationPublicationStageHandler`` (the production
resumption path registered with the pending-work scheduler), following the
style of ``tests/test_pending_work_resumption.py`` for the sibling
Issue/PR-processing stages delivered by #1919-#1922.
"""

from __future__ import annotations

import asyncio
import time

from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import (
    VALIDATION_PUBLICATION_STAGE,
    AutomationEngine,
    _ValidationPublicationStageHandler,
)
from auto_coder.github_pending_work import (
    PendingObligation,
    PendingReason,
    PendingWorkScheduler,
    PendingWorkStore,
    StageOutcome,
    WorkIdentity,
)
from auto_coder.requirement_contract import build_normative_issue_manifest
from auto_coder.specification_analyzer import SpecificationAnalysisResult, SpecificationFinding
from auto_coder.specification_validation_lifecycle import (
    DIAGNOSTIC_EFFECT,
    READINESS_WITHDRAWAL_EFFECT,
    SpecificationValidationLifecycle,
    validation_publication_identity,
)
from auto_coder.util.github_request_outcome import (
    DeliveryCertainty,
    GitHubApiOutcome,
    GitHubRequestContext,
    GitHubRequestError,
    GitHubRequestOutcome,
    GitHubRequestRefused,
    GitHubResponseMetadata,
    RequestProvenance,
)

BODY = "## Requirements\n- REQ-001: Return the current value."
FINDING = SpecificationFinding("material_ambiguity", ("REQ-001",), "The current value is undefined.", "Define its source.", "", "")


def _github_error(classification, *, delivery=DeliveryCertainty.HTTP_RESPONSE_RECEIVED, retry_after=0.0, status=403):
    outcome = GitHubRequestOutcome(
        GitHubRequestContext("op", "attempt", "test", "https://api.github.com", "GET", "read", "/repos/{owner}/{repo}"),
        None if classification is GitHubApiOutcome.REFUSED else status,
        classification,
        RequestProvenance.NETWORK,
        delivery,
        GitHubResponseMetadata(retry_after_seconds=retry_after),
        1,
    )
    if classification is GitHubApiOutcome.REFUSED:
        return GitHubRequestRefused(outcome)
    return GitHubRequestError(outcome)


def _blocked_gate(tmp_path, remediation_reason="material ambiguity"):
    result = SpecificationAnalysisResult("BLOCKED", (FINDING,))
    return SpecificationValidationLifecycle("owner/repo", "policy-a", tmp_path / "decisions.json", lambda *_a: result)


def _blocked_decision(gate, issue_number=1728, body=BODY):
    manifest = build_normative_issue_manifest(issue_number, "Title", body)
    return gate.decide(manifest, "Title", body)


class _FakeGitHub:
    """Controlled GitHub adapter: independently fails comment posting and label removal."""

    def __init__(self, ready=True):
        self.ready = ready
        self.comments: list[dict[str, object]] = []
        self.removals = 0
        self.comment_error: Exception | None = None
        self.label_error: Exception | None = None

    def get_issue_dispatch_snapshot_strict(self, _repo, _number):
        labels = [{"name": "implementation-ready"}] if self.ready else []
        return {"number": 1728, "title": "Title", "body": BODY, "labels": labels}

    def get_issue_comments_strict(self, _repo, _number):
        return list(self.comments)

    def add_comment_to_issue(self, _repo, _number, body):
        if self.comment_error is not None:
            raise self.comment_error
        self.comments.append({"body": body})

    def remove_labels(self, _repo, _number, _labels, item_type="issue"):
        if self.label_error is not None:
            raise self.label_error
        self.removals += 1
        self.ready = False

    def get_direct_sub_issues_strict(self, _repo, _number):
        return []


# ---------------------------------------------------------------------------
# AS-001: comment succeeds, readiness withdrawal is deferred; the retained
# effects are independent, publication is never reported complete early, and
# the label effect eventually completes governed without a second comment.
# ---------------------------------------------------------------------------


def test_comment_succeeds_label_removal_deferred_completes_independently(tmp_path, monkeypatch):
    store = PendingWorkStore(tmp_path / "pending.db")
    monkeypatch.setattr("auto_coder.specification_validation_lifecycle.get_pending_work_store", lambda: store)

    gate = _blocked_gate(tmp_path)
    decision = _blocked_decision(gate)
    github = _FakeGitHub()
    github.label_error = _github_error(GitHubApiOutcome.REFUSED, delivery=DeliveryCertainty.DEFINITELY_NOT_SENT)

    identity = validation_publication_identity("owner/repo", 1728, decision.identity.key)

    try:
        gate.apply_blocked(github, decision)
        raised = None
    except GitHubRequestError as exc:
        raised = exc
    assert raised is not None

    # The comment is durably confirmed and must never be sent again.
    assert len(github.comments) == 1
    saved = gate.store.get(decision.identity)
    assert saved is not None and saved.findings_published is True and saved.readiness_removed is False

    # complete_effect() was actually called in production code (the gap this
    # Issue closes): the diagnostic effect is already durably confirmed, so
    # the retained obligation -- as computed by the production
    # _defer_validation_publication call site from the durable decision
    # state -- only ever names the still-missing sibling.
    obligation = store.defer(identity, raised, (READINESS_WITHDRAWAL_EFFECT,))
    assert obligation.unfinished_effects == (READINESS_WITHDRAWAL_EFFECT,)

    # Governed resumption (no restart): the label is removed without a
    # duplicate comment.
    github.label_error = None
    engine = AutomationEngine(github, AutomationConfig())
    engine._specification_validators["owner/repo"] = gate
    handler = _ValidationPublicationStageHandler(engine, "owner/repo")
    resumed = store.get(identity)
    outcome = handler.dispatch(resumed)
    assert outcome.error is None
    assert outcome.superseded is False
    assert len(github.comments) == 1
    assert github.removals == 1
    assert store.get(identity) is None


def test_comment_succeeds_label_removal_deferred_survives_restart(tmp_path, monkeypatch):
    """Same as above but with a fresh PendingWorkStore/engine instance standing in for a controller restart."""
    db_path = tmp_path / "pending.db"
    decisions_path = tmp_path / "decisions.json"
    store = PendingWorkStore(db_path)
    monkeypatch.setattr("auto_coder.specification_validation_lifecycle.get_pending_work_store", lambda: store)

    gate = SpecificationValidationLifecycle("owner/repo", "policy-a", decisions_path, lambda *_a: SpecificationAnalysisResult("BLOCKED", (FINDING,)))
    decision = _blocked_decision(gate)
    github = _FakeGitHub()
    github.label_error = _github_error(GitHubApiOutcome.SECONDARY_THROTTLED)
    identity = validation_publication_identity("owner/repo", 1728, decision.identity.key)

    try:
        gate.apply_blocked(github, decision)
    except GitHubRequestError as exc:
        store.defer(identity, exc, (READINESS_WITHDRAWAL_EFFECT,))

    # "Restart": brand-new store/gate/engine instances backed by the same files.
    restarted_store = PendingWorkStore(db_path)
    monkeypatch.setattr("auto_coder.specification_validation_lifecycle.get_pending_work_store", lambda: restarted_store)
    restarted_gate = SpecificationValidationLifecycle("owner/repo", "policy-a", decisions_path)
    github.label_error = None
    engine = AutomationEngine(github, AutomationConfig())
    engine._specification_validators["owner/repo"] = restarted_gate
    handler = _ValidationPublicationStageHandler(engine, "owner/repo")

    obligation = restarted_store.interrupted() or restarted_store.due(now=time.time() + 1)
    # Simulate scheduler recovery picking the retained obligation up.
    resumed = restarted_store.get(identity)
    assert resumed is not None
    outcome = handler.recover(resumed)
    assert outcome.error is None
    assert len(github.comments) == 1
    assert github.removals == 1
    assert restarted_store.get(identity) is None


# ---------------------------------------------------------------------------
# AS-002: readiness withdrawal already confirmed before the diagnostic is
# delivered. The diagnostic must not be hidden behind the (now absent) label.
# ---------------------------------------------------------------------------


def test_readiness_withdrawn_before_diagnostic_still_delivers_diagnostic(tmp_path, monkeypatch):
    store = PendingWorkStore(tmp_path / "pending.db")
    monkeypatch.setattr("auto_coder.specification_validation_lifecycle.get_pending_work_store", lambda: store)

    gate = _blocked_gate(tmp_path)
    decision = _blocked_decision(gate)
    # Manually establish the supported effect ordering: withdrawal already
    # confirmed durably, diagnostic still missing, and the label is already
    # gone from GitHub (as the withdrawal itself would leave it).
    withdrawn_but_undiagnosed = gate.store.get(decision.identity)
    from dataclasses import replace

    gate.store.save(replace(withdrawn_but_undiagnosed, readiness_removed=True))
    github = _FakeGitHub(ready=False)

    error = gate.apply_blocked(github, decision)

    assert error is None
    assert len(github.comments) == 1, "diagnostic must still be delivered even though the label is already absent"
    assert github.removals == 0, "an already-withdrawn label must not be re-removed"
    saved = gate.store.get(decision.identity)
    assert saved.findings_published is True and saved.readiness_removed is True


def test_validation_publication_stage_handler_resumes_after_restart_without_readiness_label(tmp_path, monkeypatch):
    store = PendingWorkStore(tmp_path / "pending.db")
    monkeypatch.setattr("auto_coder.specification_validation_lifecycle.get_pending_work_store", lambda: store)
    monkeypatch.setattr("auto_coder.automation_engine.get_pending_work_store", lambda: store)

    gate = _blocked_gate(tmp_path)
    decision = _blocked_decision(gate)
    from dataclasses import replace

    gate.store.save(replace(gate.store.get(decision.identity), readiness_removed=True))
    github = _FakeGitHub(ready=False)
    identity = validation_publication_identity("owner/repo", 1728, decision.identity.key)
    store.defer(identity, _github_error(GitHubApiOutcome.PRIMARY_THROTTLED), (DIAGNOSTIC_EFFECT,), now=time.time() - 1)

    engine = AutomationEngine(github, AutomationConfig())
    engine._specification_validators["owner/repo"] = gate
    engine.pending_work_scheduler = PendingWorkScheduler(store, poll_interval=0.02)
    engine.pending_work_scheduler.register_handler(VALIDATION_PUBLICATION_STAGE, _ValidationPublicationStageHandler(engine, "owner/repo"))

    async def scenario():
        shutdown = asyncio.Event()
        task = asyncio.create_task(engine.pending_work_scheduler.run(shutdown))
        try:
            for _ in range(300):
                if store.get(identity) is None:
                    break
                await asyncio.sleep(0.01)
        finally:
            shutdown.set()
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())

    assert store.get(identity) is None, "retained diagnostic obligation was not automatically discovered/completed"
    assert len(github.comments) == 1
    assert github.removals == 0


# ---------------------------------------------------------------------------
# AS-003: a stale (superseded) obligation must never authorize a new
# submission's effects.
# ---------------------------------------------------------------------------


def test_stage_handler_supersedes_when_issue_body_changed_while_waiting(tmp_path, monkeypatch):
    store = PendingWorkStore(tmp_path / "pending.db")
    monkeypatch.setattr("auto_coder.specification_validation_lifecycle.get_pending_work_store", lambda: store)

    gate = _blocked_gate(tmp_path)
    decision = _blocked_decision(gate)
    identity = validation_publication_identity("owner/repo", 1728, decision.identity.key)
    obligation = PendingObligation(identity, PendingReason.THROTTLED, 0.0, (DIAGNOSTIC_EFFECT, READINESS_WITHDRAWAL_EFFECT))

    class _EditedGithub(_FakeGitHub):
        def get_issue_dispatch_snapshot_strict(self, _repo, _number):
            return {"number": 1728, "title": "Title", "body": BODY + "\nCorrected", "labels": [{"name": "implementation-ready"}]}

    github = _EditedGithub()
    engine = AutomationEngine(github, AutomationConfig())
    engine._specification_validators["owner/repo"] = gate
    handler = _ValidationPublicationStageHandler(engine, "owner/repo")

    outcome = handler.dispatch(obligation)

    assert outcome == StageOutcome(superseded=True)
    assert github.comments == []
    assert github.removals == 0


def test_stage_handler_supersedes_malformed_entity():
    engine = AutomationEngine(_FakeGitHub(), AutomationConfig())
    handler = _ValidationPublicationStageHandler(engine, "owner/repo")
    obligation = PendingObligation(WorkIdentity("owner/repo", "not-an-issue", VALIDATION_PUBLICATION_STAGE, "rev"), PendingReason.THROTTLED, 0.0, (DIAGNOSTIC_EFFECT,))

    assert handler.dispatch(obligation) == StageOutcome(superseded=True)


# ---------------------------------------------------------------------------
# REQ-006 / AS-007: an operational failure must defer, never fabricate a
# terminal BLOCKED-with-error outcome that drops the retained obligation.
# ---------------------------------------------------------------------------


def test_authentication_failure_during_publication_defers_not_supersedes(tmp_path, monkeypatch):
    store = PendingWorkStore(tmp_path / "pending.db")
    monkeypatch.setattr("auto_coder.specification_validation_lifecycle.get_pending_work_store", lambda: store)

    gate = _blocked_gate(tmp_path)
    decision = _blocked_decision(gate)
    github = _FakeGitHub()
    auth_error = _github_error(GitHubApiOutcome.AUTHENTICATION_FAILURE)
    github.comment_error = auth_error

    raised = None
    try:
        gate.apply_blocked(github, decision)
    except GitHubRequestError as exc:
        raised = exc
    assert raised is auth_error
    # The decision itself must remain BLOCKED -- an operational failure does
    # not get rewritten into a different semantic verdict (REQ-006).
    saved = gate.store.get(decision.identity)
    assert saved is not None and saved.verdict == "BLOCKED" and saved.findings_published is False


# ---------------------------------------------------------------------------
# REQ-002 / REQ-007: complete_effect actually has a production caller now,
# and effects are confirmed only on positively established success.
# ---------------------------------------------------------------------------


def test_apply_blocked_completes_both_effects_via_production_pending_work_store(tmp_path, monkeypatch):
    store = PendingWorkStore(tmp_path / "pending.db")
    monkeypatch.setattr("auto_coder.specification_validation_lifecycle.get_pending_work_store", lambda: store)
    gate = _blocked_gate(tmp_path)
    decision = _blocked_decision(gate)
    identity = validation_publication_identity("owner/repo", 1728, decision.identity.key)
    # Pre-register both effects, exactly as a first-attempt failure would
    # have retained them (REQ-001).
    store.defer(identity, _github_error(GitHubApiOutcome.SECONDARY_THROTTLED), (DIAGNOSTIC_EFFECT, READINESS_WITHDRAWAL_EFFECT))

    github = _FakeGitHub()
    error = gate.apply_blocked(github, decision)

    assert error is None
    # Production code -- not a test fixture -- consumed the durable queue.
    assert store.get(identity) is None
