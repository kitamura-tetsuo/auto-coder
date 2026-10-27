"""Controlled GitHub integration regressions for replay-safe closure replies.

The transport retains remote effects even when their responses are lost. The
production readers, classification, ledger, resolver and mutation confirmations
remain in the causal path; no live GitHub or model execution is claimed.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event
from typing import Callable

import httpx
import pytest

from auto_coder.adversarial_validator import ReviewThreadDisposition
from auto_coder.canonical_pr_blocker_ledger import BlockerAdmissionPayload, BlockerAlias, CanonicalPRBlockerLedger, CorrectionScope, QualifiedRequirement
from auto_coder.review_feedback_marker import REVIEW_ADDRESSED_MARKER
from auto_coder.review_thread_closure_receipt import ClosureReceiptError, closure_receipt_lock, observe_closure_receipt, put_receipt
from auto_coder.review_thread_validation import (
    STALE_BLOCKER_CLEARED_MARKER,
    STALE_BLOCKER_MARKER,
    StaleReviewThreadRegistry,
    StaleReviewThreadResolutionError,
    classify_review_threads,
    reopen_review_threads_after_publication_failure,
    resolve_addressed_review_threads,
    retry_pending_stale_review_thread_rollbacks,
)
from auto_coder.util import gh_cache
from auto_coder.util.gh_cache import GitHubClient

API_ORIGIN = "https://api.github.com"
REPOSITORY = "owner/repo"
PR_NUMBER = 5746
THREAD_ID = "PRRT_closure_receipt"
ROOT_ID = 411
REVIEWER_ID = 601
CONTROLLER_LOGIN = "auto-coder-controller[bot]"
HEAD = "1" * 40
NEXT_HEAD = "2" * 40
BASE = "3" * 40


@dataclass
class RemoteComment:
    database_id: int = 0
    body: str = ""
    login: str = CONTROLLER_LOGIN
    author_id: int = 701
    reply_to: int | None = ROOT_ID
    updated: int = 0

    def graphql_node(self):
        return {
            "databaseId": self.database_id,
            "body": self.body,
            "author": {"__typename": "Bot", "login": self.login, "databaseId": self.author_id},
            "createdAt": "2026-10-23T03:00:00Z",
            "updatedAt": f"2026-10-23T03:00:{self.updated:02d}Z",
            "replyTo": {"databaseId": self.reply_to} if self.reply_to is not None else None,
        }


def _initial_comments():
    return [
        RemoteComment(ROOT_ID, "REQ-001: The retry counter in src/retry.py does not reset.", "auto-coder-reviewer[bot]", REVIEWER_ID, None),
        RemoteComment(ROOT_ID + 1, f"Reset the retry counter in src/retry.py.\n{REVIEW_ADDRESSED_MARKER}", "implementation-agent[bot]", 702),
    ]


@dataclass
class ReviewThreadTransport:
    """A persistent external boundary, independent of controller reconstruction."""

    comments: list[RemoteComment] = field(default_factory=_initial_comments)
    head: str = HEAD
    resolved: bool = False
    posts: list[str] = field(default_factory=list)
    patches: list[tuple[int, str]] = field(default_factory=list)
    effects: list[str] = field(default_factory=list)
    page_reads: list[int] = field(default_factory=list)
    lose_posts: int = 0
    lose_patches: int = 0
    lose_resolves: int = 0
    reject_patches: bool = False
    reject_unresolves: bool = False
    unresolve_attempts: int = 0
    fail_thread_reads: bool = False
    missing_thread: bool = False
    duplicate_thread: bool = False
    missing_page_cursor: bool = False
    page_size: int = 100
    move_head_after_resolve: bool = False
    before_create: Callable[[], None] | None = None
    before_resolve: Callable[[], None] | None = None
    before_unresolve: Callable[[], None] | None = None

    @property
    def users(self):
        return self

    @property
    def pulls(self):
        return self

    def get_authenticated(self):
        return {"login": CONTROLLER_LOGIN, "id": 701}

    def append_comment(self, body, *, login=CONTROLLER_LOGIN, author_id=701):
        comment = RemoteComment(max(item.database_id for item in self.comments) + 1, body, login, author_id)
        self.comments.append(comment)
        return comment

    def create_reply_for_review_comment(self, owner, repo, number, root, *, body):
        assert (owner, repo, number, root) == ("owner", "repo", PR_NUMBER, ROOT_ID)
        if self.before_create is not None:
            self.before_create()
        self.posts.append(body)
        self.effects.append("reply")
        comment = self.append_comment(body)
        if self.lose_posts:
            self.lose_posts -= 1
            raise RuntimeError("Reply accepted; response lost")
        return {"id": comment.database_id, "body": body}

    def update_review_comment(self, owner, repo, comment_id, *, body):
        assert (owner, repo) == ("owner", "repo")
        self.patches.append((comment_id, body))
        if self.reject_patches:
            raise RuntimeError("Comment update refused before send")
        target = next(comment for comment in self.comments if comment.database_id == comment_id)
        assert target.login == CONTROLLER_LOGIN
        target.body = body
        target.updated += 1
        self.effects.append("patch")
        if self.lose_patches:
            self.lose_patches -= 1
            raise RuntimeError("Comment update accepted; response lost")
        return {"id": target.database_id, "body": body}

    def strict_request(self, method, url, **_kwargs):
        assert method == "GET"
        assert url == f"{API_ORIGIN}/repos/{REPOSITORY}/pulls/{PR_NUMBER}"
        return self._response(url, {"number": PR_NUMBER, "head": {"sha": self.head}, "base": {"sha": BASE}})

    @staticmethod
    def _response(url, data):
        return httpx.Response(200, json=data, request=httpx.Request("POST", url))

    def _comment_page(self, offset):
        end = min(len(self.comments), offset + self.page_size)
        self.page_reads.append(offset)
        more = end < len(self.comments)
        return {
            "nodes": [comment.graphql_node() for comment in self.comments[offset:end]],
            "pageInfo": {"hasNextPage": more, "endCursor": str(end) if more and not self.missing_page_cursor else None},
        }

    def caching_request(self, _client, method, url, **kwargs):
        assert method == "POST"
        payload = kwargs["json"]
        query = payload["query"]
        variables = payload.get("variables", {})
        if "unresolveReviewThread" in query:
            assert variables["threadId"] == THREAD_ID
            self.unresolve_attempts += 1
            if self.before_unresolve is not None:
                self.before_unresolve()
            if self.reject_unresolves:
                raise RuntimeError("Unresolve refused before send")
            self.effects.append("unresolve")
            self.resolved = False
            data = {"unresolveReviewThread": {"thread": {"id": THREAD_ID, "isResolved": False}}}
        elif "resolveReviewThread" in query:
            assert variables["threadId"] == THREAD_ID
            if self.before_resolve is not None:
                self.before_resolve()
            self.effects.append("resolve")
            self.resolved = True
            if self.move_head_after_resolve:
                self.head = NEXT_HEAD
                self.move_head_after_resolve = False
            if self.lose_resolves:
                self.lose_resolves -= 1
                raise RuntimeError("Resolve accepted; response lost")
            data = {"resolveReviewThread": {"thread": {"id": THREAD_ID, "isResolved": True}}}
        else:
            if self.fail_thread_reads:
                raise RuntimeError("Thread observation unavailable")
            if "node(id: $thread)" in query:
                assert variables["thread"] == THREAD_ID
                data = {"node": {"comments": self._comment_page(int(variables["cursor"]))}}
            else:
                assert variables["number"] == PR_NUMBER
                node = {"id": THREAD_ID, "isResolved": self.resolved, "isOutdated": False, "comments": self._comment_page(0)}
                nodes = [] if self.missing_thread else [node, node] if self.duplicate_thread else [node]
                data = {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        return self._response(url, {"data": data})


@dataclass
class ReceiptScenario:
    transport: ReviewThreadTransport
    path: Path
    blocker_id: str
    next_attempt: int = 0

    def client(self):
        return GitHubClient("controlled-token")

    def ledger(self):
        return CanonicalPRBlockerLedger(db_path=self.path / "ledger.db")

    def registry(self):
        return StaleReviewThreadRegistry(path=self.path / "rollback.json")

    def claimed(self, client=None):
        client = client or self.client()
        snapshot = self.ledger().get_snapshot(API_ORIGIN, REPOSITORY, PR_NUMBER, require_retained_state=True)
        classification = classify_review_threads(
            client.get_pr_review_threads_strict(REPOSITORY, PR_NUMBER),
            {REVIEWER_ID},
            snapshot=snapshot,
            authenticated_controller_login=client.get_authenticated_user_login(),
        )
        assert classification.blocking_unresolved_count == 0
        return classification.claimed

    def close(self, *, status="ADDRESSED", rationale="The reset in src/retry.py now runs before every retry.", claimed=None, client=None, registry=None):
        self.next_attempt += 1
        client = client or self.client()
        selected = self.claimed(client) if claimed is None else claimed
        return resolve_addressed_review_threads(
            client,
            REPOSITORY,
            PR_NUMBER,
            self.transport.head,
            selected,
            [ReviewThreadDisposition(thread_id=THREAD_ID, status=status, rationale=rationale, evidence="src/retry.py resets the counter before entering the retry loop; tests/test_retry.py exercises this boundary.")],
            stale_registry=registry or self.registry(),
            ledger=self.ledger(),
            base_sha=BASE,
            requirement_manifest_revision="contract-v1",
            review_attempt_id=f"attempt-{self.next_attempt}",
        )


@pytest.fixture
def receipt_scenario(monkeypatch, tmp_path):
    transport = ReviewThreadTransport()
    monkeypatch.setattr(gh_cache, "get_ghapi_client", lambda _token: transport)
    monkeypatch.setattr(gh_cache, "get_caching_client", lambda: transport)
    monkeypatch.setattr(gh_cache, "_caching_request", transport.caching_request)
    monkeypatch.setattr(gh_cache, "_strict_request", transport.strict_request)
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(tmp_path / "runtime"))
    ledger = CanonicalPRBlockerLedger(db_path=tmp_path / "ledger.db")
    snapshot = ledger.initialize_namespace(API_ORIGIN, REPOSITORY, PR_NUMBER)
    blocker, _snapshot = ledger.admit_blocker(
        API_ORIGIN,
        REPOSITORY,
        PR_NUMBER,
        operation_id="admit-retry-counter",
        expected_ledger_revision=snapshot.ledger_revision,
        payload=BlockerAdmissionPayload(
            category="IMPLEMENTATION",
            qualified_requirements=(QualifiedRequirement(42, "REQ-001"),),
            authoritative_boundary="src/retry.py",
            incorrect_behavior_or_missing_invariant="The retry counter does not reset",
            required_correction_outcome="Reset the retry counter before each retry",
            evidence_needed="Production retry execution shows the counter reset",
            accepted_scope=CorrectionScope(description="Retry counter reset in src/retry.py"),
            aliases=(BlockerAlias(alias_type="github_root_comment", alias_value=str(ROOT_ID)), BlockerAlias(alias_type="github_thread", alias_value=THREAD_ID)),
            reviewed_head_sha=HEAD,
            observation_identity="retry-counter-finding",
        ),
        review_observation_identity="retry-counter-finding",
    )
    return ReceiptScenario(transport, tmp_path, blocker)


def test_same_head_reopen_and_reconstruction_reuse_one_reply(receipt_scenario):
    scenario = receipt_scenario
    claimed = scenario.claimed()
    first = scenario.close(claimed=claimed)
    assert first.resolved_thread_ids == (THREAD_ID,)
    assert scenario.transport.resolved is True

    reopened = reopen_review_threads_after_publication_failure(scenario.client(), REPOSITORY, PR_NUMBER, claimed, first.resolved_thread_ids, registry=scenario.registry())
    assert reopened == [THREAD_ID]
    assert scenario.transport.resolved is False

    second = scenario.close(rationale="A second independent evaluation confirms src/retry.py resets the counter.")
    assert second.resolved_thread_ids == (THREAD_ID,)
    assert scenario.transport.resolved is True
    assert scenario.transport.effects.count("resolve") == 2
    assert scenario.transport.effects.count("unresolve") == 1
    assert len(scenario.transport.posts) == 1
    assert scenario.transport.patches
    assert len({comment_id for comment_id, _body in scenario.transport.patches}) == 1


def test_confirmed_replay_does_not_mutate_github_again(receipt_scenario):
    scenario = receipt_scenario
    claimed = scenario.claimed()
    assert scenario.close(claimed=claimed).resolved_thread_ids == (THREAD_ID,)
    previous_effects = list(scenario.transport.effects)

    replay = scenario.close(claimed=claimed, rationale="Different wording for the same verified src/retry.py correction.")

    assert replay.resolved_thread_ids == (THREAD_ID,)
    assert scenario.transport.effects == previous_effects
    assert len(scenario.transport.posts) == 1


@pytest.mark.parametrize("lost_response", ["lose_posts", "lose_patches", "lose_resolves"])
def test_accepted_lost_response_reconciles_after_reconstruction(receipt_scenario, lost_response):
    scenario = receipt_scenario
    claimed = scenario.claimed()
    setattr(scenario.transport, lost_response, 1)
    first = scenario.close(claimed=claimed)
    if not first.resolved_thread_ids:
        assert first.unfinished_outcomes

    # Each invocation reconstructs the client, ledger and rollback registry.
    resumed = scenario.close(claimed=claimed)

    assert resumed.resolved_thread_ids == (THREAD_ID,)
    assert scenario.transport.resolved is True
    assert len(scenario.transport.posts) == 1
    assert len({comment_id for comment_id, _body in scenario.transport.patches}) <= 1


def test_pending_update_refusal_prevents_another_resolve(receipt_scenario):
    scenario = receipt_scenario
    claimed = scenario.claimed()
    first = scenario.close(claimed=claimed)
    reopen_review_threads_after_publication_failure(scenario.client(), REPOSITORY, PR_NUMBER, claimed, first.resolved_thread_ids, registry=scenario.registry())
    scenario.transport.reject_patches = True
    resolves_before = scenario.transport.effects.count("resolve")

    blocked = scenario.close(claimed=claimed)

    assert blocked.resolved_thread_ids == ()
    assert blocked.unfinished_outcomes
    assert scenario.transport.resolved is False
    assert scenario.transport.effects.count("resolve") == resolves_before
    assert len(scenario.transport.posts) == 1

    scenario.transport.reject_patches = False
    assert scenario.close(claimed=claimed).resolved_thread_ids == (THREAD_ID,)
    assert len(scenario.transport.posts) == 1


def test_same_head_close_waits_for_durable_rollback_cleanup(receipt_scenario):
    scenario = receipt_scenario
    claimed = scenario.claimed()
    first = scenario.close(claimed=claimed)
    assert first.resolved_thread_ids == (THREAD_ID,)
    prior = observe_closure_receipt(scenario.client(), REPOSITORY, PR_NUMBER, THREAD_ID, ROOT_ID).receipt
    assert prior is not None
    scenario.transport.reject_patches = True

    reopened = reopen_review_threads_after_publication_failure(scenario.client(), REPOSITORY, PR_NUMBER, claimed, first.resolved_thread_ids, registry=scenario.registry())

    assert reopened == [THREAD_ID]
    assert scenario.transport.resolved is False
    assert scenario.transport.effects.count("unresolve") == 1
    assert scenario.registry().marker_cleanup_pending_for_pr(REPOSITORY, PR_NUMBER) == {THREAD_ID: ROOT_ID}
    assert scenario.registry().receipt_operation_keys_for_pr(REPOSITORY, PR_NUMBER) == {THREAD_ID: prior.operation_key}
    scenario.transport.reject_patches = False
    effects_before = list(scenario.transport.effects)

    blocked = scenario.close(claimed=claimed)

    assert blocked.resolved_thread_ids == ()
    assert blocked.unfinished_outcomes
    assert scenario.transport.resolved is False
    assert scenario.transport.effects == effects_before
    assert scenario.registry().marker_cleanup_pending_for_pr(REPOSITORY, PR_NUMBER) == {THREAD_ID: ROOT_ID}

    assert retry_pending_stale_review_thread_rollbacks(scenario.client(), REPOSITORY, PR_NUMBER, registry=scenario.registry()) == []
    assert scenario.registry().marker_cleanup_pending_for_pr(REPOSITORY, PR_NUMBER) == {}
    assert scenario.registry().receipt_operation_keys_for_pr(REPOSITORY, PR_NUMBER) == {}
    assert scenario.transport.effects == effects_before + ["patch"]

    resumed = scenario.close(claimed=claimed)

    assert resumed.resolved_thread_ids == (THREAD_ID,)
    assert scenario.transport.resolved is True
    assert scenario.transport.effects.count("resolve") == 2
    assert scenario.transport.effects.count("unresolve") == 1
    assert len(scenario.transport.posts) == 1


@pytest.mark.parametrize("recovery_state", ["same_head", "changed_head", "already_unresolved"])
def test_failed_publication_rollback_survives_local_storage_loss(receipt_scenario, monkeypatch, recovery_state):
    scenario = receipt_scenario
    claimed = scenario.claimed()
    first = scenario.close(claimed=claimed)
    assert first.resolved_thread_ids == (THREAD_ID,)
    registry = scenario.registry()

    def reject_local_write(*_args, **_kwargs):
        raise OSError("Rollback registry storage unavailable")

    monkeypatch.setattr(registry, "record_rollback_transition", reject_local_write)
    monkeypatch.setattr(registry, "record", reject_local_write)
    scenario.transport.reject_unresolves = True

    reopened = reopen_review_threads_after_publication_failure(scenario.client(), REPOSITORY, PR_NUMBER, claimed, first.resolved_thread_ids, registry=registry)

    assert reopened == []
    assert scenario.transport.resolved is True
    assert scenario.transport.unresolve_attempts > 0
    assert scenario.registry().pending_for_pr(REPOSITORY, PR_NUMBER) == []
    assert scenario.registry().rollback_transitions_for_pr(REPOSITORY, PR_NUMBER) == {}
    obligation = observe_closure_receipt(scenario.client(), REPOSITORY, PR_NUMBER, THREAD_ID, ROOT_ID).receipt
    assert obligation is not None
    assert obligation.phase == "ROLLBACK_REQUIRED"
    effects_before_replay = list(scenario.transport.effects)

    replay = scenario.close(claimed=claimed)

    assert replay.resolved_thread_ids == ()
    assert replay.unfinished_outcomes
    assert scenario.transport.effects == effects_before_replay
    assert observe_closure_receipt(scenario.client(), REPOSITORY, PR_NUMBER, THREAD_ID, ROOT_ID).receipt == obligation
    if recovery_state == "changed_head":
        scenario.transport.head = NEXT_HEAD
    fresh_registry = StaleReviewThreadRegistry(path=scenario.path / "restart-without-local-rollback.json")

    pending = retry_pending_stale_review_thread_rollbacks(scenario.client(), REPOSITORY, PR_NUMBER, registry=fresh_registry)

    assert pending == [THREAD_ID]
    assert scenario.transport.resolved is True
    assert len(scenario.transport.posts) == 1
    assert observe_closure_receipt(scenario.client(), REPOSITORY, PR_NUMBER, THREAD_ID, ROOT_ID).receipt == obligation
    scenario.transport.reject_unresolves = False
    if recovery_state == "already_unresolved":
        scenario.client().unresolve_review_thread(THREAD_ID)
    attempts_before_recovery = scenario.transport.unresolve_attempts
    assert retry_pending_stale_review_thread_rollbacks(scenario.client(), REPOSITORY, PR_NUMBER, registry=fresh_registry) == []
    assert scenario.transport.resolved is False
    assert scenario.transport.unresolve_attempts == attempts_before_recovery + (0 if recovery_state == "already_unresolved" else 1)
    assert scenario.transport.effects.count("unresolve") == 1
    assert len(scenario.transport.posts) == 1
    assert "ROLLED_BACK" in next(comment.body for comment in scenario.transport.comments if comment.login == CONTROLLER_LOGIN)


def test_rejected_rollback_receipt_write_preserves_local_recovery(receipt_scenario):
    scenario = receipt_scenario
    claimed = scenario.claimed()
    first = scenario.close(claimed=claimed)
    assert first.resolved_thread_ids == (THREAD_ID,)
    scenario.transport.reject_patches = True
    scenario.transport.reject_unresolves = True

    reopened = reopen_review_threads_after_publication_failure(scenario.client(), REPOSITORY, PR_NUMBER, claimed, first.resolved_thread_ids, registry=scenario.registry())

    assert reopened == []
    assert scenario.transport.resolved is True
    assert scenario.registry().pending_for_pr(REPOSITORY, PR_NUMBER) == [THREAD_ID]
    assert any("ROLLBACK_REQUIRED" in body for _comment_id, body in scenario.transport.patches)
    receipt = observe_closure_receipt(scenario.client(), REPOSITORY, PR_NUMBER, THREAD_ID, ROOT_ID).receipt
    assert receipt is not None and receipt.phase == "RESOLVED"
    assert retry_pending_stale_review_thread_rollbacks(scenario.client(), REPOSITORY, PR_NUMBER, registry=scenario.registry()) == [THREAD_ID]
    assert scenario.transport.resolved is True
    scenario.transport.reject_patches = False
    scenario.transport.reject_unresolves = False

    assert retry_pending_stale_review_thread_rollbacks(scenario.client(), REPOSITORY, PR_NUMBER, registry=scenario.registry()) == []
    assert scenario.transport.resolved is False
    assert scenario.transport.effects.count("unresolve") == 1
    assert len(scenario.transport.posts) == 1
    assert scenario.registry().pending_for_pr(REPOSITORY, PR_NUMBER) == []


def test_old_cleanup_reports_newer_required_receipt_unfinished_when_unresolved(receipt_scenario):
    scenario = receipt_scenario
    claimed = scenario.claimed()
    first = scenario.close(claimed=claimed)
    old_registry = scenario.registry()
    scenario.transport.reject_patches = True
    assert reopen_review_threads_after_publication_failure(scenario.client(), REPOSITORY, PR_NUMBER, claimed, first.resolved_thread_ids, registry=old_registry) == [THREAD_ID]
    assert old_registry.marker_cleanup_pending_for_pr(REPOSITORY, PR_NUMBER) == {THREAD_ID: ROOT_ID}
    old_operation = old_registry.receipt_operation_keys_for_pr(REPOSITORY, PR_NUMBER)[THREAD_ID]
    scenario.transport.reject_patches = False
    scenario.transport.head = NEXT_HEAD
    other_registry = StaleReviewThreadRegistry(path=scenario.path / "other-process-rollback.json")
    second = scenario.close(claimed=claimed, registry=other_registry)
    assert second.resolved_thread_ids == (THREAD_ID,)

    def reject_cleanup_after_actual_unresolve():
        scenario.transport.reject_patches = True

    scenario.transport.before_unresolve = reject_cleanup_after_actual_unresolve
    assert reopen_review_threads_after_publication_failure(scenario.client(), REPOSITORY, PR_NUMBER, claimed, second.resolved_thread_ids, registry=other_registry) == [THREAD_ID]
    scenario.transport.before_unresolve = None
    scenario.transport.reject_patches = False
    newer_receipt = observe_closure_receipt(scenario.client(), REPOSITORY, PR_NUMBER, THREAD_ID, ROOT_ID).receipt
    assert newer_receipt is not None
    assert newer_receipt.phase == "ROLLBACK_REQUIRED"
    assert newer_receipt.operation_key != old_operation
    assert scenario.transport.resolved is False
    effects_before = list(scenario.transport.effects)

    pending = retry_pending_stale_review_thread_rollbacks(scenario.client(), REPOSITORY, PR_NUMBER, registry=old_registry)

    assert pending == [THREAD_ID]
    assert scenario.transport.effects == effects_before
    assert observe_closure_receipt(scenario.client(), REPOSITORY, PR_NUMBER, THREAD_ID, ROOT_ID).receipt == newer_receipt
    assert old_registry.marker_cleanup_pending_for_pr(REPOSITORY, PR_NUMBER) == {}
    assert retry_pending_stale_review_thread_rollbacks(scenario.client(), REPOSITORY, PR_NUMBER, registry=old_registry) == []
    assert scenario.transport.effects == effects_before + ["patch"]
    assert scenario.transport.effects.count("unresolve") == 2
    assert len(scenario.transport.posts) == 1


def test_reopened_operation_rearms_pending_guard_before_resolve(receipt_scenario):
    scenario = receipt_scenario
    claimed = scenario.claimed()
    first = scenario.close(claimed=claimed)
    reopen_review_threads_after_publication_failure(scenario.client(), REPOSITORY, PR_NUMBER, claimed, first.resolved_thread_ids, registry=scenario.registry())
    observed_pending = []

    def observe_pending():
        owned = [comment for comment in scenario.transport.comments if comment.login == CONTROLLER_LOGIN]
        assert len(owned) == 1
        assert "PENDING" in owned[0].body
        assert HEAD in owned[0].body
        observed_pending.append(owned[0].database_id)

    scenario.transport.before_resolve = observe_pending
    scenario.transport.move_head_after_resolve = True

    stale = scenario.close(claimed=claimed)

    assert observed_pending
    assert stale.resolved_thread_ids == ()
    assert scenario.transport.resolved is False
    assert scenario.transport.effects.count("unresolve") == 2
    assert len(scenario.transport.posts) == 1
    assert "ROLLED_BACK" in next(comment.body for comment in scenario.transport.comments if comment.login == CONTROLLER_LOGIN)


def test_pending_receipt_recovers_without_local_registry(receipt_scenario, monkeypatch):
    scenario = receipt_scenario
    registry = scenario.registry()

    def fail_registry(*_args, **_kwargs):
        raise OSError("Local state volume unavailable")

    monkeypatch.setattr(registry, "record_rollback_transition", fail_registry)
    scenario.transport.move_head_after_resolve = True
    with pytest.raises(StaleReviewThreadResolutionError):
        scenario.close(registry=registry)
    assert scenario.transport.resolved is True
    assert len(scenario.transport.posts) == 1

    fresh_registry = StaleReviewThreadRegistry(path=scenario.path / "fresh-empty-registry.json")
    pending = retry_pending_stale_review_thread_rollbacks(scenario.client(), REPOSITORY, PR_NUMBER, registry=fresh_registry)

    assert pending == []
    assert scenario.transport.resolved is False
    assert scenario.transport.effects.count("unresolve") == 1
    assert len(scenario.transport.posts) == 1


def test_resolved_pending_receipt_finishes_after_terminal_update_failure(receipt_scenario):
    scenario = receipt_scenario
    scenario.transport.reject_patches = True

    failed = scenario.close()

    assert failed.unfinished_outcomes
    assert scenario.transport.resolved is True
    owned = next(comment for comment in scenario.transport.comments if comment.login == CONTROLLER_LOGIN)
    assert "PENDING" in owned.body
    assert len(scenario.transport.posts) == 1
    assert scenario.transport.effects.count("resolve") == 1
    effects_before_recovery = list(scenario.transport.effects)
    scenario.transport.reject_patches = False

    fresh_registry = StaleReviewThreadRegistry(path=scenario.path / "fresh-terminal-update-registry.json")
    pending = retry_pending_stale_review_thread_rollbacks(scenario.client(), REPOSITORY, PR_NUMBER, registry=fresh_registry)

    assert pending == []
    assert scenario.transport.resolved is True
    assert "RESOLVED" in owned.body
    assert scenario.transport.effects == effects_before_recovery + ["patch"]
    assert len(scenario.transport.posts) == 1
    effects_after_recovery = list(scenario.transport.effects)
    assert retry_pending_stale_review_thread_rollbacks(scenario.client(), REPOSITORY, PR_NUMBER, registry=scenario.registry()) == []
    assert scenario.transport.effects == effects_after_recovery


@pytest.mark.parametrize("fault", ["fail_thread_reads", "missing_thread", "duplicate_thread", "missing_page_cursor"])
def test_unavailable_or_conflicting_observation_cannot_publish(receipt_scenario, fault):
    scenario = receipt_scenario
    claimed = scenario.claimed()
    if fault == "missing_page_cursor":
        scenario.transport.page_size = 1
    setattr(scenario.transport, fault, True)

    result = scenario.close(claimed=claimed)

    assert result.resolved_thread_ids == ()
    assert result.unfinished_outcomes
    assert scenario.transport.posts == []
    assert scenario.transport.patches == []
    assert "resolve" not in scenario.transport.effects


def test_receipt_on_later_comment_page_is_reused(receipt_scenario):
    scenario = receipt_scenario
    for index in range(101):
        scenario.transport.append_comment(f"Discussion note {index}", login="human-reviewer", author_id=801)
    claimed = scenario.claimed()
    first = scenario.close(claimed=claimed)
    assert first.resolved_thread_ids == (THREAD_ID,)
    assert any(offset >= 100 for offset in scenario.transport.page_reads)
    scenario.transport.page_reads.clear()

    replay = scenario.close(claimed=claimed)

    assert replay.resolved_thread_ids == (THREAD_ID,)
    assert any(offset >= 100 for offset in scenario.transport.page_reads)
    assert len(scenario.transport.posts) == 1


@pytest.mark.parametrize("status", ["STILL_VALID", "INCONCLUSIVE"])
def test_existing_receipt_does_not_authorize_new_substantive_review(receipt_scenario, status):
    scenario = receipt_scenario
    claimed = scenario.claimed()
    first = scenario.close(claimed=claimed)
    reopen_review_threads_after_publication_failure(scenario.client(), REPOSITORY, PR_NUMBER, claimed, first.resolved_thread_ids, registry=scenario.registry())
    scenario.transport.append_comment("The counter still survives the separate timeout retry; investigate that path.", login="auto-coder-reviewer[bot]", author_id=REVIEWER_ID)
    effects_before = list(scenario.transport.effects)

    result = scenario.close(status=status)

    assert result.resolved_thread_ids == ()
    assert result.unfinished_outcomes
    assert scenario.transport.resolved is False
    assert scenario.transport.effects == effects_before
    assert len(scenario.transport.posts) == 1


def test_changed_head_rebinds_same_reply_after_independent_acceptance(receipt_scenario):
    scenario = receipt_scenario
    claimed = scenario.claimed()
    first = scenario.close(claimed=claimed)
    reopen_review_threads_after_publication_failure(scenario.client(), REPOSITORY, PR_NUMBER, claimed, first.resolved_thread_ids, registry=scenario.registry())
    scenario.transport.head = NEXT_HEAD
    result = scenario.close(rationale="The independently inspected src/retry.py correction is valid on the new head.")

    assert result.resolved_thread_ids == (THREAD_ID,)
    assert len(scenario.transport.posts) == 1
    assert NEXT_HEAD in next(comment.body for comment in scenario.transport.comments if comment.login == CONTROLLER_LOGIN)


def test_wrong_author_receipt_cannot_be_adopted(receipt_scenario):
    scenario = receipt_scenario
    assert scenario.close().resolved_thread_ids == (THREAD_ID,)
    owned_body = next(comment.body for comment in scenario.transport.comments if comment.login == CONTROLLER_LOGIN)
    scenario.transport.comments = _initial_comments()
    foreign = scenario.transport.append_comment(owned_body, login="implementation-agent[bot]", author_id=702)
    scenario.transport.resolved = False
    scenario.transport.posts.clear()
    scenario.transport.patches.clear()

    result = scenario.close()

    assert result.resolved_thread_ids == (THREAD_ID,)
    assert foreign.body == owned_body
    assert len(scenario.transport.posts) == 1
    assert all(comment_id != foreign.database_id for comment_id, _body in scenario.transport.patches)


@pytest.mark.parametrize("malformation", ["duplicate", "invalid_payload"])
def test_ambiguous_owned_receipt_does_not_create_replacement(receipt_scenario, malformation):
    scenario = receipt_scenario
    claimed = scenario.claimed()
    assert scenario.close(claimed=claimed).resolved_thread_ids == (THREAD_ID,)
    owned = next(comment for comment in scenario.transport.comments if comment.login == CONTROLLER_LOGIN)
    if malformation == "duplicate":
        scenario.transport.append_comment(owned.body)
    else:
        owned.body = owned.body.splitlines()[0] + "\n<!-- broken JSON -->"
    effects_before = list(scenario.transport.effects)

    result = scenario.close(claimed=claimed)

    assert result.resolved_thread_ids == ()
    assert result.unfinished_outcomes
    assert scenario.transport.effects == effects_before
    assert len(scenario.transport.posts) == 1


def test_competing_publisher_observably_contends_and_later_reuses_reply(receipt_scenario):
    scenario = receipt_scenario
    claimed = scenario.claimed()
    entered = Event()
    release = Event()

    def pause_before_first_create():
        entered.set()
        assert release.wait(5), "Test failed to release the actual reply transport"

    scenario.transport.before_create = pause_before_first_create
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(scenario.close, claimed=claimed)
        try:
            assert entered.wait(5), "Publisher did not reach the protected transport boundary"
            contending = scenario.close(claimed=claimed)
            assert contending.resolved_thread_ids == ()
            assert contending.unfinished_outcomes
            assert scenario.transport.posts == []
        finally:
            release.set()
        assert first.result(timeout=5).resolved_thread_ids == (THREAD_ID,)

    scenario.transport.before_create = None
    resumed = scenario.close(claimed=claimed)
    assert resumed.resolved_thread_ids == (THREAD_ID,)
    assert len(scenario.transport.posts) == 1


def test_publication_reopen_observably_contends_with_closure_lock(receipt_scenario):
    scenario = receipt_scenario
    claimed = scenario.claimed()
    first = scenario.close(claimed=claimed)
    assert first.resolved_thread_ids == (THREAD_ID,)
    effects_before = list(scenario.transport.effects)
    entered = Event()
    release = Event()

    def hold_closure_lock():
        with closure_receipt_lock(REPOSITORY, PR_NUMBER):
            entered.set()
            assert release.wait(5), "Test failed to release the actual closure lock"

    with ThreadPoolExecutor(max_workers=1) as executor:
        lock_holder = executor.submit(hold_closure_lock)
        try:
            assert entered.wait(5), "Worker did not acquire the actual closure lock"
            reopened = reopen_review_threads_after_publication_failure(scenario.client(), REPOSITORY, PR_NUMBER, claimed, first.resolved_thread_ids, registry=scenario.registry())
            assert reopened == []
            assert scenario.transport.resolved is True
            assert scenario.transport.effects == effects_before
        finally:
            release.set()
        lock_holder.result(timeout=5)

    reopened = reopen_review_threads_after_publication_failure(scenario.client(), REPOSITORY, PR_NUMBER, claimed, first.resolved_thread_ids, registry=scenario.registry())

    assert reopened == [THREAD_ID]
    assert scenario.transport.resolved is False
    assert scenario.transport.effects.count("unresolve") == 1
    assert len(scenario.transport.posts) == 1
    assert scenario.registry().rollback_transitions_for_pr(REPOSITORY, PR_NUMBER) == {}
    assert scenario.registry().marker_cleanup_pending_for_pr(REPOSITORY, PR_NUMBER) == {}


@pytest.mark.parametrize("guard", ["exact_operation", "wrong_key", "changed_rationale", "changed_head", "changed_operation", "resolved_prior"])
def test_historical_cleanup_only_retires_the_exact_pending_operation(receipt_scenario, guard):
    scenario = receipt_scenario
    scenario.transport.reject_patches = guard != "resolved_prior"
    scenario.close()
    scenario.transport.reject_patches = False
    client = scenario.client()
    observation = observe_closure_receipt(client, REPOSITORY, PR_NUMBER, THREAD_ID, ROOT_ID)
    prior = observation.receipt
    assert prior is not None
    assert prior.phase == ("RESOLVED" if guard == "resolved_prior" else "PENDING")
    # The old operation was actually rolled back, then resolved independently.
    # Cleanup must preserve that newer resolved state.
    client.unresolve_review_thread(THREAD_ID)
    client.resolve_review_thread(THREAD_ID)
    desired = replace(prior, phase="ROLLED_BACK")
    confirmed_key = prior.operation_key
    if guard == "wrong_key":
        confirmed_key = "different-rollback-operation"
    elif guard == "changed_rationale":
        desired = replace(desired, rationale="A different rationale cannot inherit old rollback proof.")
    elif guard == "changed_head":
        desired = replace(desired, validated_head_sha=NEXT_HEAD)
    elif guard == "changed_operation":
        desired = replace(desired, operation_key="different-closure-operation")
    effects_before = list(scenario.transport.effects)
    owned = next(comment for comment in scenario.transport.comments if comment.login == CONTROLLER_LOGIN)
    body_before = owned.body

    with closure_receipt_lock(REPOSITORY, PR_NUMBER):
        if guard == "exact_operation":
            confirmed = put_receipt(client, observation, desired, confirmed_rollback_operation_key=confirmed_key)
            assert confirmed.receipt == desired
            assert scenario.transport.effects == effects_before + ["patch"]
            assert "ROLLED_BACK" in owned.body
        else:
            with pytest.raises(ClosureReceiptError):
                put_receipt(client, observation, desired, confirmed_rollback_operation_key=confirmed_key)
            assert scenario.transport.effects == effects_before
            assert owned.body == body_before

    assert scenario.transport.resolved is True
    assert len(scenario.transport.posts) == 1


def test_receipt_evidence_round_trips_markup_and_trailing_whitespace(receipt_scenario):
    scenario = receipt_scenario
    assert scenario.close().resolved_thread_ids == (THREAD_ID,)
    client = scenario.client()
    observation = observe_closure_receipt(client, REPOSITORY, PR_NUMBER, THREAD_ID, ROOT_ID)
    assert observation.receipt is not None
    desired = replace(observation.receipt, evidence="Boundary \u03b1 <retry> & --> remains verified.\n \t")
    effects_before = list(scenario.transport.effects)

    with closure_receipt_lock(REPOSITORY, PR_NUMBER):
        confirmed = put_receipt(client, observation, desired)

    assert confirmed.receipt == desired
    assert observe_closure_receipt(scenario.client(), REPOSITORY, PR_NUMBER, THREAD_ID, ROOT_ID).receipt == desired
    assert scenario.transport.effects == effects_before + ["patch"]
    assert len(scenario.transport.posts) == 1


@pytest.mark.parametrize("is_resolved", [True, False])
def test_required_rollback_cannot_be_promoted_or_rebound(receipt_scenario, is_resolved):
    scenario = receipt_scenario
    assert scenario.close().resolved_thread_ids == (THREAD_ID,)
    client = scenario.client()
    observation = observe_closure_receipt(client, REPOSITORY, PR_NUMBER, THREAD_ID, ROOT_ID)
    assert observation.receipt is not None
    with closure_receipt_lock(REPOSITORY, PR_NUMBER):
        observation = put_receipt(client, observation, replace(observation.receipt, phase="ROLLBACK_REQUIRED"))
    if not is_resolved:
        client.unresolve_review_thread(THREAD_ID)
        observation = observe_closure_receipt(client, REPOSITORY, PR_NUMBER, THREAD_ID, ROOT_ID)
    obligation = observation.receipt
    assert obligation is not None
    effects_before = list(scenario.transport.effects)
    forbidden = (
        replace(obligation, phase="PENDING"),
        replace(obligation, phase="RESOLVED"),
        replace(obligation, validated_head_sha=NEXT_HEAD),
        replace(obligation, operation_key="another-closure-operation"),
        replace(obligation, rationale="A different rationale cannot replace the rollback obligation."),
        replace(obligation, phase="ROLLED_BACK", evidence="Changed evidence cannot inherit old rollback proof."),
    )

    with closure_receipt_lock(REPOSITORY, PR_NUMBER):
        for desired in forbidden:
            with pytest.raises(ClosureReceiptError):
                put_receipt(client, observation, desired, confirmed_rollback_operation_key=obligation.operation_key)
        assert put_receipt(client, observation, obligation).receipt == obligation

    assert scenario.transport.effects == effects_before
    assert scenario.transport.resolved is is_resolved
    assert len(scenario.transport.posts) == 1


@pytest.mark.parametrize("prior_state", ["missing", "rolled_back"])
def test_rollback_intent_cannot_manufacture_or_resurrect_an_operation(receipt_scenario, prior_state):
    scenario = receipt_scenario
    claimed = scenario.claimed()
    first = scenario.close(claimed=claimed)
    assert first.resolved_thread_ids == (THREAD_ID,)
    client = scenario.client()
    prior = observe_closure_receipt(client, REPOSITORY, PR_NUMBER, THREAD_ID, ROOT_ID).receipt
    assert prior is not None
    if prior_state == "missing":
        scenario.transport.comments = _initial_comments()
        desired = replace(prior, phase="ROLLBACK_REQUIRED", comment_id=None)
    else:
        assert reopen_review_threads_after_publication_failure(client, REPOSITORY, PR_NUMBER, claimed, first.resolved_thread_ids, registry=scenario.registry()) == [THREAD_ID]
        client.resolve_review_thread(THREAD_ID)
        prior = observe_closure_receipt(client, REPOSITORY, PR_NUMBER, THREAD_ID, ROOT_ID).receipt
        assert prior is not None and prior.phase == "ROLLED_BACK"
        desired = replace(prior, phase="ROLLBACK_REQUIRED")
    observation = observe_closure_receipt(client, REPOSITORY, PR_NUMBER, THREAD_ID, ROOT_ID)
    effects_before = list(scenario.transport.effects)

    with closure_receipt_lock(REPOSITORY, PR_NUMBER), pytest.raises(ClosureReceiptError):
        put_receipt(client, observation, desired)

    assert scenario.transport.effects == effects_before
    assert scenario.transport.resolved is True
    assert len(scenario.transport.posts) == 1


def test_active_legacy_and_new_receipt_are_both_retired_after_rollback(receipt_scenario):
    scenario = receipt_scenario
    assert scenario.close().resolved_thread_ids == (THREAD_ID,)
    scenario.transport.append_comment(f"{STALE_BLOCKER_MARKER}\n<!-- resolved-against-head: {HEAD} -->")
    scenario.transport.head = NEXT_HEAD

    pending = retry_pending_stale_review_thread_rollbacks(scenario.client(), REPOSITORY, PR_NUMBER, registry=scenario.registry())

    assert pending == []
    assert scenario.transport.resolved is False
    assert scenario.transport.effects.count("unresolve") == 1
    assert sum(body == STALE_BLOCKER_CLEARED_MARKER for body in scenario.transport.posts) == 1
    assert len(scenario.transport.posts) == 2
    effects_after_cleanup = list(scenario.transport.effects)

    assert retry_pending_stale_review_thread_rollbacks(scenario.client(), REPOSITORY, PR_NUMBER, registry=scenario.registry()) == []
    assert scenario.transport.effects == effects_after_cleanup
