from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest

from auto_coder.issue_dispatch import CandidateHandoff, IssueAttemptIdentity, IssueDispatchGuard
from auto_coder.issue_job_workspace import IssueJobCannotFix, IssueJobSource, IssueJobWorkspaceError, IssueJobWorkspaceProducer
from auto_coder.local_job_handoff import LocalJobKind, LocalJobOffer, LocalJobStore


def git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "value.txt").write_text("original\n", encoding="utf-8")
    git(repo, "add", "value.txt")
    git(repo, "commit", "-m", "initial")
    remote = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "origin", "main")
    return repo, git(repo, "rev-parse", "HEAD")


def claim(store: LocalJobStore, target: int, *, attempt: str | None = None):
    attempt = attempt or f"attempt-{target}"
    offer = LocalJobOffer(LocalJobKind.ISSUE_IMPLEMENTATION, "owner/repo", target, attempt, "codex", f"prompt-{target}")
    identity = IssueAttemptIdentity("owner", "repo", target, attempt)
    guard = IssueDispatchGuard(store.path.with_name(f"dispatch-{target}-{attempt}.sqlite3"))
    dispatch = guard.reserve(identity, CandidateHandoff("codex", "local"))
    assert dispatch.admitted
    accepted = store.offer_issue(offer, identity, guard)
    assert accepted is not None
    acquired = store.claim(offer.job_id)
    assert acquired is not None and acquired.acquired
    return acquired


def test_concurrent_jobs_use_distinct_pinned_workspaces(tmp_path: Path, _use_real_commands) -> None:
    repo, _head = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    producer = IssueJobWorkspaceProducer(store, tmp_path / "workspaces")
    claims = [claim(store, number) for number in (1, 2)]
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    results = []

    def run(index: int) -> None:
        source = IssueJobSource("owner/repo", repo, "main", f"issue-{index + 1}")

        def invoke(workspace: Path, prompt: str) -> str:
            entered[index].set()
            assert entered[1 - index].wait(5)
            assert release.wait(5)
            (workspace / "value.txt").write_text(f"{prompt}\n", encoding="utf-8")
            return f"ACTION_SUMMARY: {prompt}"

        results.append(producer.execute(claims[index], source, invoke))

    threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    assert entered[0].wait(5) and entered[1].wait(5)
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "branch", "--show-current") == "main"
    release.set()
    for thread in threads:
        thread.join(5)

    assert len(results) == 2
    assert results[0].workspace != results[1].workspace
    assert {item.work_branch for item in results} == {"issue-1", "issue-2"}
    assert {item.workspace.joinpath("value.txt").read_text(encoding="utf-8") for item in results} == {"prompt-1\n", "prompt-2\n"}
    reopened = LocalJobStore(tmp_path / "jobs.sqlite3")
    assert all(reopened.get(item.job_id).result_reference == item.result_reference for item in results)  # type: ignore[union-attr]
    assert all(Path(reopened.get(item.job_id).workspace_path).is_dir() for item in results)  # type: ignore[union-attr]


def test_remote_advance_is_fetched_despite_stale_local_main(tmp_path: Path, _use_real_commands) -> None:
    repo, old_head = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    acquired = claim(store, 3)
    (repo / "value.txt").write_text("latest remote main\n", encoding="utf-8")
    git(repo, "commit", "-am", "remote advance after admission")
    latest = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "origin", "main")
    git(repo, "reset", "--hard", old_head)
    git(repo, "update-ref", "refs/remotes/origin/main", old_head)

    def inspect(workspace: Path, _prompt: str) -> str:
        assert git(workspace, "rev-parse", "HEAD") == latest
        assert (workspace / "value.txt").read_text() == "latest remote main\n"
        assert git(workspace, "status", "--porcelain") == ""
        return "ACTION_SUMMARY: verified current remote source"

    checkpoint = IssueJobWorkspaceProducer(store, tmp_path / "workspaces").execute(acquired, IssueJobSource("owner/repo", repo, "main", "issue-3"), inspect)
    record = LocalJobStore(store.path).get(acquired.record.job_id)
    assert record is not None
    assert record.source_commit == checkpoint.source_commit == latest
    assert record.source_ref == "refs/remotes/origin/main"
    assert record.publication_remote == git(repo, "remote", "get-url", "origin")
    assert git(repo, "rev-parse", "HEAD") == old_head
    assert git(repo, "rev-parse", "origin/main") == old_head


