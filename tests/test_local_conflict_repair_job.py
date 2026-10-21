from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.codex_pr_attribution import AttributionDisposition
from auto_coder.conflict_resolver import inspect_conflicts
from auto_coder.invocation_admission import InvocationAdmissionGate
from auto_coder.local_conflict_repair_job import LocalConflictRepairJobAdapter
from auto_coder.local_job_handoff import InvocationOutcome, LocalJobKind, LocalJobOffer, LocalJobState, LocalJobStore, PRRepairAdmission
from auto_coder.local_job_runner import LocalJobProviderEntryRefused, LocalJobRunner
from auto_coder.local_review_repair import LocalReviewRepairStore
from auto_coder.pr_local_job_runtime import build_pr_local_job_adapters
from auto_coder.pr_processor import _start_mergeability_remediation
from auto_coder.pr_repair_dispatch import RepairDisposition, RepairKind
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


@pytest.mark.parametrize(
    "response",
    [
        "Resolution attempted; unable to confirm completion",
        "ACTION_SUMMARY: resolved\nCANNOT_FIX",
        "ACTION_SUMMARY:",
        "ACTION_SUMMARY: resolved\nextra unstructured text",
    ],
)
def test_ambiguous_editor_output_is_nonpublishing_after_files_are_resolved(tmp_path: Path, _use_real_commands, response: str) -> None:
    root, head, base = history(tmp_path)

    def edit(_prompt: str, workspace: Path) -> str:
        (workspace / "shared.txt").write_text("resolved\n")
        return response

    store, claim, _metadata, instance = adapter(tmp_path, root, head, base, edit)
    result = instance.invoke_at_provider_entry(claim.record, lambda: store.mark_provider_entered(claim))
    manifest = json.loads(result.output)
    assert result.outcome is not InvocationOutcome.COMPLETED
    assert manifest["outcome"] != InvocationOutcome.COMPLETED.value
    assert not inspect_conflicts(Path(manifest["workspace"])).marker_paths


