from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import auto_coder.issue_job_finalizer as finalizer_module
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.issue_dispatch import CandidateHandoff, IssueAttemptIdentity, IssueDispatchGuard
from auto_coder.issue_job_finalizer import IssueJobFinalizer
from auto_coder.issue_job_workspace import IssueJobSource, IssueJobWorkspaceProducer
from auto_coder.local_job_handoff import InvocationOutcome, LocalJobKind, LocalJobOffer, LocalJobState, LocalJobStore

from .test_issue_job_workspace import claim, git, repository


def _completed_job(tmp_path: Path, number: int = 41, *, tracked_ignored: bool = False, change: bool = True):
    source, head = repository(tmp_path)
    if tracked_ignored:
        context = source / ".auto-coder" / "context.md"
        context.parent.mkdir()
        context.write_text("tracked context\n")
        git(source, "add", ".auto-coder/context.md")
        source.joinpath(".gitignore").write_text(".auto-coder/\n")
        git(source, "add", ".gitignore")
    source.joinpath("baseline.txt").write_text("baseline\n", encoding="utf-8")
    git(source, "add", "baseline.txt")
    git(source, "commit", "-m", "add baseline")
    head = git(source, "rev-parse", "HEAD")
    remote = tmp_path / "publication.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(source, "remote", "set-url", "origin", str(remote))
    git(source, "push", "origin", "main")
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    acquired = claim(store, number)
    slots = ImplementationSlotRepository("owner/repo", 2, tmp_path / "slots.json", tmp_path / "retired.json")
    assert slots.reserve_new(ImplementationOwner("issue", number))

    def edit(workspace: Path, _prompt: str) -> str:
        git(workspace, "config", "user.email", "test@example.com")
        git(workspace, "config", "user.name", "Test")
        if change:
            (workspace / "value.txt").write_text("implemented\n", encoding="utf-8")
        if tracked_ignored:
            (workspace / ".auto-coder" / "context.md").write_text("updated tracked context\n")
            (workspace / ".auto-coder" / "runtime.txt").write_text("ignored runtime output\n")
        return "ACTION_SUMMARY: implemented exact result"

    checkpoint = IssueJobWorkspaceProducer(store, tmp_path / "workspaces", slots).execute(acquired, IssueJobSource("owner/repo", source, "main", f"issue-{number}"), edit)
    return source, remote, store, slots, checkpoint


def test_tracked_ignored_context_is_published_without_ignored_runtime_output(tmp_path: Path, _use_real_commands) -> None:
    source, remote, store, slots, checkpoint = _completed_job(tmp_path, 46, tracked_ignored=True)
    existing = {"number": 93, "head": {"ref": checkpoint.work_branch}, "body": "Closes #46"}
    result = IssueJobFinalizer(store, slots, lambda *_: existing, lambda *_: None).finalize(checkpoint.job_id)

    assert result.disposition == "published", result.diagnostic
    assert git(remote, "show", "issue-46:.auto-coder/context.md") == "updated tracked context"
    assert git(remote, "show", "issue-46:value.txt") == "implemented"
    assert ".auto-coder/runtime.txt" not in git(remote, "ls-tree", "-r", "--name-only", "issue-46").splitlines()
    assert (source / ".auto-coder" / "context.md").read_text() == "tracked context\n"


def test_lost_pr_create_response_is_reconciled_without_duplicate(tmp_path: Path, _use_real_commands) -> None:
    source, remote, store, slots, checkpoint = _completed_job(tmp_path)
    observed: dict[str, object] = {}
    creates = 0

    def lookup(_repository: str, branch: str):
        return observed.get(branch)

    def create(record, _summary: str) -> None:
        nonlocal creates
        creates += 1
        observed[record.work_branch] = {"number": 91, "head": {"ref": record.work_branch}, "body": f"Closes #{record.target_number}"}
        raise TimeoutError("response lost after remote creation")

    finalizer = IssueJobFinalizer(store, slots, lookup, create)
    result = finalizer.finalize(checkpoint.job_id)

    assert result.disposition == "published"
    assert result.pr_number == 91
    assert creates == 1
    assert store.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "commit").state == "completed"  # type: ignore[union-attr]
    assert store.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "push").state == "completed"  # type: ignore[union-attr]
    assert store.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "pr").evidence == "91"  # type: ignore[union-attr]
    assert subprocess.run(["git", "rev-parse", "--verify", "issue-41"], cwd=source, capture_output=True).returncode != 0
    assert git(remote, "rev-parse", "issue-41") == git(checkpoint.workspace, "rev-parse", "HEAD")
    assert finalizer.resume_all("owner/repo") == ()
    shutil.rmtree(checkpoint.workspace)
    replay = finalizer.finalize(checkpoint.job_id)
    assert replay.disposition == "published"
    assert replay.pr_number == 91
    assert creates == 1