def test_retry_fetches_new_remote_commit_and_keeps_prior_result_pinned(tmp_path: Path, _use_real_commands) -> None:
    repo, head = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    producer = IssueJobWorkspaceProducer(store, tmp_path / "workspaces")
    first = producer.execute(claim(store, 5574, attempt="0"), IssueJobSource("owner/repo", repo, "main", "issue-5574-attempt-0"), lambda *_: "ACTION_SUMMARY: first attempt")
    (repo / "prerequisite.txt").write_text("new remote prerequisite\n")
    git(repo, "add", "prerequisite.txt")
    git(repo, "commit", "-m", "advance remote before retry")
    latest = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "origin", "main")
    git(repo, "checkout", "--detach", head)
    git(repo, "update-ref", "refs/remotes/origin/main", head)

    def retry(workspace: Path, _prompt: str) -> str:
        assert git(workspace, "rev-parse", "HEAD") == latest
        assert (workspace / "prerequisite.txt").read_text() == "new remote prerequisite\n"
        return "ACTION_SUMMARY: retry from current remote main"

    second = producer.execute(claim(store, 5574, attempt="retry-id"), IssueJobSource("owner/repo", repo, "main", "issue-5574-attempt-retry-id"), retry)
    assert first.job_id != second.job_id
    assert first.workspace != second.workspace
    assert first.source_commit == git(first.workspace, "rev-parse", "HEAD") == head
    assert second.source_commit == latest
    assert not (first.workspace / "prerequisite.txt").exists()
    assert store.get(first.job_id).source_commit == head
    assert store.get(second.job_id).source_commit == latest