def test_clean_merge_with_marker_bearing_base_is_nonpublishing(tmp_path: Path, _use_real_commands) -> None:
    root, head, base = history(tmp_path, conflict=False)
    git(root, "switch", "main")
    marker_file = root / "generated.txt"
    marker_file.write_text("<<<<<<< retained marker\nleft\n=======\nright\n>>>>>>> retained marker\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "add marker-bearing generated file")
    base = git(root, "rev-parse", "HEAD").stdout.strip()
    git(root, "push", "origin", "main")
    store, claim, _metadata, instance = adapter(tmp_path, root, head, base, lambda _prompt, _root: (_ for _ in ()).throw(AssertionError("editor called")))
    result = instance.invoke_at_provider_entry(claim.record, lambda: (_ for _ in ()).throw(AssertionError("checkpoint called")))
    manifest = json.loads(result.output)
    inspection = inspect_conflicts(Path(manifest["workspace"]))
    assert inspection.complete and inspection.marker_paths == ("generated.txt",)
    assert result.outcome is InvocationOutcome.FAILED
    assert manifest["completion_source"] == "controller_verified_merge"
    assert manifest["outcome"] == InvocationOutcome.FAILED.value
    assert store.get(claim.record.job_id).provider_entered is False  # type: ignore[union-attr]


def test_root_scoped_inspection_never_treats_observation_failure_as_clean(tmp_path: Path, _use_real_commands) -> None:
    result = inspect_conflicts(tmp_path / "missing")
    assert not result.complete
    assert result.reason


def wait_until(predicate, timeout: float = 5) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_selected_local_origin_runs_through_runner_and_persists_exact_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    """The production selection seam must connect admission to durable execution."""
    from auto_coder import pr_processor

    root, head, base = history(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    review_store = LocalReviewRepairStore(tmp_path / "reviews.sqlite3")
    metadata = {
        "number": 7,
        "state": "open",
        "merged": False,
        "title": "Conflict",
        "body": "",
        "user": {"login": "human"},
        "head": {"repo": {"full_name": "owner/repo"}, "ref": "feature", "sha": head},
        "base": {"repo": {"full_name": "owner/repo"}, "ref": "main", "sha": base},
    }
    github = Mock()
    github.get_pull_request_metadata_strict.return_value = metadata
    editor_roots: list[Path] = []

    def edit(_prompt: str, workspace: Path) -> str:
        editor_roots.append(workspace)
        assert "<<<<<<<" in (workspace / "shared.txt").read_text()
        (workspace / "shared.txt").write_text("runner resolved\n")
        return "ACTION_SUMMARY: runner resolved"

    with (
        patch.object(pr_processor, "_selected_local_backend_name", return_value="codex"),
        patch.object(pr_processor, "check_pr_repair_exhaustion", return_value=None),
    ):
        admitted = pr_processor._offer_selected_local_pr_repair(
            "owner/repo",
            metadata,
            RepairKind.MERGE_CONFLICT,
            github,
            conflict_eligible=True,
            store=store,
            repair_store=review_store,
        )

    assert admitted is not None and admitted.disposition is RepairDisposition.ACCEPTED_OR_UNCERTAIN
    job = store.get(admitted.receipt)
    assert job is not None and job.state is LocalJobState.PENDING
    with (
        patch("auto_coder.pr_repair_guard.check_pr_repair_exhaustion", return_value=None),
        patch("auto_coder.cloud_manager.CloudManager.read_bindings_strict", return_value={}),
        patch(
            "auto_coder.codex_pr_attribution.CodexPrAttributionRepository.get",
            return_value=Mock(disposition=AttributionDisposition.UNRESOLVED),
        ),
    ):
        adapters = build_pr_local_job_adapters(
            github,
            AutomationConfig(),
            store,
            repository_path=root,
            conflict_editor_factory=lambda _backend, _job: edit,
            conflict_workspace_producer=PRRepairWorkspaceProducer(store, tmp_path / "workspaces"),
        )
        runner = LocalJobRunner(
            store,
            InvocationAdmissionGate(),
            repository="owner/repo",
            capacity=1,
            adapters=adapters,
        )
        assert runner.poll() == 1
        wait_until(lambda: runner.active_count() == 0)
    completed = store.get(job.job_id)
    assert completed is not None and completed.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING
    artifact = store.get_result_artifact(completed.result_reference)
    assert artifact is not None
    verified = PRRepairWorkspaceProducer(store, tmp_path / "workspaces").revalidate_result(completed, artifact)
    assert verified.manifest.head_sha == head
    assert verified.manifest.base_sha == base
    assert verified.manifest.merge_head == base
    assert verified.manifest.observed_output == "ACTION_SUMMARY: runner resolved"
    assert len(editor_roots) == 1
    assert git(root, "rev-parse", "HEAD").stdout.strip() == base
    assert git(root, "ls-remote", "origin", "refs/heads/feature").stdout.split()[0] == head
    runner.close()


def test_normal_mergeability_remediation_admits_and_runs_in_private_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    """Normal conflict processing reaches durable admission before checkout."""
    root, head_sha, base_sha = history(tmp_path)
    peer_head = git(root, "rev-parse", "HEAD").stdout.strip()
    peer_contents = (root / "shared.txt").read_text()
    remote_head = git(root, "rev-parse", "origin/feature").stdout.strip()
    metadata = {
        "number": 7,
        "state": "open",
        "merged": False,
        "title": "Conflict",
        "body": "",
        "user": {"login": "human"},
        "head": {"repo": {"full_name": "owner/repo"}, "ref": "feature", "sha": head_sha},
        "base": {"repo": {"full_name": "owner/repo"}, "ref": "main", "sha": base_sha},
    }
    github = Mock(token="token")
    github.get_pull_request_metadata_strict.return_value = metadata
    api = Mock()
    api.pulls.get.return_value = metadata
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(root)

    def edit(_prompt: str, workspace: Path) -> str:
        (workspace / "shared.txt").write_text("resolved\n")
        return "ACTION_SUMMARY: resolved conflict"

    with (
        patch("auto_coder.pr_processor.GitHubClient.get_instance", return_value=github),
        patch("auto_coder.pr_processor.get_ghapi_client", return_value=api),
        patch("auto_coder.pr_processor._selected_local_backend_name", return_value="codex"),
        patch("auto_coder.pr_processor.check_pr_repair_exhaustion", return_value=None),
        patch("auto_coder.pr_processor._checkout_pr_branch") as checkout,
    ):
        actions = _start_mergeability_remediation(7, "DIRTY", "owner/repo")

    assert actions.repair_result is not None
    assert actions.repair_result.disposition is RepairDisposition.ACCEPTED_OR_UNCERTAIN
    checkout.assert_not_called()
    store = LocalJobStore()
    job = store.get(actions.repair_result.receipt)
    assert job is not None and job.state is LocalJobState.PENDING
    with patch("auto_coder.pr_repair_guard.check_pr_repair_exhaustion", return_value=None):
        adapters = build_pr_local_job_adapters(
            github,
            AutomationConfig(),
            store,
            repository_path=root,
            conflict_editor_factory=lambda _backend, _job: edit,
            conflict_workspace_producer=PRRepairWorkspaceProducer(store, tmp_path / "workspaces"),
        )
        runner = LocalJobRunner(store, InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters=adapters)
        assert runner.poll() == 1
        wait_until(lambda: runner.active_count() == 0)
    completed = store.get(job.job_id)
    assert completed is not None and completed.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING
    assert git(root, "rev-parse", "HEAD").stdout.strip() == peer_head
    assert (root / "shared.txt").read_text() == peer_contents
    assert git(root, "rev-parse", "origin/feature").stdout.strip() == remote_head


@pytest.mark.parametrize("authority", ["disabled", "exhausted", "unavailable", "cloud", "codex"])
def test_production_adapter_refreshes_revocable_authority_before_preparation(tmp_path: Path, _use_real_commands, authority: str) -> None:
    root, head_sha, base_sha = history(tmp_path)
    store, claim, metadata, _instance = adapter(tmp_path, root, head_sha, base_sha, lambda _prompt, _root: "ACTION_SUMMARY: unexpected")
    github = Mock()
    github.get_pull_request_metadata_strict.return_value = metadata
    config = Mock(ENABLE_MERGEABILITY_REMEDIATION=authority != "disabled")
    allowance = patch("auto_coder.pr_repair_guard.check_pr_repair_exhaustion", return_value=Mock(is_exhausted=True) if authority == "exhausted" else None)
    if authority == "unavailable":
        allowance = patch("auto_coder.pr_repair_guard.check_pr_repair_exhaustion", side_effect=OSError("unreadable"))
    with (
        allowance,
        patch("auto_coder.cloud_manager.CloudManager.read_bindings_strict", return_value={"7": Mock()} if authority == "cloud" else {}),
        patch(
            "auto_coder.codex_pr_attribution.CodexPrAttributionRepository.get",
            return_value=Mock(disposition=AttributionDisposition.VERIFIED if authority == "codex" else AttributionDisposition.UNRESOLVED),
        ),
    ):
        production = build_pr_local_job_adapters(github, config, store, repository_path=root)[LocalJobKind.PR_MERGE_CONFLICT]
        assert production.authorize_provider_entry(claim.record) is False


def test_production_adapter_rechecks_cloud_binding_at_final_entry(tmp_path: Path, _use_real_commands) -> None:
    root, head_sha, base_sha = history(tmp_path)
    editor_calls = 0

    def edit(_prompt: str, _root: Path) -> str:
        nonlocal editor_calls
        editor_calls += 1
        return "ACTION_SUMMARY: unexpected"

    store, claim, metadata, _instance = adapter(tmp_path, root, head_sha, base_sha, edit)
    github = Mock()
    github.get_pull_request_metadata_strict.return_value = metadata
    with (
        patch("auto_coder.pr_repair_guard.check_pr_repair_exhaustion", return_value=None),
        patch("auto_coder.cloud_manager.CloudManager.read_bindings_strict", side_effect=({}, {"7": Mock()})),
        patch(
            "auto_coder.codex_pr_attribution.CodexPrAttributionRepository.get",
            return_value=Mock(disposition=AttributionDisposition.UNRESOLVED),
        ),
    ):
        production = build_pr_local_job_adapters(
            github,
            Mock(ENABLE_MERGEABILITY_REMEDIATION=True),
            store,
            repository_path=root,
            conflict_editor_factory=lambda _backend, _job: edit,
            conflict_workspace_producer=PRRepairWorkspaceProducer(store, tmp_path / "production-workspaces"),
        )[LocalJobKind.PR_MERGE_CONFLICT]
        assert production.authorize_provider_entry(claim.record) is True
        with pytest.raises(LocalJobProviderEntryRefused):
            production.invoke_at_provider_entry(claim.record, lambda: True)
    assert editor_calls == 0
    assert store.get(claim.record.job_id).provider_entered is False


@pytest.mark.parametrize("final_allowance", [Mock(is_exhausted=True), OSError("unreadable")])
def test_production_adapter_rechecks_allowance_at_final_entry(tmp_path: Path, _use_real_commands, final_allowance: object) -> None:
    root, head_sha, base_sha = history(tmp_path)
    editor_calls = 0

    def edit(_prompt: str, _root: Path) -> str:
        nonlocal editor_calls
        editor_calls += 1
        return "ACTION_SUMMARY: unexpected"

    store, claim, metadata, _instance = adapter(tmp_path, root, head_sha, base_sha, edit)
    github = Mock()
    github.get_pull_request_metadata_strict.return_value = metadata
    allowance_effects: tuple[object, object]
    if isinstance(final_allowance, BaseException):
        allowance_effects = (None, final_allowance)
    else:
        allowance_effects = (None, final_allowance)
    with (
        patch("auto_coder.pr_repair_guard.check_pr_repair_exhaustion", side_effect=allowance_effects),
        patch("auto_coder.cloud_manager.CloudManager.read_bindings_strict", return_value={}),
        patch(
            "auto_coder.codex_pr_attribution.CodexPrAttributionRepository.get",
            return_value=Mock(disposition=AttributionDisposition.UNRESOLVED),
        ),
    ):
        production = build_pr_local_job_adapters(
            github,
            Mock(ENABLE_MERGEABILITY_REMEDIATION=True),
            store,
            repository_path=root,
            conflict_editor_factory=lambda _backend, _job: edit,
            conflict_workspace_producer=PRRepairWorkspaceProducer(store, tmp_path / "production-workspaces"),
        )[LocalJobKind.PR_MERGE_CONFLICT]
        assert production.authorize_provider_entry(claim.record) is True
        with pytest.raises(LocalJobProviderEntryRefused):
            production.invoke_at_provider_entry(claim.record, lambda: True)
    assert editor_calls == 0


def test_runner_rechecks_advanced_base_at_held_provider_entry(tmp_path: Path, _use_real_commands) -> None:
    """A retained job must not edit after the live base advances in place."""
    root, head, base = history(tmp_path)
    store, claim, metadata, instance = adapter(tmp_path, root, head, base, lambda _prompt, _root: "unexpected")
    # Return the claim to pending so the real runner owns entry and persistence.
    assert store.release_unentered_claim(claim, "test runner transfer")
    at_final_read = Event()
    release_read = Event()
    reads = 0

    def held_read(_repo: str, _number: int):
        nonlocal reads
        reads += 1
        if reads == 2:
            at_final_read.set()
            assert release_read.wait(5)
        return metadata

    instance._metadata_reader = held_read
    editor_calls = 0

    def forbidden_editor(_prompt: str, _root: Path) -> str:
        nonlocal editor_calls
        editor_calls += 1
        return "unexpected"

    instance._editor_factory = lambda _backend, _job: forbidden_editor
    runner = LocalJobRunner(
        store,
        InvocationAdmissionGate(),
        repository="owner/repo",
        capacity=1,
        adapters={LocalJobKind.PR_MERGE_CONFLICT: instance},
    )
    assert runner.poll() == 1
    assert at_final_read.wait(5)
    git(root, "switch", "main")
    (root / "advance.txt").write_text("advanced\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "advance base")
    advanced = git(root, "rev-parse", "HEAD").stdout.strip()
    git(root, "push", "origin", "main")
    metadata["base"]["sha"] = advanced
    release_read.set()
    wait_until(lambda: runner.active_count() == 0)
    retained = store.get(claim.record.job_id)
    assert retained is not None and not retained.provider_entered
    assert not retained.result_reference
    assert editor_calls == 0
    assert git(root, "ls-remote", "origin", "refs/heads/feature").stdout.split()[0] == head
    runner.close()


def test_reopened_runner_never_replays_unconfirmed_entered_editor(tmp_path: Path, _use_real_commands) -> None:
    """A crash after entry but before result persistence cannot become a fresh edit."""
    root, head, base = history(tmp_path)
    path = tmp_path / "jobs.sqlite3"

    class InterruptedStore(LocalJobStore):
        def persist_result_artifact(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise KeyboardInterrupt("simulated controller death before result checkpoint")

    store = InterruptedStore(path)
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
    metadata = {
        "state": "open",
        "merged": False,
        "head": {"repo": {"full_name": "owner/repo"}, "ref": "feature", "sha": head},
        "base": {"repo": {"full_name": "owner/repo"}, "ref": "main", "sha": base},
    }
    editor_calls = 0

    def edit(_prompt: str, workspace: Path) -> str:
        nonlocal editor_calls
        editor_calls += 1
        (workspace / "shared.txt").write_text("resolved before controller crash\n")
        return "ACTION_SUMMARY: response not durably acknowledged"

    first_adapter = LocalConflictRepairJobAdapter(
        store,
        PRRepairSource(root),
        lambda _repo, _number: metadata,
        lambda _job, _metadata: True,
        lambda _job, _metadata: True,
        lambda _backend, _job: edit,
        workspace_producer=PRRepairWorkspaceProducer(store, tmp_path / "workspaces"),
    )
    first = LocalJobRunner(
        store,
        InvocationAdmissionGate(),
        repository="owner/repo",
        capacity=1,
        adapters={LocalJobKind.PR_MERGE_CONFLICT: first_adapter},
        runner_id="process:999999:dead",
    )
    assert first.poll() == 1
    wait_until(lambda: first.active_count() == 0)
    interrupted = LocalJobStore(path).get(job.job_id)
    assert interrupted is not None and interrupted.state is LocalJobState.RUNNING
    assert interrupted.provider_entered and not interrupted.result_reference
    assert editor_calls == 1
    first.close()

    reopened = LocalJobStore(path)
    second_adapter = LocalConflictRepairJobAdapter(
        reopened,
        PRRepairSource(root),
        lambda _repo, _number: metadata,
        lambda _job, _metadata: True,
        lambda _job, _metadata: True,
        lambda _backend, _job: edit,
        workspace_producer=PRRepairWorkspaceProducer(reopened, tmp_path / "workspaces"),
    )
    restarted = LocalJobRunner(
        reopened,
        InvocationAdmissionGate(),
        repository="owner/repo",
        capacity=1,
        adapters={LocalJobKind.PR_MERGE_CONFLICT: second_adapter},
    )
    restarted.recover_unsettled()
    assert restarted.poll() == 0
    retained = reopened.get(job.job_id)
    assert retained is not None and retained.state is LocalJobState.RUNNING
    assert retained.execution_incarnation == interrupted.execution_incarnation
    assert editor_calls == 1
    assert not retained.result_reference
    restarted.close()
