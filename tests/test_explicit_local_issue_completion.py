"""Integration coverage of single-target local execution and publication."""

import json
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock, patch

import pytest

from auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult, ExplicitTargetOutcome
from auto_coder.automation_engine import AutomationEngine
from auto_coder.execution_trace import get_trace_collector
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.issue_dispatch import CandidateHandoff, DispatchOutcome, DispatchResult, IssueAttemptIdentity, IssueDispatchGuard
from auto_coder.issue_local_job import IssueLocalJobAdapter
from auto_coder.local_job_handoff import LocalJobKind, LocalJobOffer, LocalJobState, LocalJobStore
from auto_coder.local_job_runner import LocalJobRunner
from auto_coder.utils import CommandExecutor

from .test_dashboard_observability import _assert_required_stage_visible, _mounted_detail
from .test_issue_job_workspace import git, repository


@pytest.mark.parametrize("behavior", ["published", "no_change", "failed", "refused", "publication_pending", "codex_json", "claude_json", "muse_json", "cannot_fix", "external_runner", "external_checkpoint", "generated_cache", "cache_no_change", "stale_main", "retry_latest_main", "fetch_failed"])
def test_explicit_issue_waits_for_local_execution_and_finalization(tmp_path, monkeypatch, _use_real_commands, _use_real_sleep, behavior):
    fresh_source = behavior in {"stale_main", "retry_latest_main"}
    attempt = "retry-new-attempt" if behavior == "retry_latest_main" else "0"
    behavior = "published" if fresh_source else behavior
    generated_cache = behavior in {"generated_cache", "cache_no_change"}
    behavior = "no_change" if behavior == "cache_no_change" else "published" if generated_cache else behavior
    checkpoint_pause = behavior == "external_checkpoint"
    external = behavior in {"external_runner", "external_checkpoint"}
    behavior = "published" if external else behavior
    stream = behavior if behavior.endswith("_json") else ""
    behavior = "published" if stream else behavior
    source, _head = repository(tmp_path)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(source, "remote", "set-url", "origin", str(remote))
    git(source, "push", "origin", "main")
    latest = _head
    if fresh_source:
        (source / "prerequisite.txt").write_text("latest prerequisite\n")
        git(source, "add", "prerequisite.txt")
        git(source, "commit", "-m", "current remote prerequisite")
        latest = git(source, "rev-parse", "HEAD")
        git(source, "push", "origin", "main")
        git(source, "switch", "-c", "dependabot/old", _head)
        (source / "unrelated.txt").write_text("WIP from unrelated branch\n")
        git(source, "add", "unrelated.txt")
        git(source, "commit", "-m", "unrelated WIP")
        git(source, "update-ref", "refs/remotes/origin/main", _head)
    elif behavior == "fetch_failed":
        git(remote, "update-ref", "-d", "refs/heads/main")
    shared_head = git(source, "rev-parse", "HEAD")
    monkeypatch.chdir(source)
    # Configure the controller-owned commits in the retained private clone.
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    guard = IssueDispatchGuard(tmp_path / "dispatch.sqlite3")
    slots = ImplementationSlotRepository("owner/repo", 2, tmp_path / "slots.json", tmp_path / "retired.json")
    owner = ImplementationOwner("issue", 41)
    execution = slots.start_execution(owner, generation="generation")
    assert execution
    identity = IssueAttemptIdentity("owner", "repo", 41, attempt)
    reserved = guard.reserve(identity, CandidateHandoff("codex", "local"))
    offer = LocalJobOffer(LocalJobKind.ISSUE_IMPLEMENTATION, "owner/repo", 41, attempt, "codex", "implement", execution)
    accepted = store.offer_issue(offer, identity, guard)
    assert accepted
    # An unrelated earlier job must remain untouched by --only.
    other_identity = IssueAttemptIdentity("owner", "repo", 42, "0")
    assert guard.reserve(other_identity, CandidateHandoff("codex", "local")).admitted
    other = store.offer_issue(LocalJobOffer(LocalJobKind.ISSUE_IMPLEMENTATION, "owner/repo", 42, "0", "codex", "other"), other_identity, guard)
    assert other
    github = Mock()
    engine = AutomationEngine(github, AutomationConfig())
    traces = get_trace_collector()
    traces.clear()
    engine.local_job_store = store
    monkeypatch.setattr(engine, "_get_implementation_slots", lambda _repo: slots)
    candidate = Candidate(type="issue", data={"number": 41, "title": "Implement"}, priority=1)
    dispatch = DispatchResult(identity, DispatchOutcome.LOCAL_ACCEPTED, "codex", "local", accepted.job_id, claim_incarnation=reserved.claim_incarnation)
    processing = CandidateProcessingResult(type="issue", number=41, success=True, target_outcome=ExplicitTargetOutcome.SUCCESS, dispatch_result=dispatch, actions=["Accepted local implementation job"])
    monkeypatch.setattr(engine, "_check_and_handle_closed_branch", lambda _repo: True)
    monkeypatch.setattr(engine, "_create_candidate_from_single", lambda *_args, **_kwargs: candidate)
    monkeypatch.setattr(engine, "_preflight_explicit_issue_relationships", lambda *_args, **_kwargs: candidate.data)
    monkeypatch.setattr(engine, "_validate_submitted_parent_generation_for_child", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(engine, "_process_single_candidate_unified", lambda *_args, **_kwargs: processing)
    monkeypatch.setattr("auto_coder.issue_local_job.IssueLocalJobAdapter.authorize_provider_entry", lambda *_args: behavior != "refused")
    monkeypatch.setattr("auto_coder.util.github_action.check_and_handle_closed_state", lambda *_args: None)
    entered, release = Event(), Event()
    calls = []

    def implement(prompt):
        calls.append(prompt)
        job_workspace = Path(store.get(accepted.job_id).workspace_path)
        assert git(job_workspace, "rev-parse", "HEAD") == latest
        assert git(job_workspace, "branch", "--show-current") == f"issue-41-attempt-{attempt}"
        if fresh_source:
            assert (job_workspace / "prerequisite.txt").read_text() == "latest prerequisite\n"
            assert not (job_workspace / "unrelated.txt").exists()
        entered.set()
        assert release.wait(10)
        if behavior == "failed":
            raise RuntimeError("implementation failed")
        if behavior == "cannot_fix":
            return "CANNOT_FIX"
        if behavior != "no_change":
            result = CommandExecutor.run_command(["python3", "-c", "from pathlib import Path; Path('value.txt').write_text('implemented\\n')"])
            assert result.success
        if generated_cache:
            generated = CommandExecutor.run_command(["python3", "-c", "from pathlib import Path; p = Path('node_modules/runtime.txt'); p.parent.mkdir(); p.write_text('unverified runtime output')"])
            assert generated.success
        summary = "ACTION_SUMMARY: implemented"
        if stream == "codex_json":
            return "\n".join(json.dumps(event) for event in [{"type": "thread.started"}, {"type": "item.completed", "item": {"type": "agent_message", "text": summary}}, {"type": "turn.completed"}])
        if stream == "claude_json":
            return "\n".join(json.dumps(event) for event in [{"type": "system", "subtype": "init"}, {"type": "result", "subtype": "success", "result": summary}])
        if stream == "muse_json":
            return json.dumps({"record_type": "event", "payload_type": "run.terminal.completed", "payload": {"text": summary}})
        return summary

    monkeypatch.setattr("auto_coder.cli_helpers.build_backend_manager", lambda **_kwargs: Mock(_run_llm_cli=implement))
    published = []
    github.find_pr_by_head_branch_strict.side_effect = lambda *_args: published[0] if published else None
    github.get_issue.return_value = candidate.data

    def create(_repo, _issue, branch, *_args):
        if behavior == "publication_pending":
            raise RuntimeError("publication unavailable")
        published.append({"number": 91, "head": {"ref": branch}, "body": "Closes #41"})

    monkeypatch.setattr("auto_coder.issue_processor._create_pr_for_issue", create)
    results = []
    external_runner = None
    checkpoint_entered, checkpoint_release = Event(), Event()
    if checkpoint_pause:
        record_result = store.record_result

        def checkpoint(*args, **kwargs):
            recorded = record_result(*args, **kwargs)
            checkpoint_entered.set()
            assert checkpoint_release.wait(10)
            return recorded

        monkeypatch.setattr(store, "record_result", checkpoint)
    if external:
        monkeypatch.setattr("auto_coder.automation_engine.runner_owner_alive", lambda owner: True if owner == "external-owner" else None)
        external_runner = LocalJobRunner(store, engine.invocation_gate, repository="owner/repo", capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: IssueLocalJobAdapter(engine, "owner/repo", store, slots, source)}, runner_id="external-owner", job_ids=frozenset({accepted.job_id}))
        assert external_runner.poll() == 1
        assert entered.wait(10)
        if checkpoint_pause:
            release.set()
            assert checkpoint_entered.wait(10)
            assert store.get(accepted.job_id).state is LocalJobState.RESULT_RECORDED
    thread = Thread(target=lambda: results.append(engine.process_single("owner/repo", "issue", 41, explicit_only=True, force=True)))
    thread.start()
    try:
        if behavior not in {"refused", "fetch_failed"}:
            assert entered.wait(10)
            thread.join(0.2)
            assert thread.is_alive()
            assert results == []
            assert published == []
            if checkpoint_pause:
                assert store.get(accepted.job_id).state is LocalJobState.RESULT_RECORDED
    finally:
        checkpoint_release.set()
        release.set()
        thread.join(15)
        if external_runner is not None:
            external_runner.close(wait=True)
    assert not thread.is_alive()
    assert len(results) == 1
    expected = "success" if behavior in {"published", "no_change"} else "failed" if behavior in {"failed", "cannot_fix", "fetch_failed"} else "deferred"
    assert results[0]["target_outcome"] == expected
    assert results[0]["target_type"] == "issue"
    assert results[0]["target_number"] == 41
    assert calls == ([] if behavior in {"refused", "fetch_failed"} else ["implement"])
    assert store.get(other.job_id).state is LocalJobState.PENDING
    assert source.joinpath("value.txt").read_text() == "original\n"
    assert git(source, "rev-parse", "HEAD") == shared_head
    job = store.get(accepted.job_id)
    assert job
    if behavior in {"published", "no_change", "failed", "cannot_fix", "fetch_failed"}:
        assert job.state is LocalJobState.SETTLED
        assert slots.active_execution_ids(owner) == ()
    if behavior == "published":
        assert "Published PR #91" in results[0]["target_reason"]
        assert git(remote, "show", f"{job.work_branch}:value.txt") == "implemented"
        assert len(published) == 1
        assert job.source_commit == latest
        assert job.source_ref == "refs/remotes/origin/main"
        assert git(remote, "rev-parse", f"{job.work_branch}^") == latest
        if fresh_source:
            assert git(remote, "show", f"{job.work_branch}:prerequisite.txt") == "latest prerequisite"
            assert "unrelated.txt" not in git(remote, "ls-tree", "-r", "--name-only", job.work_branch).splitlines()
        assert "node_modules/runtime.txt" not in git(remote, "ls-tree", "-r", "--name-only", job.work_branch).splitlines()
    elif behavior == "fetch_failed":
        assert not job.provider_entered
        assert job.workspace_path == job.source_commit == ""
        assert store.get_effect(job.job_id, job.execution_incarnation, "push") is None
        assert published == []
    elif behavior == "publication_pending":
        assert job.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING
        assert "publication deferred" in results[0]["target_reason"]
        assert slots.active_execution_ids(owner) == (execution,)
    elif behavior == "refused":
        assert job.state is LocalJobState.PENDING
        assert not job.provider_entered
    finalizations = [event for event in traces.get_snapshot(repository="owner/repo", item_type="issue", item_number=41).events if event.stage_id == "issue.local-publication-finalization" and event.facts.get("job_id") == accepted.job_id]
    if behavior == "refused":
        assert finalizations == []
    else:
        assert len(finalizations) == 1
        assert finalizations[0].origin == "explicit-single-target"
        assert finalizations[0].facts["job_id"] == accepted.job_id
        assert finalizations[0].facts["disposition"] == ("pending" if behavior == "publication_pending" else "failed" if behavior == "fetch_failed" else behavior)
        assert finalizations[0].outcome == ("completed" if behavior in {"published", "no_change"} else "failed" if behavior in {"failed", "cannot_fix", "fetch_failed"} else "deferred")
    with patch("auto_coder.dashboard.ui") as mock_ui:
        diagram = _mounted_detail(mock_ui, "issue", 41)
    _assert_required_stage_visible(diagram, "local completion")
    if behavior != "refused":
        _assert_required_stage_visible(diagram, "publication finalization")
    assert f"outcome: {'completed' if expected == 'success' else expected}" in diagram
    if behavior in {"published", "no_change", "failed", "cannot_fix", "publication_pending"}:
        replay = engine.process_single("owner/repo", "issue", 41, explicit_only=True, force=True)
        assert replay["target_outcome"] == expected
        assert calls == ["implement"]
        assert len(published) == (1 if behavior == "published" else 0)