def test_configured_base_branch_is_used_instead_of_default_main(tmp_path: Path, _use_real_commands) -> None:
    repo, head = repository(tmp_path)
    git(repo, "switch", "-c", "release/current")
    (repo / "value.txt").write_text("release baseline\n")
    git(repo, "commit", "-am", "release baseline")
    latest = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "origin", "release/current")
    git(repo, "switch", "main")
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    acquired = claim(store, 9)
    checkpoint = IssueJobWorkspaceProducer(store, tmp_path / "workspaces").execute(acquired, IssueJobSource("owner/repo", repo, "release/current", "issue-9"), lambda *_: "ACTION_SUMMARY: release source")
    assert checkpoint.source_commit == latest
    assert (checkpoint.workspace / "value.txt").read_text() == "release baseline\n"
    assert store.get(checkpoint.job_id).source_ref == "refs/remotes/origin/release/current"
    assert git(repo, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("failure", ["missing_remote", "unreachable_remote", "missing_branch"])
def test_remote_preparation_failure_never_invokes_or_binds(tmp_path: Path, _use_real_commands, failure: str) -> None:
    repo, _head = repository(tmp_path)
    if failure == "missing_remote":
        git(repo, "remote", "remove", "origin")
    elif failure == "unreachable_remote":
        git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    else:
        git(Path(git(repo, "remote", "get-url", "origin")), "update-ref", "-d", "refs/heads/main")
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    acquired = claim(store, 7)

    def unexpected_invocation(*_args) -> str:
        raise AssertionError("Remote preparation failure must prevent provider entry")

    with pytest.raises(IssueJobWorkspaceError):
        IssueJobWorkspaceProducer(store, tmp_path / "workspaces").execute(acquired, IssueJobSource("owner/repo", repo, "main", "issue-7"), unexpected_invocation)
    record = LocalJobStore(store.path).get(acquired.record.job_id)
    assert record is not None
    assert record.result_reference == record.workspace_path == record.source_commit == record.source_ref == ""
    assert not record.provider_entered


def test_model_head_change_is_not_a_successful_checkpoint(tmp_path: Path, _use_real_commands) -> None:
    repo, _head = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    acquired = claim(store, 4)
    producer = IssueJobWorkspaceProducer(store, tmp_path / "workspaces")

    def commit(workspace: Path, _prompt: str) -> str:
        git(workspace, "config", "user.email", "test@example.com")
        git(workspace, "config", "user.name", "Test")
        (workspace / "value.txt").write_text("committed\n", encoding="utf-8")
        git(workspace, "commit", "-am", "unauthorized")
        return "ACTION_SUMMARY: done"

    with pytest.raises(IssueJobWorkspaceError, match="changed private HEAD"):
        producer.execute(acquired, IssueJobSource("owner/repo", repo, "main", "issue-4"), commit)

    record = store.get(acquired.record.job_id)
    assert record is not None and record.result_reference == ""
    assert Path(record.workspace_path).joinpath("value.txt").read_text(encoding="utf-8") == "committed\n"


def test_index_flags_cannot_hide_working_file_result(tmp_path: Path, _use_real_commands) -> None:
    repo, _head = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    acquired = claim(store, 5)
    producer = IssueJobWorkspaceProducer(store, tmp_path / "workspaces")

    def edit_hidden_from_git(workspace: Path, _prompt: str) -> str:
        git(workspace, "update-index", "--assume-unchanged", "value.txt")
        (workspace / "value.txt").write_text("actual result\n", encoding="utf-8")
        assert git(workspace, "diff", "--", "value.txt") == ""
        return "ACTION_SUMMARY: changed value"

    checkpoint = producer.execute(acquired, IssueJobSource("owner/repo", repo, "main", "issue-5"), edit_hidden_from_git)
    artifact = LocalJobStore(store.path).get_result_artifact(checkpoint.result_reference)
    assert artifact is not None
    manifest = json.loads(artifact.output)
    assert manifest["changed_files"]["value.txt"]["checksum"] == __import__("hashlib").sha256(b"actual result\n").hexdigest()


@pytest.mark.parametrize(
    ("response", "expected_error", "message"),
    [
        ("CANNOT_FIX", IssueJobCannotFix, "local invocation returned CANNOT_FIX"),
        ("implementation failed", IssueJobWorkspaceError, "confirmed implementation result"),
        ("ACTION_SUMMARY:", IssueJobWorkspaceError, "confirmed implementation result"),
        ("ACTION_SUMMARY: ok\nextra output", IssueJobWorkspaceError, "confirmed implementation result"),
    ],
)
def test_unconfirmed_response_never_records_completed_result(tmp_path: Path, _use_real_commands, response: str, expected_error, message: str) -> None:
    repo, _head = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    acquired = claim(store, 10 + len(response))
    producer = IssueJobWorkspaceProducer(store, tmp_path / "workspaces")

    with pytest.raises(expected_error, match=message):
        producer.execute(acquired, IssueJobSource("owner/repo", repo, "main", f"issue-{10 + len(response)}"), lambda *_: response)

    reopened = LocalJobStore(store.path)
    record = reopened.get(acquired.record.job_id)
    assert record is not None and record.result_reference == ""
    assert record.invocation_outcome is None


@pytest.mark.parametrize("detached", [False, True])
def test_shared_branch_and_dirty_files_cannot_enter_remote_workspace(tmp_path: Path, _use_real_commands, detached: bool) -> None:
    repo, head = repository(tmp_path)
    git(repo, "switch", "-c", "dependabot/old")
    (repo / "peer.txt").write_text("unrelated committed content\n")
    git(repo, "add", "peer.txt")
    git(repo, "commit", "-m", "unrelated WIP")
    peer_head = git(repo, "rev-parse", "HEAD")
    if detached:
        git(repo, "checkout", "--detach")
    (repo / "value.txt").write_text("staged version\n")
    git(repo, "add", "value.txt")
    (repo / "value.txt").write_text("working version\n")
    (repo / "untracked.txt").write_text("untracked\n")
    (repo / ".gitignore").write_text("local.env\n")
    (repo / "local.env").write_text("ignored\n")
    before = git(repo, "status", "--porcelain", "--ignored")
    index = git(repo, "write-tree")
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    acquired = claim(store, 6)

    def inspect(workspace: Path, _prompt: str) -> str:
        assert git(workspace, "rev-parse", "HEAD") == head
        assert git(workspace, "show", ":value.txt") == "original"
        assert (workspace / "value.txt").read_text() == "original\n"
        assert git(workspace, "status", "--porcelain", "--ignored") == ""
        assert {p.name for p in workspace.iterdir()} == {".git", "value.txt"}
        return "ACTION_SUMMARY: verified clean remote source"

    IssueJobWorkspaceProducer(store, tmp_path / "workspaces").execute(acquired, IssueJobSource("owner/repo", repo, "main", "issue-6"), inspect)
    assert git(repo, "rev-parse", "HEAD") == peer_head
    assert git(repo, "branch", "--show-current") == ("" if detached else "dependabot/old")
    assert git(repo, "write-tree") == index
    assert git(repo, "status", "--porcelain", "--ignored") == before
    assert (repo / "value.txt").read_text() == "working version\n"
    assert (repo / "untracked.txt").read_text() == "untracked\n"
    assert (repo / "local.env").read_text() == "ignored\n"


@pytest.mark.parametrize(
    "output",
    [
        '{"type":"thread.started"}\n{"type":"turn.failed"}\n{"type":"item.completed","item":{"type":"agent_message","text":"ACTION_SUMMARY: false success"}}',
        '{"type":"thread.started"}\nnot JSON\n{"type":"item.completed","item":{"type":"agent_message","text":"ACTION_SUMMARY: false success"}}',
        '{"type":"thread.started"}\n{"type":"turn.completed"}',
        '{"type":"system","subtype":"init"}\n{"type":"result","subtype":"error","result":"ACTION_SUMMARY: false success"}',
        '{"record_type":"event","payload_type":"run.terminal.failed","payload":{"reason":"failed"}}',
    ],
)
def test_unsuccessful_cli_stream_never_checkpoints_success(tmp_path: Path, _use_real_commands, output: str) -> None:
    repo, _head = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    acquired = claim(store, 99)
    producer = IssueJobWorkspaceProducer(store, tmp_path / "workspaces")
    with pytest.raises(ValueError):
        producer.execute(acquired, IssueJobSource("owner/repo", repo, "main", "issue-99"), lambda *_: output)
    record = store.get(acquired.record.job_id)
    assert record is not None
    assert record.result_reference == ""


@pytest.mark.parametrize("modified", [False, True])
def test_job_workspace_supports_backend_manager_private_clone(tmp_path: Path, _use_real_commands, modified: bool) -> None:
    from auto_coder.worktree_utils import isolated_local_llm_worktree

    repo, _head = repository(tmp_path)
    if modified:
        (repo / "value.txt").write_text("staged\n")
        git(repo, "add", "value.txt")
        (repo / "value.txt").write_text("working\n")
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    acquired = claim(store, 101)
    producer = IssueJobWorkspaceProducer(store, tmp_path / "workspaces")

    def implement(workspace: Path, _prompt: str) -> str:
        # Use the actual preparation and handoff path used by BackendManager;
        # neither copying nor index-consistency checks are replaced.
        with isolated_local_llm_worktree(workspace) as private:
            private_path = Path(private)
            assert private_path != workspace
            assert git(private_path, "show", "HEAD:value.txt") == "original"
            assert git(private_path, "show", ":value.txt") == "original"
            assert private_path.joinpath("value.txt").read_text() == "original\n"
            private_path.joinpath("value.txt").write_text("implemented\n")
        assert workspace.joinpath("value.txt").read_text() == "implemented\n"
        return "ACTION_SUMMARY: implemented"

    checkpoint = producer.execute(acquired, IssueJobSource("owner/repo", repo, "main", "issue-101"), implement)
    assert checkpoint.workspace.joinpath("value.txt").read_text() == "implemented\n"
    assert repo.joinpath("value.txt").read_text() == ("working\n" if modified else "original\n")
    assert git(repo, "show", ":value.txt") == ("staged" if modified else "original")


@pytest.mark.parametrize("directory", ["node_modules", ".cache", "__pycache__", ".agent-tmp"])
def test_generated_files_do_not_enter_job_capture(tmp_path: Path, _use_real_commands, directory: str) -> None:
    repo, _ = repository(tmp_path)
    generated = repo / "nested" / directory
    generated.mkdir(parents=True)
    (generated / "tracked.txt").write_text("tracked context\n")
    git(repo, "add", f"nested/{directory}/tracked.txt")
    git(repo, "commit", "-m", "tracked context in disposable directory")
    git(repo, "push", "origin", "main")
    (generated / "runtime.txt").write_text("local runtime\n")
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    acquired = claim(store, 13)

    def implement(workspace, _prompt):
        assert (workspace / "nested" / directory / "tracked.txt").read_text() == "tracked context\n"
        assert not (workspace / "nested" / directory / "runtime.txt").exists()
        output = workspace / "nested" / directory / "new-runtime.txt"
        output.write_text("generated by implementation\n")
        (workspace / "value.txt").write_text("implemented\n")
        return "ACTION_SUMMARY: implemented value.txt"

    checkpoint = IssueJobWorkspaceProducer(store, tmp_path / "workspaces").execute(acquired, IssueJobSource("owner/repo", repo, "main", "issue-13"), implement)
    artifact = store.get_result_artifact(checkpoint.result_reference)
    assert artifact is not None
    manifest = json.loads(artifact.output)
    assert set(manifest["changed_files"]) == {"value.txt"}
    assert f"nested/{directory}/tracked.txt" in manifest["workspace_files"]
    assert not any(name.endswith("runtime.txt") for name in manifest["workspace_files"])
    assert (generated / "runtime.txt").read_text() == "local runtime\n"
    assert (repo / "value.txt").read_text() == "original\n"
