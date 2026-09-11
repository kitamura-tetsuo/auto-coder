"""Negative admission caching and webhook-driven refresh regressions."""

import asyncio
from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult, ExplicitTargetOutcome
from auto_coder.automation_engine import AutomationEngine
from auto_coder.issue_admission_cache import IssueAdmissionCache
from auto_coder.reissue_required_store import ReissueRequiredStore
from auto_coder.util.gh_cache import GitHubClient
from auto_coder.webhook_server import process_github_payload


def issue(number=2000, body="Parent-Issue: #1999", updated_at="2026-09-12T08:00:00Z"):
    return {"number": number, "title": "Child", "body": body, "state": "open", "updated_at": updated_at, "labels": [{"name": "implementation-ready"}]}


def blocked():
    return CandidateProcessingResult(type="issue", number=2000, target_outcome=ExplicitTargetOutcome.BLOCKED, error="Invalid contract", blocked_cacheable=True)


def mark_parent():
    store = ReissueRequiredStore("owner/repo")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.mark(1999)


def test_negative_cache_copies_results_and_never_authorizes():
    cache = IssueAdmissionCache()
    result = blocked()
    cache.remember("owner/repo", 2000, "policy", 0, result)
    result.actions.append("mutated")
    assert cache.get("owner/repo", 2000, "policy").actions == []
    assert cache.get("other/repo", 2000, "policy") is None
    assert cache.get("owner/repo", 2000, "changed-policy") is None
    for outcome in (ExplicitTargetOutcome.DEFERRED, ExplicitTargetOutcome.SKIPPED):
        result.number = 2001
        result.target_outcome = outcome
        cache.remember("owner/repo", 2001, "policy", 0, result)
        assert cache.get("owner/repo", 2001, "policy") is None
    result.target_outcome = ExplicitTargetOutcome.BLOCKED
    result.number = 2002
    result.blocked_cacheable = False
    cache.remember("owner/repo", 2002, "policy", 0, result)
    assert cache.get("owner/repo", 2002, "policy") is None


def test_invalidation_fences_inflight_result_and_expires_parent_observation():
    cache = IssueAdmissionCache()
    cache.observe("owner/repo", issue())
    epoch = cache.epoch("owner/repo")
    cache.invalidate("owner/repo")
    cache.remember("owner/repo", 2000, "policy", epoch, blocked())
    assert cache.get("owner/repo", 2000, "policy") is None
    assert cache.snapshot("owner/repo", 2000) is None
    assert cache.observe("owner/repo", issue()) is False
    assert cache.snapshot("owner/repo", 2000) is None


def test_observations_and_blocks_expire_without_webhooks(monkeypatch):
    monkeypatch.setattr("auto_coder.issue_admission_cache.time.monotonic", lambda: 10.0)
    cache = IssueAdmissionCache()
    cache.observe("owner/repo", issue())
    cache.remember("owner/repo", 2000, "policy", 0, blocked())
    monkeypatch.setattr("auto_coder.issue_admission_cache.time.monotonic", lambda: 310.0)
    cache.observe("owner/repo", issue())
    assert cache.snapshot("owner/repo", 2000) is None
    assert cache.get("owner/repo", 2000, "policy") is None
    cache.observe("owner/repo", issue(), authoritative=True)
    assert cache.snapshot("owner/repo", 2000)["body"] == "Parent-Issue: #1999"


def test_auxiliary_webhook_and_candidate_fields_do_not_invalidate_refusal():
    cache = IssueAdmissionCache()
    cache.observe("owner/repo", issue())
    cache.remember("owner/repo", 2000, "policy", 0, blocked())
    cache.observe("owner/repo", {**issue(), "html_url": "https://github.com/owner/repo/issues/2000"})
    cache.observe("owner/repo", {**issue(), "labels": ["implementation-ready"]})
    assert cache.get("owner/repo", 2000, "policy").error == "Invalid contract"


def test_equal_timestamp_conflicts_require_authoritative_refresh():
    cache = IssueAdmissionCache()
    cache.observe("owner/repo", issue())
    changed = issue(body="No parent declaration")
    assert cache.observe("owner/repo", changed) is False
    assert cache.snapshot("owner/repo", 2000) is None
    assert cache.observe("owner/repo", changed) is False
    assert cache.observe("owner/repo", changed, authoritative=True) is True
    assert cache.snapshot("owner/repo", 2000)["body"] == "No parent declaration"


def test_terminal_parent_refuses_before_any_github_or_slot_call():
    github = MagicMock(spec=GitHubClient)
    config = AutomationConfig(repo_name="owner/repo")
    engine = AutomationEngine(github, config)
    mark_parent()
    result = engine._process_single_candidate_unified("owner/repo", Candidate(type="issue", data=issue(), priority=0), config)
    assert result.target_outcome is ExplicitTargetOutcome.BLOCKED
    assert result.error == "Parent specification set requires a replacement Issue number"
    assert result.blocked_cacheable is True
    assert github.mock_calls == []
    assert engine.implementation_slots is None


