"""Interrupted durable Issue work resumes in its original provider repository."""

import gc
import json
import os
import signal
import time
from multiprocessing import get_context
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_coder.cli_helpers import build_backend_manager
from auto_coder.execution_trace import get_trace_collector
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.invocation_admission import InvocationAdmissionGate
from auto_coder.issue_dispatch import CandidateHandoff, IssueAttemptIdentity, IssueDispatchGuard
from auto_coder.issue_job_finalizer import IssueJobFinalizer
from auto_coder.issue_local_job import IssueJobAuthorizationPorts, IssueLocalJobAdapter
from auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
from auto_coder.local_job_handoff import LocalJobKind, LocalJobOffer, LocalJobState, LocalJobStore
from auto_coder.local_job_runner import LocalJobRunner, writer_group_settled

from .test_issue_job_workspace import git
from .test_muse_msp import _host, _repository


def _wait(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def _crash_controller(source: Path, root: Path) -> None:
    """Run the real adapter/transport in a separately killable controller."""
    os.chdir(source)
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3", timeout=60)})
    store = LocalJobStore(root / "jobs.sqlite3")
    slots = ImplementationSlotRepository("owner/repo", 1, root / "slots.json", root / "retired.json")
    ports = IssueJobAuthorizationPorts(SimpleNamespace(), "main", lambda _: True, lambda *_args: {}, lambda *_args: None, lambda *_args: None, lambda *_args: "generation")
    adapter = IssueLocalJobAdapter(ports, "owner/repo", store, slots, source)
    with (
        patch("auto_coder.llm_backend_config.get_llm_config", return_value=config),
        patch("auto_coder.cli_helpers.get_llm_config", return_value=config),
        patch("auto_coder.muse_client.get_llm_config", return_value=config),
        patch("auto_coder.issue_job_workspace.default_issue_job_workspace_root", return_value=root / "workspaces"),
        patch.object(adapter, "authorize_provider_entry", return_value=True),
        patch.object(adapter._start_notifier, "observe_and_publish"),
    ):
        manager = build_backend_manager(["muse"], "muse", {"muse": "muse-spark-1.3"})
        with patch("auto_coder.cli_helpers.build_backend_manager", return_value=manager):
            runner = LocalJobRunner(store, InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter})
            assert runner.poll() == 1
            while True:
                time.sleep(0.05)