@pytest.mark.parametrize(("returned_type", "returned_number"), [("pr", 41), ("issue", 42)])
def test_mismatched_explicit_identity_cannot_start_local_job(monkeypatch, returned_type, returned_number):
    engine = AutomationEngine(Mock(), AutomationConfig())
    candidate = Candidate(type="issue", data={"number": 41, "title": "Requested"}, priority=1)
    processing = CandidateProcessingResult(
        type=returned_type,
        number=returned_number,
        success=True,
        target_outcome=ExplicitTargetOutcome.SUCCESS,
        dispatch_result=DispatchResult(IssueAttemptIdentity("owner", "repo", returned_number, "0"), DispatchOutcome.LOCAL_ACCEPTED, provider_reference="unrelated-job"),
    )
    monkeypatch.setattr(engine, "_check_and_handle_closed_branch", lambda _: True)
    monkeypatch.setattr(engine, "_create_candidate_from_single", lambda *_args, **_kwargs: candidate)
    monkeypatch.setattr(engine, "_preflight_explicit_issue_relationships", lambda *_args, **_kwargs: candidate.data)
    monkeypatch.setattr(engine, "_validate_submitted_parent_generation_for_child", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(engine, "_process_single_candidate_unified", lambda *_args, **_kwargs: processing)
    complete = Mock()
    monkeypatch.setattr(engine, "_complete_explicit_local_issue", complete)
    result = engine.process_single("owner/repo", "issue", 41, explicit_only=True)
    complete.assert_not_called()
    assert result["target_outcome"] == "failed"
    assert result["target_reason"] == f"Explicit target identity mismatch: requested issue #41, processed {returned_type} #{returned_number}"
