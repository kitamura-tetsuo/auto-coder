"""Regression for event-driven workers reporting a false-empty dashboard."""

import asyncio
from unittest.mock import MagicMock

import httpx
import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.open_target_observations import OpenTarget, OpenTargetObservations, open_targets_notice
from src.auto_coder.util.gh_cache import OpenGitHubEntities, OpenGitHubIssue


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    github = MagicMock()
    github.get_pr_details.side_effect = lambda item: item
    github.get_issue_details.side_effect = lambda item: item
    value = AutomationEngine(github, AutomationConfig())
    monkeypatch.setattr(value, "_schedule_pr_owner_reclamation", lambda *args: None)
    monkeypatch.setattr(value, "_revoke_closed_issue_eligibility", lambda *args: None)
    monkeypatch.setattr(value, "_is_pr_author_allowed", lambda item: False)
    monkeypatch.setattr(value, "_is_issue_author_allowed", lambda item: False)
    return value


def reconcile(engine, issues=(), prs=()):
    engine.github.get_open_entities_strict.return_value = OpenGitHubEntities(issues=[OpenGitHubIssue(n) for n in issues], pull_requests=list(prs))
    asyncio.run(engine._reconcile_open_github_entities("owner/repo"))


def identities(engine):
    return {(row["type"], row["number"]) for row in engine.get_status()["open_items"]}


def test_complete_startup_projects_targets_without_extra_reads(engine):
    reconcile(engine, (12,), (34,))
    assert identities(engine) == {("issue", 12), ("pr", 34)}
    state = engine.get_status()
    assert state["open_target_observation"]["availability"] == "available"
    assert "Recorded open targets" in open_targets_notice(state["open_target_observation"], empty=False)
    assert engine.invalidations.pending_count("owner/repo") == 2
    engine.github.get_open_entities_strict.assert_called_once_with("owner/repo")
    engine.github.get_pull_request_metadata_strict.assert_not_called()
    engine.github.get_issue_dispatch_snapshot_strict.assert_not_called()


def test_unknown_and_complete_empty_have_distinct_presentation(engine):
    state = engine.get_status()
    assert state["open_target_observation"]["availability"] == "unavailable"
    assert "has not been confirmed" in open_targets_notice(state["open_target_observation"], empty=True)
    reconcile(engine)
    state = engine.get_status()
    assert identities(engine) == set()
    assert "Complete enumeration observed" in open_targets_notice(state["open_target_observation"], empty=True)


@pytest.mark.parametrize("prior", [False, True])
def test_failed_enumeration_never_claims_observed_empty(engine, prior):
    if prior:
        reconcile(engine, prs=(34,))
    engine.github.get_open_entities_strict.side_effect = RuntimeError("transport unavailable")
    with pytest.raises(RuntimeError, match="transport unavailable"):
        asyncio.run(engine._reconcile_open_github_entities("owner/repo"))
    state = engine.get_status()
    assert state["open_target_observation"]["availability"] == ("stale" if prior else "unavailable")
    assert identities(engine) == ({("pr", 34)} if prior else set())
    assert "No open issues" not in open_targets_notice(state["open_target_observation"], empty=not prior)


@pytest.mark.parametrize("kind", ["pr", "issue"])
def test_strict_observation_updates_advisory_view_before_admission(engine, kind):
    reconcile(engine)
    data = {"number": 34, "state": "open", "title": "Observed title", "body": ""}
    method = engine.github.get_pull_request_metadata_strict if kind == "pr" else engine.github.get_issue_dispatch_snapshot_strict
    method.return_value = data
    # The author is deliberately disallowed: being visible grants no admission.
    assert engine._create_candidate_from_single("owner/repo", kind, 34, True) is None
    assert identities(engine) == {(kind, 34)}
    assert engine.get_status()["open_items"][0]["title"] == "Observed title"
    method.return_value = {**data, "state": "closed"}
    assert engine._create_candidate_from_single("owner/repo", kind, 34, True) is None
    assert identities(engine) == set()
    method.return_value = data
    engine._create_candidate_from_single("owner/repo", kind, 34, True)
    request = httpx.Request("GET", "https://api.github.com/resource")
    method.side_effect = httpx.HTTPStatusError("missing", request=request, response=httpx.Response(404, request=request))
    assert engine._create_candidate_from_single("owner/repo", kind, 34, True) is None
    assert identities(engine) == set()


def test_transport_failure_keeps_known_target(engine):
    reconcile(engine, prs=(34,))
    engine.github.get_pull_request_metadata_strict.side_effect = RuntimeError("transport unavailable")
    with pytest.raises(RuntimeError, match="transport unavailable"):
        engine._create_candidate_from_single("owner/repo", "pr", 34, True)
    assert identities(engine) == {("pr", 34)}


def test_newer_strict_close_or_open_wins_over_older_enumeration():
    view = OpenTargetObservations()
    revision = view.revision()
    view.observe(OpenTarget("pr", 34), "closed")
    view.observe(OpenTarget("issue", 56, "New title"), "open")
    view.complete([OpenTarget("pr", 34)], revision)
    targets, metadata = view.snapshot()
    assert targets == [OpenTarget("issue", 56, "New title")]
    assert metadata["availability"] == "available"