def test_unfinished_publication_reopens_stores_without_repeating_push(tmp_path: Path, monkeypatch, _use_real_commands) -> None:
    _source, _remote, store, slots, checkpoint = _completed_job(tmp_path, 44)
    original = store.get(checkpoint.job_id)
    assert original is not None and original.execution_incarnation
    real_push = finalizer_module.git_push
    pushes = 0

    def counted_push(**kwargs):
        nonlocal pushes
        pushes += 1
        return real_push(**kwargs)

    monkeypatch.setattr(finalizer_module, "git_push", counted_push)

    def unavailable_lookup(_repository: str, _branch: str):
        raise TimeoutError("GitHub unavailable after confirmed push")

    first = IssueJobFinalizer(store, slots, unavailable_lookup, lambda *_args: None).finalize(checkpoint.job_id)
    retained = store.get(checkpoint.job_id)
    assert first.disposition == "pending"
    assert retained is not None and retained.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING
    assert retained.execution_incarnation == original.execution_incarnation
    assert pushes == 1

    reopened_store = LocalJobStore(store.path)
    reopened_slots = ImplementationSlotRepository(
        "owner/repo",
        1,
        slots.storage_path,
        slots.retired_storage_path,
    )
    observed: dict[str, object] = {}
    creates = 0

    def lookup(_repository: str, branch: str):
        return observed.get(branch)

    def create(record, _summary: str) -> None:
        nonlocal creates
        creates += 1
        observed[record.work_branch] = {
            "number": 94,
            "head": {"ref": record.work_branch},
            "body": f"Closes #{record.target_number}",
        }

    resumed = IssueJobFinalizer(reopened_store, reopened_slots, lookup, create).resume_all("owner/repo")

    assert len(resumed) == 1
    assert resumed[0].job_id == checkpoint.job_id
    assert resumed[0].disposition == "published"
    assert resumed[0].pr_number == 94
    assert pushes == 1
    assert creates == 1
    settled = reopened_store.get(checkpoint.job_id)
    assert settled is not None and settled.execution_incarnation == original.execution_incarnation
    assert reopened_store.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "push").state == "completed"  # type: ignore[union-attr]
    assert reopened_store.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "pr").evidence == "94"  # type: ignore[union-attr]


def test_settled_no_change_replays_after_workspace_disposal(tmp_path: Path, _use_real_commands) -> None:
    _source, remote, store, slots, checkpoint = _completed_job(tmp_path, 47, change=False)

    def unexpected_publication(*_args):
        raise AssertionError("No-change jobs must not publish")

    finalizer = IssueJobFinalizer(store, slots, unexpected_publication, unexpected_publication)
    assert finalizer.finalize(checkpoint.job_id).disposition == "no_change"
    shutil.rmtree(checkpoint.workspace)
    assert finalizer.finalize(checkpoint.job_id).disposition == "no_change"
    assert git(remote, "branch", "--list", "issue-47") == ""
    assert store.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "push") is None


def test_no_change_receipt_write_failure_does_not_settle_success(tmp_path: Path, monkeypatch, _use_real_commands) -> None:
    _source, remote, store, slots, checkpoint = _completed_job(tmp_path, 49, change=False)
    original_record_effect = store.record_effect
    failed = False

    def fail_first_commit_receipt(claim, name: str, state: str, evidence: str) -> bool:
        nonlocal failed
        if name == "commit" and not failed:
            failed = True
            return False
        return original_record_effect(claim, name, state, evidence)

    monkeypatch.setattr(store, "record_effect", fail_first_commit_receipt)

    def unexpected_publication(*_args):
        raise AssertionError("No-change jobs must not publish")

    first = IssueJobFinalizer(store, slots, unexpected_publication, unexpected_publication).finalize(checkpoint.job_id)

    assert first.disposition == "pending"
    assert first.diagnostic == "no-change commit checkpoint failed"
    assert store.get(checkpoint.job_id).state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING  # type: ignore[union-attr]
    assert store.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "commit") is None
    assert store.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "publication") is None

    reopened = LocalJobStore(store.path)
    replay = IssueJobFinalizer(reopened, slots, unexpected_publication, unexpected_publication).finalize(checkpoint.job_id)

    assert replay.disposition == "no_change"
    assert reopened.get(checkpoint.job_id).state is LocalJobState.SETTLED  # type: ignore[union-attr]
    assert reopened.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "commit").state == "skipped"  # type: ignore[union-attr]
    assert reopened.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "publication").state == "skipped"  # type: ignore[union-attr]
    assert git(remote, "branch", "--list", "issue-49") == ""


