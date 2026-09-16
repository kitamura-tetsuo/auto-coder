"""Issue #2026 (REQ-007): resume unfinished decomposition BLOCKED publication
effects without loss or duplicate delivery.

Before this change ``DecompositionValidationLifecycle.apply_blocked`` did not
participate in the durable pending-work store at all (unlike the
specification standalone/inherited-child routes, Issue #1923): a
``GitHubRequestError`` while posting the findings comment or removing the
parent's readiness label was only ever recorded in the returned failure
string, with no registered stage handler that could ever resume it. These
tests exercise the real ``DecompositionValidationLifecycle.apply_blocked``
and the new ``automation_engine._DecompositionPublicationStageHandler``
registered on ``DECOMPOSITION_PUBLICATION_STAGE``, following the style of
``tests/test_validation_publication_resumption.py``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import (
    AutomationEngine,
    _DecompositionPublicationStageHandler,
)
from auto_coder.decomposition_analyzer import DecompositionAnalysisResult
from auto_coder.decomposition_validation_lifecycle import (
    DECOMPOSITION_PUBLICATION_STAGE,
    DecompositionValidationLifecycle,
    decomposition_publication_identity,
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
from auto_coder.specification_validation_lifecycle import DIAGNOSTIC_EFFECT, READINESS_WITHDRAWAL_EFFECT
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

PARENT_BODY = "## Objective\nCoordinate the tracked child behaviors."
CHILD_BODY = "## Requirements\n- REQ-001: Deliver the first behavior."
REVIEWER_LOGIN = "auto-coder-reviewer[bot]"
REVIEWER_APP_ID = 990001


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


def _parent(number=10, title="Parent", body=PARENT_BODY, ready=True, state="open"):
    return {"id": number, "number": number, "title": title, "body": body, "state": state, "labels": [{"name": "implementation-ready"}] if ready else []}


def _child(number=11, title="Child", body=CHILD_BODY):
    return {"id": number, "number": number, "title": title, "body": body, "state": "open", "labels": []}


class _FakeDecompositionGithub:
    """Controlled adapter: independently fails comment posting and label removal."""

    def __init__(self, parent, children):
        self.parent = dict(parent)
        self.children = [dict(c) for c in children]
        self.comments: list[dict] = []
        self.removals: list[tuple[str, int]] = []
        self.comment_error: Exception | None = None
        self.label_error: Exception | None = None

    def get_issue_dispatch_snapshot_strict(self, _repo, number):
        if number == self.parent["number"]:
            return dict(self.parent)
        for child in self.children:
            if child["number"] == number:
                return dict(child)
        raise ValueError(f"unknown issue {number}")

    def get_direct_sub_issues_strict(self, _repo, number):
        if number == self.parent["number"]:
            return [dict(c) for c in self.children]
        return []

    def get_parent_issue_details_strict(self, _repo, _number):
        return None

    def get_issue_comments_strict(self, _repo, _number):
        return list(self.comments)

    def publish_issue_review_comment(self, _repo, number, body, authorize_fn):
        from auto_coder.issue_review_publication import PublicationReceipt

        if self.comment_error is not None:
            raise self.comment_error
        comment_id = len(self.comments) + 1
        self.comments.append({"id": comment_id, "number": number, "body": body, "user": {"login": REVIEWER_LOGIN}, "performed_via_github_app": {"id": REVIEWER_APP_ID}})
        return PublicationReceipt(comment_id, REVIEWER_LOGIN, REVIEWER_APP_ID)

    def reviewer_app_identity(self, _repo):
        from auto_coder.github_app_reviewer import ReviewerAppIdentity

        return ReviewerAppIdentity(login=REVIEWER_LOGIN, app_id=REVIEWER_APP_ID)

    def remove_labels(self, repo, number, _labels, item_type="issue"):
        if self.label_error is not None:
            raise self.label_error
        self.removals.append((repo, number))
        self.parent["labels"] = []


def _blocked_gate(tmp_path):
    result = DecompositionAnalysisResult("BLOCKED", remediation="EDIT_IN_PLACE")
    return DecompositionValidationLifecycle("owner/repo", "policy-a", tmp_path / "sets.json", lambda *_a: result)


def _decomposition_inputs(parent, children):
    from auto_coder.decomposition_analyzer import DecompositionIssue

    def adapt(item):
        return DecompositionIssue(build_normative_issue_manifest(item["number"], item["title"], item["body"]), item["body"])

    return adapt(parent), [adapt(child) for child in children]


def _blocked_decision(gate, parent, children):
    identity = gate.identity(parent, children)
    parent_input, child_inputs = _decomposition_inputs(parent, children)
    return gate.decide(identity, parent_input, child_inputs)


def test_comment_succeeds_label_removal_deferred_completes_independently(tmp_path, monkeypatch):
    store = PendingWorkStore(tmp_path / "pending.db")
    monkeypatch.setattr("auto_coder.decomposition_validation_lifecycle.get_pending_work_store", lambda: store)

    gate = _blocked_gate(tmp_path)
    parent, children = _parent(), [_child()]
    decision = _blocked_decision(gate, parent, children)
    github = _FakeDecompositionGithub(parent, children)
    github.label_error = _github_error(GitHubApiOutcome.REFUSED, delivery=DeliveryCertainty.DEFINITELY_NOT_SENT)

    error = gate.apply_blocked(github, decision, lambda _n: (github.parent, github.children))

    assert error is not None and "readiness withdrawal failed" in error
    assert len(github.comments) == 1
    assert github.removals == []
    saved = gate.store.get(decision.identity)
    assert saved.findings_published is True
    assert saved.readiness_removed is False

    identity = decomposition_publication_identity("owner/repo", 10, decision.identity.key)
    obligation = store.get(identity)
    assert obligation is not None
    assert obligation.unfinished_effects == (READINESS_WITHDRAWAL_EFFECT,)

    # The retained obligation resolves independently once the label API
    # recovers, without ever re-posting the already-confirmed comment.
    github.label_error = None
    error2 = gate.apply_blocked(github, decision, lambda _n: (github.parent, github.children))
    assert error2 is None
    assert len(github.comments) == 1, "already-confirmed diagnostic must never be reposted"
    assert github.removals == [("owner/repo", 10)]
    assert store.get(identity) is None


def test_decomposition_publication_stage_handler_resumes_after_restart(tmp_path, monkeypatch):
    store = PendingWorkStore(tmp_path / "pending.db")
    monkeypatch.setattr("auto_coder.decomposition_validation_lifecycle.get_pending_work_store", lambda: store)
    monkeypatch.setattr("auto_coder.automation_engine.get_pending_work_store", lambda: store)

    gate = _blocked_gate(tmp_path)
    parent, children = _parent(), [_child()]
    decision = _blocked_decision(gate, parent, children)
    # Manually establish the supported effect ordering: readiness withdrawal
    # already confirmed durably, diagnostic still missing, and the label is
    # already gone from GitHub (as the withdrawal itself would leave it).
    gate.store.save(replace(gate.store.get(decision.identity), readiness_removed=True))
    parent["labels"] = []
    github = _FakeDecompositionGithub(parent, children)

    identity = decomposition_publication_identity("owner/repo", 10, decision.identity.key)
    store.defer(identity, _github_error(GitHubApiOutcome.PRIMARY_THROTTLED), (DIAGNOSTIC_EFFECT,), now=time.time() - 1)

    engine = AutomationEngine(github, AutomationConfig())
    engine._decomposition_validators["owner/repo"] = gate
    engine.pending_work_scheduler = PendingWorkScheduler(store, poll_interval=0.02)
    engine.pending_work_scheduler.register_handler(DECOMPOSITION_PUBLICATION_STAGE, _DecompositionPublicationStageHandler(engine, "owner/repo"))

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
    assert github.removals == []


def test_stage_handler_supersedes_when_parent_body_changed_while_waiting(tmp_path, monkeypatch):
    store = PendingWorkStore(tmp_path / "pending.db")
    monkeypatch.setattr("auto_coder.decomposition_validation_lifecycle.get_pending_work_store", lambda: store)

    gate = _blocked_gate(tmp_path)
    parent, children = _parent(), [_child()]
    decision = _blocked_decision(gate, parent, children)
    identity = decomposition_publication_identity("owner/repo", 10, decision.identity.key)
    obligation = PendingObligation(identity, PendingReason.THROTTLED, 0.0, (DIAGNOSTIC_EFFECT, READINESS_WITHDRAWAL_EFFECT))

    edited_parent = {**parent, "body": PARENT_BODY + "\nCorrected"}
    github = _FakeDecompositionGithub(edited_parent, children)
    engine = AutomationEngine(github, AutomationConfig())
    engine._decomposition_validators["owner/repo"] = gate
    handler = _DecompositionPublicationStageHandler(engine, "owner/repo")

    outcome = handler.dispatch(obligation)

    assert outcome == StageOutcome(superseded=True)
    assert github.comments == []
    assert github.removals == []


def test_stage_handler_supersedes_malformed_entity():
    engine = AutomationEngine(_FakeDecompositionGithub(_parent(), [_child()]), AutomationConfig())
    handler = _DecompositionPublicationStageHandler(engine, "owner/repo")
    obligation = PendingObligation(WorkIdentity("owner/repo", "not-an-issue", DECOMPOSITION_PUBLICATION_STAGE, "rev"), PendingReason.THROTTLED, 0.0, (DIAGNOSTIC_EFFECT,))

    assert handler.dispatch(obligation) == StageOutcome(superseded=True)


def test_authentication_failure_during_publication_defers_not_supersedes(tmp_path, monkeypatch):
    store = PendingWorkStore(tmp_path / "pending.db")
    monkeypatch.setattr("auto_coder.decomposition_validation_lifecycle.get_pending_work_store", lambda: store)

    gate = _blocked_gate(tmp_path)
    parent, children = _parent(), [_child()]
    decision = _blocked_decision(gate, parent, children)
    github = _FakeDecompositionGithub(parent, children)
    auth_error = _github_error(GitHubApiOutcome.AUTHENTICATION_FAILURE)
    github.comment_error = auth_error

    error = gate.apply_blocked(github, decision, lambda _n: (github.parent, github.children))

    assert error is not None and "findings publication failed" in error
    identity = decomposition_publication_identity("owner/repo", 10, decision.identity.key)
    obligation = store.get(identity)
    assert obligation is not None
    assert obligation.reason == PendingReason.AUTHENTICATION
    assert obligation.unfinished_effects == (DIAGNOSTIC_EFFECT,)
