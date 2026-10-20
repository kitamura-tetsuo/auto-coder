from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from auto_coder.conflict_resolver import inspect_conflicts
from auto_coder.local_conflict_repair_job import LocalConflictRepairJobAdapter
from auto_coder.local_job_handoff import InvocationOutcome, LocalJobKind, LocalJobOffer, LocalJobStore, PRRepairAdmission
from auto_coder.local_review_repair import LocalReviewRepairStore
from auto_coder.pr_repair_workspace import PRRepairSource, PRRepairWorkspaceProducer


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, check=check, capture_output=True, text=True)


def history(tmp_path: Path, *, conflict: bool = True) -> tuple[Path, str, str]:
    root = tmp_path / "controller"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "shared.txt").write_text("original\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    git(root, "switch", "-c", "feature")
    (root / "shared.txt").write_text("feature\n")
    git(root, "commit", "-am", "feature")
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    git(root, "switch", "main")
    path = root / ("shared.txt" if conflict else "base.txt")
    path.write_text("base\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    base = git(root, "rev-parse", "HEAD").stdout.strip()
    remote = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "origin", "main", "feature")
    return root, head, base


def adapter(tmp_path: Path, root: Path, head: str, base: str, editor):  # type: ignore[no-untyped-def]
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    offer = LocalJobOffer(LocalJobKind.PR_MERGE_CONFLICT, "owner/repo", 7, "attempt", "codex", "{}")
    admission = PRRepairAdmission(
        "owner/repo",
        "feature",
        head,
        "owner/repo",
        "main",
        base,
        "merge_conflict",
        "evidence",
        "upstream",
        pr_open=True,
        pr_unmerged=True,
        local_route=True,
        owner_allowed=True,
        conflict_eligible=True,
    )
    job = store.offer_pr_repair(offer, admission, LocalReviewRepairStore(tmp_path / "reviews.sqlite3"))
    assert job is not None
    claim = store.claim(job.job_id, "runner")
    assert claim is not None and claim.acquired
    metadata = {
        "state": "open",
        "merged": False,
        "title": "Conflict",
        "body": "",
        "head": {"repo": {"full_name": "owner/repo"}, "ref": "feature", "sha": head},
        "base": {"repo": {"full_name": "owner/repo"}, "ref": "main", "sha": base},
    }
    instance = LocalConflictRepairJobAdapter(
        store,
        PRRepairSource(root),
        lambda _repo, _number: metadata,
        lambda _job, _metadata: True,
        lambda _backend, _job: editor,
        workspace_producer=PRRepairWorkspaceProducer(store, tmp_path / "workspaces"),
    )
    return store, claim, metadata, instance


def test_conflicted_exact_head_is_edited_and_checkpointed_without_publication(tmp_path: Path, _use_real_commands) -> None:
    root, head, base = history(tmp_path)
    peer_head = git(root, "rev-parse", "HEAD").stdout.strip()
    calls: list[Path] = []

    def edit(_prompt: str, workspace: Path) -> str:
        calls.append(workspace)
        assert "<<<<<<<" in (workspace / "shared.txt").read_text()
        (workspace / "shared.txt").write_text("resolved\n")
        return "ACTION_SUMMARY: resolved"

    store, claim, _metadata, instance = adapter(tmp_path, root, head, base, edit)
    result = instance.invoke_at_provider_entry(claim.record, lambda: store.mark_provider_entered(claim))
    manifest = json.loads(result.output)

    assert result.outcome is InvocationOutcome.COMPLETED
    assert len(calls) == 1
    assert manifest["head_sha"] == head and manifest["base_sha"] == base
    assert manifest["merge_head"] == base and manifest["completion_source"] == "model"
    assert git(calls[0], "rev-parse", "HEAD").stdout.strip() == head
    assert git(root, "rev-parse", "HEAD").stdout.strip() == peer_head
    assert (root / "shared.txt").read_text() == "base\n"


def test_changed_authority_before_editor_refuses_entry(tmp_path: Path, _use_real_commands) -> None:
    root, head, base = history(tmp_path)
    calls = 0

    def edit(_prompt: str, _workspace: Path) -> str:
        nonlocal calls
        calls += 1
        return "unexpected"

    store, claim, metadata, instance = adapter(tmp_path, root, head, base, edit)
    reads = 0

    def changing(_repo: str, _number: int):
        nonlocal reads
        reads += 1
        if reads == 1:
            return metadata
        return metadata | {"base": metadata["base"] | {"sha": "changed"}}

    instance._metadata_reader = changing
    assert instance.authorize_provider_entry(claim.record)
    try:
        instance.invoke_at_provider_entry(claim.record, lambda: store.mark_provider_entered(claim))
        raise AssertionError("changed authority must refuse editor entry")
    except Exception as exc:
        assert "authority changed" in str(exc)
    assert calls == 0
    assert store.get(claim.record.job_id).provider_entered is False  # type: ignore[union-attr]


def test_clean_merge_records_controller_result_without_editor_entry(tmp_path: Path, _use_real_commands) -> None:
    root, head, base = history(tmp_path, conflict=False)
    store, claim, _metadata, instance = adapter(tmp_path, root, head, base, lambda _prompt, _root: (_ for _ in ()).throw(AssertionError("editor called")))
    result = instance.invoke_at_provider_entry(claim.record, lambda: (_ for _ in ()).throw(AssertionError("checkpoint called")))
    manifest = json.loads(result.output)
    assert result.outcome is InvocationOutcome.COMPLETED
    assert manifest["completion_source"] == "controller_verified_merge"
    assert store.get(claim.record.job_id).provider_entered is False  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("response", "rewrite", "expected"),
    [
        ("CANNOT_FIX", False, InvocationOutcome.CANNOT_FIX),
        ("ACTION_SUMMARY: incomplete", False, InvocationOutcome.FAILED),
    ],
)
def test_cannot_fix_and_leftover_markers_are_nonpublishing(
    tmp_path: Path,
    _use_real_commands,
    response: str,
    rewrite: bool,
    expected: InvocationOutcome,
) -> None:
    root, head, base = history(tmp_path)

    def edit(_prompt: str, workspace: Path) -> str:
        if rewrite:
            (workspace / "shared.txt").write_text("resolved\n")
        return response

    store, claim, _metadata, instance = adapter(tmp_path, root, head, base, edit)
    result = instance.invoke_at_provider_entry(claim.record, lambda: store.mark_provider_entered(claim))
    manifest = json.loads(result.output)
    assert result.outcome is expected
    assert manifest["outcome"] == expected.value
    assert manifest["merge_head"] == base
    assert git(root, "rev-parse", "HEAD").stdout.strip() == base


def test_root_scoped_inspection_never_treats_observation_failure_as_clean(tmp_path: Path, _use_real_commands) -> None:
    result = inspect_conflicts(tmp_path / "missing")
    assert not result.complete
    assert result.reason