def test_settled_completed_job_without_publication_receipts_is_not_success(tmp_path: Path) -> None:
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    acquired = claim(store, 48)
    artifact = store.persist_result_artifact(acquired, InvocationOutcome.COMPLETED, "ACTION_SUMMARY: implemented")
    assert artifact is not None
    assert store.record_result(acquired, InvocationOutcome.COMPLETED, artifact.artifact_id)
    assert store.mark_downstream_pending(acquired)
    assert store.settle(acquired, "pr:91")
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json", tmp_path / "retired.json")

    def unexpected_publication(*_args):
        raise AssertionError("Settled jobs must not repeat publication")

    result = IssueJobFinalizer(store, slots, unexpected_publication, unexpected_publication).finalize(acquired.record.job_id)
    assert result.disposition == "pending"
    assert result.pr_number is None
    assert result.diagnostic == "settled job has no confirmed publication receipt"


def test_unconfirmed_push_stays_pending_and_replay_does_not_create_pr(tmp_path: Path, monkeypatch, _use_real_commands) -> None:
    _source, _remote, store, slots, checkpoint = _completed_job(tmp_path, 42)
    calls = 0

    def failed_push(**_kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(success=False, stderr="network result unknown")

    monkeypatch.setattr("auto_coder.issue_job_finalizer.git_push", failed_push)
    finalizer = IssueJobFinalizer(store, slots, lambda *_: None, lambda *_: (_ for _ in ()).throw(AssertionError("PR must not be created")))

    first = finalizer.finalize(checkpoint.job_id)
    second = IssueJobFinalizer(LocalJobStore(store.path), slots, lambda *_: None, lambda *_: None).finalize(checkpoint.job_id)

    assert first.disposition == second.disposition == "pending"
    assert calls == 2
    assert store.get(checkpoint.job_id).invocation_outcome is InvocationOutcome.COMPLETED  # type: ignore[union-attr]
    assert store.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "commit").state == "completed"  # type: ignore[union-attr]
    assert store.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "push").state == "indeterminate"  # type: ignore[union-attr]
    assert store.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "pr") is None


def test_cannot_fix_never_touches_git_or_publication(tmp_path: Path) -> None:
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    acquired = claim(store, 43)
    artifact = store.persist_result_artifact(acquired, InvocationOutcome.CANNOT_FIX, "CANNOT_FIX")
    assert artifact is not None
    assert store.record_result(acquired, InvocationOutcome.CANNOT_FIX, artifact.artifact_id)
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json", tmp_path / "retired.json")
    finalizer = IssueJobFinalizer(store, slots, lambda *_: (_ for _ in ()).throw(AssertionError("lookup called")), lambda *_: None)

    result = finalizer.finalize(acquired.record.job_id)

    assert result.disposition == "cannot_fix"
    assert finalizer.finalize(acquired.record.job_id).disposition == "cannot_fix"
    assert store.get(acquired.record.job_id).state.value == "settled"  # type: ignore[union-attr]


@pytest.mark.parametrize("replacement", [False, True])
def test_failed_preparation_releases_only_transferred_execution(tmp_path: Path, replacement: bool) -> None:
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json", tmp_path / "retired.json")
    owner = ImplementationOwner("issue", 49)
    execution = slots.start_execution(owner, generation="generation")
    assert execution
    identity = IssueAttemptIdentity("owner", "repo", 49, "0")
    guard = IssueDispatchGuard(tmp_path / "dispatch.sqlite3")
    assert guard.reserve(identity, CandidateHandoff("codex", "local")).admitted
    offered = store.offer_issue(LocalJobOffer(LocalJobKind.ISSUE_IMPLEMENTATION, "owner/repo", 49, "0", "codex", "implement", execution), identity, guard)
    assert offered is not None
    acquired = store.claim(offered.job_id)
    assert acquired is not None
    artifact = store.persist_result_artifact(acquired, InvocationOutcome.FAILED, "remote fetch failed")
    assert artifact is not None
    assert store.record_result(acquired, InvocationOutcome.FAILED, artifact.artifact_id)
    current = execution
    if replacement:
        slots.release(owner)
        current = slots.start_execution(owner, generation="replacement")
        assert current and current != execution

    def unexpected_publication(*_args):
        raise AssertionError("Failed preparation must not publish")

    result = IssueJobFinalizer(store, slots, unexpected_publication, unexpected_publication).finalize(offered.job_id)
    assert result.disposition == "failed"
    assert slots.active_execution_ids(owner) == ((current,) if replacement else ())
    job = store.get(offered.job_id)
    assert job is not None
    assert job.state is LocalJobState.SETTLED
    assert not job.provider_entered
    assert job.workspace_path == job.owner_incarnation == ""
    assert store.get_effect(job.job_id, job.execution_incarnation, "push") is None