@pytest.mark.parametrize("event_type,action", [("issues", "edited"), ("issues", "labeled"), ("issues", "closed"), ("sub_issues", "sub_issue_removed"), ("issue_dependencies", "blocked_by_removed")])
def test_webhook_invalidates_negative_results_for_related_issues(event_type, action):
    engine = AutomationEngine(MagicMock(spec=GitHubClient), AutomationConfig(repo_name="owner/repo"))
    cache = engine.issue_admission_cache
    cache.remember("owner/repo", 2000, "policy", 0, blocked())
    payload = {"repository": {"full_name": "owner/repo"}, "action": action, "issue": issue(1999), "changes": {"body": {"from": "old"}}}
    asyncio.run(process_github_payload(event_type, payload, engine, "owner/repo", "delivery"))
    assert cache.get("owner/repo", 2000, "policy") is None
    assert engine.github.mock_calls == []


def test_webhook_updates_body_and_does_not_restore_old_parent():
    engine = AutomationEngine(MagicMock(spec=GitHubClient), AutomationConfig(repo_name="owner/repo"))
    mark_parent()
    engine.issue_admission_cache.observe("owner/repo", issue())
    assert engine._cached_issue_refusal("owner/repo", 2000, engine.config) is not None
    newer = issue(body="No parent declaration", updated_at="2026-09-12T09:00:00Z")
    payload = {"repository": {"full_name": "owner/repo"}, "action": "edited", "issue": newer, "changes": {"body": {"from": "Parent-Issue: #1999"}}}
    asyncio.run(process_github_payload("issues", payload, engine, "owner/repo", "newer"))
    assert engine.issue_admission_cache.snapshot("owner/repo", 2000)["body"] == "No parent declaration"
    assert engine._cached_issue_refusal("owner/repo", 2000, engine.config) is None
    payload["issue"] = issue()
    asyncio.run(process_github_payload("issues", payload, engine, "owner/repo", "older"))
    assert engine._cached_issue_refusal("owner/repo", 2000, engine.config) is None
    assert engine.github.mock_calls == []


class StandaloneGitHub:
    """In-memory external GitHub boundary for real admission-path tests."""

    def __init__(self):
        self.current = {**issue(body="## Requirements\n- Missing a requirement ID"), "user": {"id": 1}}
        self.reads = 0
        self.comments = []

    def get_issue_dispatch_snapshot_strict(self, repository, number):
        self.reads += 1
        return deepcopy(self.current)

    def get_direct_sub_issues_strict(self, repository, number):
        self.reads += 1
        return []

    def get_parent_issue_details_strict(self, repository, number):
        self.reads += 1
        return None

    def get_issue_comments_strict(self, repository, number):
        self.reads += 1
        return deepcopy(self.comments)

    def add_comment_to_issue(self, repository, number, body):
        self.comments.append({"body": body})


def test_completed_contract_refusal_is_reused_until_webhook_then_strictly_refreshed():
    github = StandaloneGitHub()
    config = AutomationConfig(repo_name="owner/repo")
    config.ISSUE_ALLOWLIST = [1]
    engine = AutomationEngine(github, config)
    candidate = Candidate(type="issue", data=deepcopy(github.current), priority=0)
    first = engine._process_single_candidate_unified("owner/repo", candidate, config)
    assert first.target_outcome is ExplicitTargetOutcome.BLOCKED
    assert first.blocked_cacheable is True
    assert github.reads > 0
    assert len(github.comments) == 1
    github.reads = 0
    second = engine._process_single_candidate_unified("owner/repo", candidate, config)
    assert second.error == first.error
    assert second.target_outcome is ExplicitTargetOutcome.BLOCKED
    assert github.reads == 0
    assert len(github.comments) == 1

    github.current.update(state="closed", updated_at="2026-09-12T10:00:00Z")
    asyncio.run(engine.invalidate_entity("owner/repo", "issue", 2000, issue_snapshot=deepcopy(github.current)))
    refreshed = engine._process_single_candidate_unified("owner/repo", candidate, config)
    assert refreshed.target_outcome is ExplicitTargetOutcome.SKIPPED
    assert github.reads > 0
    assert len(github.comments) == 1


def test_worker_acknowledges_terminal_refusal_without_strict_refresh_or_validation():
    engine = AutomationEngine(MagicMock(spec=GitHubClient), AutomationConfig(repo_name="owner/repo"))
    mark_parent()

    async def scenario():
        await engine.invalidate_entity("owner/repo", "issue", 2000, issue_snapshot=issue())
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
        try:
            await asyncio.wait_for(engine.queue.join(), timeout=2)
            assert engine.invalidations.claim("owner/repo") is None
            assert engine.active_workers[0] is None
        finally:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker

    asyncio.run(scenario())
    assert engine.github.mock_calls == []
    assert engine.implementation_slots is None
    assert engine._specification_validators == {}
    assert engine._decomposition_validators == {}
