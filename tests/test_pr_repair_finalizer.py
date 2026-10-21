from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.github_pending_work import PendingReason, PendingWorkPersistenceError, PendingWorkScheduler, PendingWorkStore
from auto_coder.invocation_admission import InvocationAdmissionGate
from auto_coder.local_job_handoff import InvocationOutcome, LocalJobKind, LocalJobOffer, LocalJobState, LocalJobStore, PRRepairAdmission
from auto_coder.local_job_runner import LocalJobRunner
from auto_coder.local_pr_repair_recovery import (
    LOCAL_PR_REPAIR_FINALIZATION_EFFECT,
    LOCAL_PR_REPAIR_FINALIZATION_STAGE,
    LocalPRRepairFinalizationHandler,
    rediscover_pr_repair_finalizations,
)
from auto_coder.local_review_repair import LocalReviewRepairStore
from auto_coder.pr_repair_finalizer import PRRepairJobFinalizer, PRRepairPublicationDisposition, PRRepairTargetObservation
from auto_coder.pr_repair_workspace import PRRepairSource, PRRepairWorkspaceProducer


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


def repository(tmp_path: Path) -> tuple[Path, Path, str, str]:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.email", "test@example.com")
    git(source, "config", "user.name", "Test")
    (source / "shared.txt").write_text("base\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "base")
    base = git(source, "rev-parse", "HEAD")
    git(source, "switch", "-c", "feature")
    (source / "feature.txt").write_text("feature\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "feature")
    head = git(source, "rev-parse", "HEAD")
    remote = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(source, "remote", "add", "origin", str(remote))
    git(source, "push", "origin", "main", "feature")
    return source, remote, head, base


def completed_job(
    tmp_path: Path,
    source: Path,
    head: str,
    base: str,
    *,
    kind: LocalJobKind = LocalJobKind.PR_CI_FAILURE,
    target: int = 7,
    edit: bool = True,
):
    store = LocalJobStore(tmp_path / f"jobs-{target}.sqlite3")
    offer = LocalJobOffer(kind, "owner/repo", target, f"attempt-{target}", "codex", "repair")
    admission = PRRepairAdmission(
        "owner/repo",
        "feature",
        head,
        "owner/repo",
        "main",
        base,
        "ci_failure" if kind is LocalJobKind.PR_CI_FAILURE else "merge_conflict",
        f"evidence-{target}",
        f"upstream-{target}",
        ("checks / tests",) if kind is LocalJobKind.PR_CI_FAILURE else (),
        True,
        True,
        True,
        True,
        kind is LocalJobKind.PR_MERGE_CONFLICT,
        kind is LocalJobKind.PR_CI_FAILURE,
    )
    assert store.offer_pr_repair(offer, admission, LocalReviewRepairStore(tmp_path / f"review-{target}.sqlite3")) is not None
    claim = store.claim(offer.job_id, "runner")
    assert claim is not None and claim.acquired
    producer = PRRepairWorkspaceProducer(store, tmp_path / f"workspaces-{target}")
    prepared = producer.prepare(claim, PRRepairSource(source))
    git(prepared.workspace, "config", "user.email", "test@example.com")
    git(prepared.workspace, "config", "user.name", "Test")
    if kind is LocalJobKind.PR_MERGE_CONFLICT:
        subprocess.run(["git", "merge", "--no-commit", "--no-ff", base], cwd=prepared.workspace, check=True, capture_output=True)
    elif edit:
        (prepared.workspace / "feature.txt").write_text("repaired\n")
    manifest = producer.capture_result(
        claim,
        prepared,
        InvocationOutcome.COMPLETED,
        "" if kind is LocalJobKind.PR_MERGE_CONFLICT else "ACTION_SUMMARY: repaired",
        completion_source="controller_verified_merge" if kind is LocalJobKind.PR_MERGE_CONFLICT else "model",
    )
    artifact = store.persist_result_artifact(claim, InvocationOutcome.COMPLETED, manifest.to_json(), executor_token="runner")
    assert artifact is not None
    assert store.record_result(claim, InvocationOutcome.COMPLETED, artifact.artifact_id, executor_token="runner")
    return store, claim, prepared, artifact


def observer(remote: Path, base: str):
    def observe(repository: str, number: int) -> PRRepairTargetObservation:
        head = git(remote, "rev-parse", "refs/heads/feature")
        return PRRepairTargetObservation(repository, number, "owner/repo", "feature", head, "owner/repo", "main", base, True, False, True)

    return observe


@pytest.mark.parametrize("replacement", ["ancestor", "divergent"])
def test_finalizer_lease_refuses_changed_remote_without_retry(tmp_path: Path, _use_real_commands, replacement: str) -> None:
    source, remote, head, base = repository(tmp_path)
    store, claim, _prepared, _artifact = completed_job(tmp_path, source, head, base)
    if replacement == "ancestor":
        changed = base
    else:
        git(source, "switch", "main")
        (source / "other.txt").write_text("other\n")
        git(source, "add", ".")
        git(source, "commit", "-m", "other")
        changed = git(source, "rev-parse", "HEAD")
        git(source, "push", "origin", f"{changed}:refs/heads/race-object")
    calls = 0

    from auto_coder.git_commit import git_push as real_push

    def raced_push(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        git(remote, "update-ref", "refs/heads/feature", changed)
        return real_push(**kwargs)

    finalizer = PRRepairJobFinalizer(store, observer(remote, base), lambda *_args: True)
    with (
        patch("auto_coder.pr_repair_finalizer.git_push", side_effect=raced_push),
        patch("auto_coder.git_branch.try_llm_commit_push") as model,
    ):
        result = finalizer.finalize(claim.record.job_id)

    assert result.disposition is PRRepairPublicationDisposition.PENDING
    assert git(remote, "rev-parse", "refs/heads/feature") == changed
    assert calls == 1
    model.assert_not_called()
    # A retained ambiguous intent performs observation only on every restart.
    assert finalizer.finalize(claim.record.job_id).disposition in {
        PRRepairPublicationDisposition.PENDING,
        PRRepairPublicationDisposition.STALE,
    }
    assert calls == 1


@pytest.mark.parametrize("empty_merge", [False, True])
def test_finalizer_preserves_real_merge_parents_and_tree(tmp_path: Path, _use_real_commands, empty_merge: bool) -> None:
    source, remote, head, _old_base = repository(tmp_path)
    git(source, "switch", "main")
    if not empty_merge:
        (source / "shared.txt").write_text("advanced\n")
        git(source, "add", ".")
        git(source, "commit", "-m", "advance base")
    else:
        git(source, "commit", "--allow-empty", "-m", "empty base advance")
    base = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", "main")
    store, claim, prepared, _artifact = completed_job(tmp_path, source, head, base, kind=LocalJobKind.PR_MERGE_CONFLICT, target=8 + int(empty_merge))
    calls = 0

    from auto_coder.git_commit import git_push as real_push

    def counted_push(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return real_push(**kwargs)

    with patch("auto_coder.pr_repair_finalizer.git_push", side_effect=counted_push), patch("auto_coder.git_branch.try_llm_commit_push") as model:
        result = PRRepairJobFinalizer(store, observer(remote, base), lambda *_args: True).finalize(claim.record.job_id)

    assert result.disposition is PRRepairPublicationDisposition.PUBLISHED
    assert git(prepared.workspace, "show", "-s", "--format=%P", result.candidate_sha).split() == [head, base]
    assert git(remote, "rev-parse", "refs/heads/feature") == result.candidate_sha
    assert calls == 1
    model.assert_not_called()


def test_restart_recovers_commit_before_lost_receipt_once(tmp_path: Path, _use_real_commands, monkeypatch) -> None:
    source, remote, head, base = repository(tmp_path)
    store, claim, prepared, artifact = completed_job(tmp_path, source, head, base, target=10)
    original_record = store.record_effect
    lost = True

    def lose_commit_receipt(effect_claim, name, state, evidence=""):  # type: ignore[no-untyped-def]
        nonlocal lost
        if lost and name == "commit" and state == "completed":
            lost = False
            return False
        return original_record(effect_claim, name, state, evidence)

    monkeypatch.setattr(store, "record_effect", lose_commit_receipt)
    first = PRRepairJobFinalizer(store, observer(remote, base), lambda *_args: True).finalize(claim.record.job_id)
    candidate = git(prepared.workspace, "rev-parse", "HEAD")
    assert first.disposition is PRRepairPublicationDisposition.PENDING
    assert candidate != head

    reopened = LocalJobStore(store.path)
    with patch("auto_coder.pr_repair_finalizer.git_commit_with_retry") as commit_again:
        recovered = PRRepairJobFinalizer(reopened, observer(remote, base), lambda *_args: True).finalize(claim.record.job_id)
        repeated = PRRepairJobFinalizer(reopened, observer(remote, base), lambda *_args: True).finalize(claim.record.job_id)

    assert recovered.disposition is repeated.disposition is PRRepairPublicationDisposition.PUBLISHED
    assert recovered.candidate_sha == repeated.candidate_sha == candidate
    assert git(remote, "rev-parse", "refs/heads/feature") == candidate
    assert reopened.get_effect_strict(claim.record.job_id, claim.record.execution_incarnation, "commit").evidence.find(artifact.artifact_id) >= 0  # type: ignore[union-attr]
    commit_again.assert_not_called()


@pytest.mark.parametrize("kind", [LocalJobKind.PR_CI_FAILURE, LocalJobKind.PR_MERGE_CONFLICT])
def test_restart_reconciles_accepted_push_before_lost_receipt(tmp_path: Path, _use_real_commands, monkeypatch, kind: LocalJobKind) -> None:
    source, remote, head, base = repository(tmp_path)
    if kind is LocalJobKind.PR_MERGE_CONFLICT:
        git(source, "switch", "main")
        (source / "shared.txt").write_text("advanced\n")
        git(source, "add", ".")
        git(source, "commit", "-m", "advance base")
        base = git(source, "rev-parse", "HEAD")
        git(source, "push", "origin", "main")
    store, claim, prepared, _artifact = completed_job(tmp_path, source, head, base, kind=kind, target=11 + int(kind is LocalJobKind.PR_MERGE_CONFLICT))
    original_record = store.record_effect
    lose_receipt = True
    pushes = 0
    wakes: list[str] = []

    from auto_coder.git_commit import git_push as real_push
    from auto_coder.pr_repair_finalizer import git_commit_with_retry as real_commit

    def counted_push(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal pushes
        pushes += 1
        return real_push(**kwargs)

    def record_without_first_push_receipt(effect_claim, name, state, evidence=""):  # type: ignore[no-untyped-def]
        nonlocal lose_receipt
        if lose_receipt and name == "push" and state == "completed":
            lose_receipt = False
            return False
        return original_record(effect_claim, name, state, evidence)

    monkeypatch.setattr(store, "record_effect", record_without_first_push_receipt)
    wake = lambda _repository, _number, candidate: not wakes.append(candidate)
    with (
        patch("auto_coder.pr_repair_finalizer.git_push", side_effect=counted_push),
        patch("auto_coder.pr_repair_finalizer.git_commit_with_retry", wraps=real_commit) as commits,
        patch("auto_coder.git_branch.try_llm_commit_push") as model,
    ):
        interrupted = PRRepairJobFinalizer(store, observer(remote, base), wake).finalize(claim.record.job_id)
        reopened = LocalJobStore(store.path)
        startup_runner = LocalJobRunner(
            reopened,
            InvocationAdmissionGate(),
            repository="owner/repo",
            capacity=1,
            adapters={kind: MagicMock()},
        )
        startup_runner.recover_unsettled()
        startup_runner.close()
        foreign_offer = LocalJobOffer(kind, "other/repo", 91, "foreign-attempt", "codex", "foreign repair")
        foreign_admission = PRRepairAdmission(
            "other/repo",
            "feature",
            head,
            "other/repo",
            "main",
            base,
            "ci_failure" if kind is LocalJobKind.PR_CI_FAILURE else "merge_conflict",
            "foreign-evidence",
            "foreign-incarnation",
            ("checks / tests",) if kind is LocalJobKind.PR_CI_FAILURE else (),
            True,
            True,
            True,
            True,
            kind is LocalJobKind.PR_MERGE_CONFLICT,
            kind is LocalJobKind.PR_CI_FAILURE,
        )
        assert reopened.offer_pr_repair(foreign_offer, foreign_admission, LocalReviewRepairStore(tmp_path / "foreign-review.sqlite3")) is not None
        foreign_claim = reopened.claim(foreign_offer.job_id, "foreign-runner")
        assert foreign_claim is not None and reopened.mark_provider_entered(foreign_claim)
        foreign_artifact = reopened.persist_result_artifact(foreign_claim, InvocationOutcome.COMPLETED, "foreign-result")
        assert foreign_artifact is not None
        assert reopened.record_result(foreign_claim, InvocationOutcome.COMPLETED, foreign_artifact.artifact_id)
        foreign_before = reopened.get(foreign_offer.job_id)
        recovered = PRRepairJobFinalizer(reopened, observer(remote, base), wake)
        pending = PendingWorkStore(tmp_path / "pending.sqlite3")
        identities = rediscover_pr_repair_finalizations("owner/repo", reopened, pending)
        assert len(identities) == 1
        obligation = pending.get(identities[0])
        assert obligation is not None
        handler = LocalPRRepairFinalizationHandler("owner/repo", reopened, recovered)
        first_outcome = handler.dispatch(obligation)
        pending.complete_effect(identities[0], first_outcome.completed_effects[0])
        second_outcome = handler.recover(obligation)

    candidate = git(prepared.workspace, "rev-parse", "HEAD")
    assert interrupted.disposition is PRRepairPublicationDisposition.PENDING
    assert first_outcome.completed_effects == second_outcome.completed_effects == (LOCAL_PR_REPAIR_FINALIZATION_EFFECT,)
    assert pending.get(identities[0]) is None
    assert git(remote, "rev-parse", "refs/heads/feature") == candidate
    assert pushes == 1
    assert commits.call_count == 1
    assert wakes == [candidate]
    settled = reopened.get(claim.record.job_id)
    assert settled is not None and settled.state is LocalJobState.SETTLED
    assert settled.execution_incarnation == claim.record.execution_incarnation
    assert reopened.get(foreign_offer.job_id) == foreign_before
    model.assert_not_called()


def test_same_finalization_controller_retries_after_outcome_store_and_observation_recover(tmp_path: Path, _use_real_commands, monkeypatch) -> None:
    source, remote, head, base = repository(tmp_path)
    jobs, claim, prepared, _artifact = completed_job(tmp_path, source, head, base, target=19)
    pending = PendingWorkStore(tmp_path / "pending.sqlite3")
    identity = rediscover_pr_repair_finalizations("owner/repo", jobs, pending)[0]
    observation_available = False
    observation_calls = 0

    def temporarily_failing_observer(repository_name: str, number: int) -> PRRepairTargetObservation:
        nonlocal observation_calls
        observation_calls += 1
        if not observation_available:
            raise RuntimeError("temporary observation failure")
        return observer(remote, base)(repository_name, number)

    wakes: list[str] = []
    finalizer = PRRepairJobFinalizer(jobs, temporarily_failing_observer, lambda _repository, _number, candidate: not wakes.append(candidate))
    handler = LocalPRRepairFinalizationHandler("owner/repo", jobs, finalizer)
    scheduler = PendingWorkScheduler(pending, repository="owner/repo", poll_interval=0.01)
    scheduler.register_handler(LOCAL_PR_REPAIR_FINALIZATION_STAGE, handler)
    retained = pending.get(identity)
    assert retained is not None
    original_mark_waiting = PendingWorkStore.mark_waiting
    failed_writes = 0

    def fail_first_outcome_write(store: PendingWorkStore, work_identity):  # type: ignore[no-untyped-def]
        nonlocal failed_writes, observation_available
        if work_identity == identity and failed_writes == 0:
            failed_writes += 1
            observation_available = True
            raise PendingWorkPersistenceError("temporary outcome write failure")
        return original_mark_waiting(store, work_identity)

    monkeypatch.setattr(PendingWorkStore, "mark_waiting", fail_first_outcome_write)

    async def run_same_controller() -> None:
        shutdown = asyncio.Event()
        task = asyncio.create_task(scheduler.run(shutdown))
        for _ in range(500):
            current = jobs.get(claim.record.job_id)
            if current is not None and current.state is LocalJobState.SETTLED and pending.get(identity) is None:
                break
            await asyncio.sleep(0.01)
        shutdown.set()
        await task

    asyncio.run(run_same_controller())

    assert failed_writes == 1
    assert pending.get(identity) is None
    settled = jobs.get(claim.record.job_id)
    assert settled is not None and settled.state is LocalJobState.SETTLED
    assert settled.execution_incarnation == claim.record.execution_incarnation
    assert git(remote, "rev-parse", "refs/heads/feature") == git(prepared.workspace, "rev-parse", "HEAD")
    assert observation_calls >= 3
    assert len(wakes) == 1


def test_operationally_blocked_finalization_waits_for_explicit_retry(tmp_path: Path, _use_real_commands) -> None:
    source, remote, head, base = repository(tmp_path)
    jobs, claim, _prepared, _artifact = completed_job(tmp_path, source, head, base, target=20)
    pending = PendingWorkStore(tmp_path / "pending.sqlite3")
    identity = rediscover_pr_repair_finalizations("owner/repo", jobs, pending)[0]
    pending.block(identity, PendingReason.AUTHENTICATION, "operator credentials required")
    wakes: list[str] = []
    handler = LocalPRRepairFinalizationHandler(
        "owner/repo",
        jobs,
        PRRepairJobFinalizer(jobs, observer(remote, base), lambda _repository, _number, candidate: not wakes.append(candidate)),
    )
    scheduler = PendingWorkScheduler(pending, repository="owner/repo", poll_interval=0.01)
    scheduler.register_handler(LOCAL_PR_REPAIR_FINALIZATION_STAGE, handler)

    async def run_controller() -> None:
        shutdown = asyncio.Event()
        task = asyncio.create_task(scheduler.run(shutdown))
        await asyncio.sleep(0.05)
        assert git(remote, "rev-parse", "refs/heads/feature") == head
        assert jobs.get(claim.record.job_id).state is LocalJobState.RESULT_RECORDED
        blocked = pending.get(identity)
        assert blocked is not None and blocked.reason is PendingReason.AUTHENTICATION
        assert scheduler.manual_retry(identity) is not None
        for _ in range(500):
            current = jobs.get(claim.record.job_id)
            if current is not None and current.state is LocalJobState.SETTLED and pending.get(identity) is None:
                break
            await asyncio.sleep(0.01)
        shutdown.set()
        await task

    asyncio.run(run_controller())

    settled = jobs.get(claim.record.job_id)
    assert settled is not None and settled.state is LocalJobState.SETTLED
    assert settled.execution_incarnation == claim.record.execution_incarnation
    assert len(wakes) == 1


def test_commit_hook_index_rewrite_cannot_be_checkpointed_or_pushed(tmp_path: Path, _use_real_commands) -> None:
    source, remote, head, base = repository(tmp_path)
    store, claim, prepared, _artifact = completed_job(tmp_path, source, head, base, target=12)
    hook = prepared.workspace / git(prepared.workspace, "rev-parse", "--git-path", "hooks") / "post-commit"
    hook.write_text(
        "#!/bin/sh\n" "blob=$(printf evil | git hash-object -w --stdin)\n" "git update-index --cacheinfo 100644,$blob,feature.txt\n" "tree=$(git write-tree)\n" "parent=$(git rev-parse HEAD^)\n" "candidate=$(printf foreign | git commit-tree $tree -p $parent)\n" "git update-ref HEAD $candidate\n"
    )
    hook.chmod(0o755)

    with patch("auto_coder.pr_repair_finalizer.git_push") as push:
        result = PRRepairJobFinalizer(store, observer(remote, base), lambda *_args: True).finalize(claim.record.job_id)

    assert result.disposition is PRRepairPublicationDisposition.PENDING
    assert git(prepared.workspace, "rev-parse", "HEAD") != head
    assert store.get_effect_strict(claim.record.job_id, claim.record.execution_incarnation, "commit").state == "pending"  # type: ignore[union-attr]
    push.assert_not_called()
    assert git(remote, "rev-parse", "refs/heads/feature") == head


def test_pending_intent_rejects_same_parent_wrong_candidate_tree(tmp_path: Path, _use_real_commands) -> None:
    source, remote, head, base = repository(tmp_path)
    store, claim, prepared, artifact = completed_job(tmp_path, source, head, base, target=13)
    intent = store.claim_effect_intent_if_absent(
        claim,
        "commit",
        PRRepairJobFinalizer._evidence(artifact_id=artifact.artifact_id, expected_parents=[head], changed_files=["feature.txt"], head_ref="feature"),
    )
    assert intent.inserted
    wrong = git(prepared.workspace, "commit-tree", f"{head}^{{tree}}", "-p", head, "-m", "foreign candidate")
    git(prepared.workspace, "update-ref", "HEAD", wrong)

    with patch("auto_coder.pr_repair_finalizer.git_push") as push:
        result = PRRepairJobFinalizer(LocalJobStore(store.path), observer(remote, base), lambda *_args: True).finalize(claim.record.job_id)

    assert result.disposition is PRRepairPublicationDisposition.PENDING
    assert store.get_effect_strict(claim.record.job_id, claim.record.execution_incarnation, "commit").state == "pending"  # type: ignore[union-attr]
    push.assert_not_called()


def test_changed_origin_push_url_is_refused_before_commit_or_push(tmp_path: Path, _use_real_commands) -> None:
    source, remote, head, base = repository(tmp_path)
    store, claim, prepared, _artifact = completed_job(tmp_path, source, head, base, target=14)
    unrelated = tmp_path / "unrelated.git"
    git(tmp_path, "clone", "--bare", str(remote), str(unrelated))
    git(prepared.workspace, "remote", "set-url", "--push", "origin", str(unrelated))

    with patch("auto_coder.pr_repair_finalizer.git_push") as push, patch("auto_coder.pr_repair_finalizer.git_commit_with_retry") as commit:
        result = PRRepairJobFinalizer(store, observer(remote, base), lambda *_args: True).finalize(claim.record.job_id)

    assert result.disposition is PRRepairPublicationDisposition.REFUSED
    assert git(remote, "rev-parse", "refs/heads/feature") == head
    assert git(unrelated, "rev-parse", "refs/heads/feature") == head
    commit.assert_not_called()
    push.assert_not_called()