@pytest.mark.parametrize(("target", "closed_issue"), [(4, 41), (41, 4)])
def test_same_branch_pr_for_different_issue_is_not_attributed(
    tmp_path: Path,
    _use_real_commands,
    target: int,
    closed_issue: int,
) -> None:
    _source, _remote, store, slots, checkpoint = _completed_job(tmp_path, target)
    creates = 0

    def create(*_args) -> None:
        nonlocal creates
        creates += 1

    existing = {
        "number": 77,
        "head": {"ref": checkpoint.work_branch},
        "body": f"Closes #{closed_issue}",
    }
    result = IssueJobFinalizer(store, slots, lambda *_: existing, create).finalize(checkpoint.job_id)

    assert result.disposition == "pending"
    assert result.pr_number is None
    assert creates == 0
    owner = next(item for item in slots.snapshot().owners if item.owner == ImplementationOwner("issue", target))  # type: ignore[union-attr]
    assert owner.implementation_prs == ()
    assert store.get(checkpoint.job_id).state.value == "downstream_effects_pending"  # type: ignore[union-attr]
    assert store.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "pr").state == "indeterminate"  # type: ignore[union-attr]
    assert store.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "association") is None


def test_restart_recovers_commit_created_before_its_checkpoint(tmp_path: Path, monkeypatch, _use_real_commands) -> None:
    _source, _remote, store, slots, checkpoint = _completed_job(tmp_path, 44)
    git(checkpoint.workspace, "add", "-A")
    git(checkpoint.workspace, "commit", "-m", "controller commit before crash")
    existing = {"number": 92, "head": {"ref": checkpoint.work_branch}, "body": "Closes #44"}
    monkeypatch.setattr("auto_coder.issue_job_finalizer.git_commit_with_retry", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not recommit")))

    result = IssueJobFinalizer(LocalJobStore(store.path), slots, lambda *_: existing, lambda *_: None).finalize(checkpoint.job_id)

    assert result.disposition == "published"
    assert store.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "commit").evidence == git(checkpoint.workspace, "rev-parse", "HEAD")  # type: ignore[union-attr]


def test_replacement_owner_fences_old_completed_job_before_git_effects(tmp_path: Path, monkeypatch, _use_real_commands) -> None:
    _source, remote, store, slots, checkpoint = _completed_job(tmp_path, 45)
    owner = ImplementationOwner("issue", 45)
    old_incarnation = slots.owner_incarnation(owner)
    slots.release(owner)
    assert slots.reserve_new(owner)
    assert slots.owner_incarnation(owner) != old_incarnation
    monkeypatch.setattr("auto_coder.issue_job_finalizer.git_commit_with_retry", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("stale job committed")))

    result = IssueJobFinalizer(LocalJobStore(store.path), slots, lambda *_: None, lambda *_: (_ for _ in ()).throw(AssertionError("stale job created PR"))).finalize(checkpoint.job_id)

    assert result.disposition == "pending"
    assert "owner" in result.diagnostic
    assert subprocess.run(["git", "rev-parse", "--verify", checkpoint.work_branch], cwd=remote, capture_output=True).returncode != 0
    current = next(item for item in slots.snapshot().owners if item.owner == owner)  # type: ignore[union-attr]
    assert current.implementation_prs == ()
    assert store.get(checkpoint.job_id).state is LocalJobState.RESULT_RECORDED  # type: ignore[union-attr]


@pytest.mark.parametrize("mutation", ["untracked", "tracked", "mode", "symlink"])
def test_post_checkpoint_workspace_drift_is_rejected(tmp_path: Path, mutation: str, _use_real_commands) -> None:
    _source, remote, store, slots, checkpoint = _completed_job(tmp_path, 46)
    if mutation == "untracked":
        checkpoint.workspace.joinpath("later.txt").write_text("unconfirmed\n", encoding="utf-8")
    elif mutation == "tracked":
        checkpoint.workspace.joinpath("baseline.txt").write_text("unconfirmed\n", encoding="utf-8")
    elif mutation == "mode":
        checkpoint.workspace.joinpath("value.txt").chmod(0o755)
    else:
        checkpoint.workspace.joinpath("value.txt").unlink()
        checkpoint.workspace.joinpath("value.txt").symlink_to("AGENTS.md")

    result = IssueJobFinalizer(store, slots, lambda *_: None, lambda *_: (_ for _ in ()).throw(AssertionError("drift published"))).finalize(checkpoint.job_id)

    assert result.disposition == "pending"
    assert "differs" in result.diagnostic
    assert subprocess.run(["git", "rev-parse", "--verify", checkpoint.work_branch], cwd=remote, capture_output=True).returncode != 0
    assert store.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "commit") is None