@pytest.mark.parametrize("interruption", ["timeout", "repeated-timeout", "quota", "controller-crash"])
def test_interrupted_issue_resumes_original_edits_session_and_attempt(tmp_path, monkeypatch, _use_real_commands, _use_real_sleep, interruption):
    source = _repository(tmp_path)
    git(source, "branch", "-M", "main")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(source, "remote", "add", "origin", str(remote))
    git(source, "push", "origin", "main")
    monkeypatch.chdir(source)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(_host(tmp_path)))
    log = tmp_path / "msp.jsonl"
    monkeypatch.setenv("MSP_LOG", str(log))
    monkeypatch.setenv("MSP_SLEEP", "60" if interruption == "controller-crash" else "2")
    if interruption == "quota":
        monkeypatch.setenv("MSP_QUOTA_RESPONSE", "1")
    monkeypatch.setenv("MSP_SHELL_MUTATE", "printf 'first segment\\n' > partial.txt")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3", timeout=60 if interruption == "controller-crash" else 10 if interruption == "quota" else 1)})
    monkeypatch.setattr("auto_coder.llm_backend_config.get_llm_config", lambda **_kwargs: config)
    monkeypatch.setattr("auto_coder.cli_helpers.get_llm_config", lambda **_kwargs: config)
    monkeypatch.setattr("auto_coder.muse_client.get_llm_config", lambda **_kwargs: config)
    monkeypatch.setattr("auto_coder.issue_job_workspace.default_issue_job_workspace_root", lambda: tmp_path / "workspaces")
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json", tmp_path / "retired.json")
    owner = ImplementationOwner("issue", 41)
    execution_id = slots.start_execution(owner, generation="generation")
    assert execution_id
    identity = IssueAttemptIdentity("owner", "repo", 41, "0")
    guard = IssueDispatchGuard(tmp_path / "dispatch.sqlite3")
    assert guard.reserve(identity, CandidateHandoff("muse", "local")).admitted
    accepted = store.offer_issue(LocalJobOffer(LocalJobKind.ISSUE_IMPLEMENTATION, "owner/repo", 41, "0", "muse", "Implement and preserve partial work", execution_id), identity, guard)
    assert accepted is not None
    ports = IssueJobAuthorizationPorts(SimpleNamespace(), "main", lambda _: True, lambda *_args: {}, lambda *_args: None, lambda *_args: None, lambda *_args: "generation")

    def adapter_for(job_store):
        adapter = IssueLocalJobAdapter(ports, "owner/repo", job_store, slots, source)
        monkeypatch.setattr(adapter, "authorize_provider_entry", lambda _job: True)
        monkeypatch.setattr(adapter._start_notifier, "observe_and_publish", lambda *_args, **_kwargs: None)
        return adapter

    manager = build_backend_manager(["muse"], "muse", {"muse": "muse-spark-1.3"})
    monkeypatch.setattr("auto_coder.cli_helpers.build_backend_manager", lambda **_kwargs: manager)
    gate = InvocationAdmissionGate()
    runner = LocalJobRunner(store, gate, repository="owner/repo", capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter_for(store)})
    child = None
    if interruption == "controller-crash":
        child = get_context("spawn").Process(target=_crash_controller, args=(source, tmp_path))
        child.start()
        try:
            _wait(lambda: (retained := store.get_continuation(accepted.job_id)) is not None and bool(retained.writer_owner) and (Path(retained.provider_workspace) / "partial.txt").is_file())
        finally:
            child.kill()
            child.join(timeout=5)
            assert not child.is_alive()
            retained = store.get_continuation(accepted.job_id)
            if retained is not None and retained.writer_owner.startswith("process:"):
                os.killpg(int(retained.writer_owner.split(":")[1]), signal.SIGKILL)
                _wait(lambda: writer_group_settled(retained.writer_owner))
    else:
        assert runner.poll() == 1
        _wait(lambda: runner.active_count() == 0)
        assert gate.snapshot().unsettled == ()

    job = store.get(accepted.job_id)
    retained = store.get_continuation(accepted.job_id)
    assert job is not None and retained is not None
    assert job.state is (LocalJobState.RUNNING if child is not None else LocalJobState.PAUSED)
    if interruption == "quota":
        assert "AutoCoderUsageLimitError" in job.diagnostic
    assert not job.result_reference and job.invocation_outcome is None
    original_workspace = retained.provider_workspace
    original_session = retained.provider_session
    assert Path(original_workspace).is_relative_to(Path(job.workspace_path).parent)
    assert (Path(original_workspace) / "partial.txt").read_text() == "first segment\n"
    assert not (Path(job.workspace_path) / "partial.txt").exists()
    assert slots.active_execution_ids(owner) == (execution_id,)
    runner.close(wait=True)
    manager.close()
    gc.collect()
    assert Path(original_workspace).is_dir()

    reopened = LocalJobStore(store.path)
    recovered_gate = InvocationAdmissionGate()
    recovered = LocalJobRunner(reopened, recovered_gate, repository="owner/repo", capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter_for(reopened)})
    try:
        if interruption == "repeated-timeout":
            recovered.resume(job.job_id)
            assert reopened.get(job.job_id).state is LocalJobState.PAUSED
            assert not reopened.get(job.job_id).result_reference
            assert recovered_gate.snapshot().unsettled == ()
        config.backends["muse"].timeout = 10
        monkeypatch.delenv("MSP_SLEEP")
        monkeypatch.delenv("MSP_QUOTA_RESPONSE", raising=False)
        monkeypatch.setenv("MSP_SHELL_MUTATE", "test -f partial.txt && printf 'continued\\n' > continued.txt")
        monkeypatch.setenv("MSP_ANSWER", "ACTION_SUMMARY: continued retained implementation")
        monkeypatch.setenv("MSP_BACKGROUND_ITEM", "1")
        background_gate = tmp_path / "background-complete"
        background_gate.touch()
        monkeypatch.setenv("MSP_BACKGROUND_GATE", str(background_gate))
        monkeypatch.setenv("MSP_NATIVE_ANSWER", "ACTION_SUMMARY: continued retained implementation")
        recovered.resume(job.job_id)
        after = reopened.get(job.job_id)
        continuation = reopened.get_continuation(job.job_id)
        assert after is not None and continuation is not None
        assert after.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING
        assert (after.job_id, after.upstream_attempt, after.execution_incarnation) == (job.job_id, job.upstream_attempt, job.execution_incarnation)
        assert (continuation.provider_session, continuation.provider_workspace) == (original_session, original_workspace)
        assert (Path(after.workspace_path) / "partial.txt").read_text() == "first segment\n"
        assert (Path(after.workspace_path) / "continued.txt").read_text() == "continued\n"
        artifact = reopened.get_result_artifact(after.result_reference)
        assert artifact is not None and {"partial.txt", "continued.txt"} <= json.loads(artifact.output)["changed_files"].keys()
        pr = {"number": 91, "head": {"ref": after.work_branch}, "body": "Closes #41"}
        result = IssueJobFinalizer(reopened, slots, lambda *_args: pr, lambda *_args: None).finalize(after.job_id)
        assert result.disposition == "published"
        assert git(remote, "show", f"{after.work_branch}:partial.txt") == "first segment"
        assert git(remote, "show", f"{after.work_branch}:continued.txt") == "continued"
        assert slots.active_execution_ids(owner) == ()
        assert recovered_gate.snapshot().unsettled == ()
        frames = [json.loads(line)["frame"] for line in log.read_text().splitlines()]
        methods = [frame.get("method") for frame in frames]
        assert methods.count("session/start") == 1
        assert methods.count("session/resume") == (2 if interruption == "repeated-timeout" else 1)
        assert all(frame["params"]["sessionId"] == original_session for frame in frames if frame.get("method") == "session/resume")
        assert (source / "tracked.txt").read_text() == "unchanged\n"
        assert not (source / "partial.txt").exists()
        if child is None:
            snapshot = get_trace_collector().get_snapshot(repository="owner/repo", item_type="issue", item_number=41)
            pauses = [event for event in snapshot.events if event.stage_id == "issue.local-session-pause" and (event.facts or {}).get("job_id") == job.job_id]
            assert pauses and all(event.outcome == "deferred" for event in pauses)
    finally:
        recovered.close(wait=True)