def test_unknown_pr_creation_is_reconciled_without_second_create(tmp_path: Path, _use_real_commands) -> None:
    _source, _remote, store, slots, checkpoint = _completed_job(tmp_path, 47)
    creates = 0

    def create(*_args) -> None:
        nonlocal creates
        creates += 1
        raise TimeoutError("response lost")

    first = IssueJobFinalizer(store, slots, lambda *_: None, create).finalize(checkpoint.job_id)
    reopened = LocalJobStore(store.path)
    second = IssueJobFinalizer(reopened, slots, lambda *_: None, create).finalize(checkpoint.job_id)
    closed = {"number": 93, "head": {"ref": checkpoint.work_branch}, "body": "Closes #47", "state": "closed"}
    recovered = IssueJobFinalizer(LocalJobStore(store.path), slots, lambda *_: closed, create).finalize(checkpoint.job_id)

    assert first.disposition == second.disposition == "pending"
    assert creates == 1
    assert recovered.disposition == "published"
    assert recovered.pr_number == 93


def test_pre_create_lookup_outage_does_not_permanently_fence_creation(tmp_path: Path, _use_real_commands) -> None:
    _source, _remote, store, slots, checkpoint = _completed_job(tmp_path, 48)
    creates = 0
    created = False

    def unavailable(*_args):
        raise RuntimeError("GitHub unavailable before create")

    first = IssueJobFinalizer(store, slots, unavailable, lambda *_: (_ for _ in ()).throw(AssertionError("create called without lookup authority"))).finalize(checkpoint.job_id)

    def lookup(_repository: str, _branch: str):
        if created:
            return {"number": 94, "head": {"ref": checkpoint.work_branch}, "body": "Closes #48"}
        return None

    def create(*_args) -> None:
        nonlocal creates, created
        creates += 1
        created = True

    resumed = IssueJobFinalizer(LocalJobStore(store.path), slots, lookup, create).finalize(checkpoint.job_id)

    assert first.disposition == "pending"
    assert store.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "pr_lookup").state == "indeterminate"  # type: ignore[union-attr]
    assert creates == 1
    assert resumed.disposition == "published"


def test_post_create_lookup_outage_fences_repeated_creation(tmp_path: Path, _use_real_commands) -> None:
    _source, _remote, store, slots, checkpoint = _completed_job(tmp_path, 49)
    creates = 0
    lookup_calls = 0

    def lookup(_repository: str, _branch: str):
        nonlocal lookup_calls
        lookup_calls += 1
        if lookup_calls == 1:
            return None
        raise RuntimeError("response unavailable after create")

    def create(*_args) -> None:
        nonlocal creates
        creates += 1

    first = IssueJobFinalizer(store, slots, lookup, create).finalize(checkpoint.job_id)
    second = IssueJobFinalizer(LocalJobStore(store.path), slots, lambda *_: None, create).finalize(checkpoint.job_id)

    assert first.disposition == second.disposition == "pending"
    assert creates == 1
    assert store.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "pr").state == "indeterminate"  # type: ignore[union-attr]


def test_post_checkpoint_generated_cache_does_not_enter_publication(tmp_path: Path, _use_real_commands) -> None:
    _source, remote, store, slots, checkpoint = _completed_job(tmp_path, 48)
    generated = checkpoint.workspace / "node_modules" / "runtime.txt"
    generated.parent.mkdir()
    generated.write_text("unconfirmed generated output\n")
    existing = {"number": 94, "head": {"ref": checkpoint.work_branch}, "body": "Closes #48"}

    result = IssueJobFinalizer(store, slots, lambda *_: existing, lambda *_: None).finalize(checkpoint.job_id)

    assert result.disposition == "published"
    assert result.pr_number == 94
    assert git(remote, "show", f"{checkpoint.work_branch}:value.txt") == "implemented"
    assert "node_modules/runtime.txt" not in git(remote, "ls-tree", "-r", "--name-only", checkpoint.work_branch).splitlines()
    assert store.get(checkpoint.job_id).state is LocalJobState.SETTLED
