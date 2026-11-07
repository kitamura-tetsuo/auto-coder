"""Explicit retained Issue continuation through the production Muse transport."""

import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import click
import pytest
from click.testing import CliRunner

from auto_coder.automation_config import AutomationConfig, Candidate
from auto_coder.automation_engine import AutomationEngine
from auto_coder.cli_commands_main import process_issues
from auto_coder.execution_trace import get_trace_collector
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.invocation_admission import InvocationAdmissionGate
from auto_coder.issue_dispatch import CandidateHandoff, IssueAttemptIdentity, IssueDispatchGuard
from auto_coder.issue_job_workspace import IssueJobSource, IssueJobWorkspaceProducer
from auto_coder.issue_local_job import IssueLocalJobAdapter, issue_job_authorization_ports
from auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
from auto_coder.local_job_handoff import ContinuationState, LocalJobKind, LocalJobOffer, LocalJobState, LocalJobStore
from auto_coder.local_job_runner import LocalJobRunner, current_runner_owner
from auto_coder.muse_background_observation import MuseBackgroundObservation, MuseObservationBinding, persist_muse_observation
from auto_coder.muse_client import MuseClient

from .test_dashboard_observability import _assert_required_stage_visible, _mounted_detail
from .test_issue_job_workspace import git, repository
from .test_muse_msp import _host

_FD_CASE = "AUTO_CODER_TEST_ISOLATED_MSP_FD_CASE"
_FD_HOSTS = "AUTO_CODER_TEST_ISOLATED_MSP_HOSTS"


@pytest.fixture(params=[False, True], ids=["ordinary-parent", "occupied-parent"])
def msp_parent_descriptors(request):
    """Own the pressure descriptors; never close unrelated caller descriptors."""
    owned = []
    try:
        if request.param and os.environ.get(_FD_CASE) != request.node.nodeid:
            while not owned or owned[-1] < 1024:
                owned.append(os.open(os.devnull, os.O_RDONLY))
        yield
    finally:
        for descriptor in owned:
            os.close(descriptor)


def _isolated_fd_resume_case(nodeid, tmp_path):
    """Run the unchanged FD oracle with fresh low slots, even in a busy shard."""
    environment = os.environ.copy()
    for key in tuple(environment):
        if key == "PYTEST_ADDOPTS" or key.startswith(("AUTO_CODER_CI_TIMING_", "COV_CORE_")) or key in {"COVERAGE_FILE", "COVERAGE_PROCESS_START"}:
            environment.pop(key)
    environment[_FD_CASE] = nodeid
    hosts = tmp_path / "isolated-fd-hosts.jsonl"
    environment[_FD_HOSTS] = str(hosts)
    report = tmp_path / "isolated-fd-result.xml"
    command = [sys.executable, "-I", "-m", "pytest", "-q", "-o", "addopts=", "--timeout=30", "--timeout-method=signal", f"--junitxml={report}", nodeid]
    with subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1], env=environment, close_fds=True, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=90)
        finally:
            try:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.communicate(timeout=10)
            finally:
                _cleanup_isolated_fd_hosts(hosts, nodeid)
        assert process.returncode == 0, (stdout + stderr)[-8000:]
    cases = ET.parse(report).findall(".//testcase")
    assert len(cases) == 1 and cases[0].get("name") == nodeid.split("::")[-1]
    assert not any(cases[0].find(tag) is not None for tag in ("skipped", "failure", "error"))


def _cleanup_isolated_fd_hosts(manifest, nodeid):
    """Separate MSP sessions are not owned by the outer pytest process group."""
    if not manifest.exists():
        return
    for line in manifest.read_text().splitlines():
        record = json.loads(line)
        assert set(record) == {"case", "pid", "started"} and record["case"] == nodeid
        pid = record["pid"]
        assert type(pid) is int and pid > 1 and isinstance(record["started"], str) and record["started"].isdigit()
        try:
            raw = Path(f"/proc/{pid}/stat").read_text()
        except FileNotFoundError:
            assert not MuseClient._process_group_exists(pid), "MSP group outlived its verifiable owner"
            continue
        fields = raw[raw.rfind(")") + 2 :].split()
        assert fields[19] == record["started"] and int(fields[2]) == int(fields[3]) == pid, "MSP cleanup identity changed"
        os.killpg(pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while MuseClient._process_group_exists(pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not MuseClient._process_group_exists(pid)


def _record_isolated_fd_host(process):
    manifest = os.environ.get(_FD_HOSTS)
    if manifest is None:
        return
    staging = None
    try:
        raw = Path(f"/proc/{process.pid}/stat").read_text()
        fields = raw[raw.rfind(")") + 2 :].split()
        assert int(fields[2]) == int(fields[3]) == process.pid
        destination = Path(manifest)
        previous = destination.read_text() if destination.exists() else ""
        with tempfile.NamedTemporaryFile("w", dir=destination.parent, delete=False) as stream:
            staging = Path(stream.name)
            stream.write(previous + json.dumps({"case": os.environ[_FD_CASE], "pid": process.pid, "started": fields[19]}) + "\n")
        staging.replace(destination)
    except BaseException:
        try:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        finally:
            try:
                process.wait(timeout=5)
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
        raise
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)


@pytest.mark.parametrize("failure", ["hard-exit", "watchdog", "changed-identity"])
def test_isolated_fd_resume_cleanup_tracks_separate_host_sessions(tmp_path, _use_real_commands, _use_real_sleep, failure):
    manifest = tmp_path / "hosts.jsonl"
    case = "owned-disposable-case"
    script = """
import json, os, pathlib, subprocess, sys, time
host = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)'],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
raw = pathlib.Path(f'/proc/{host.pid}/stat').read_text()
started = raw[raw.rfind(')') + 2:].split()[19]
manifest = pathlib.Path(sys.argv[1])
staging = manifest.with_suffix('.tmp')
staging.write_text(json.dumps({'case': sys.argv[2], 'pid': host.pid, 'started': started}) + '\\n')
staging.replace(manifest)
if sys.argv[3] == 'watchdog':
    time.sleep(60)
os._exit(7)
"""
    original = None
    with subprocess.Popen([sys.executable, "-I", "-c", script, str(manifest), case, failure], start_new_session=True, close_fds=True) as controller:
        try:
            deadline = time.monotonic() + 10
            while not manifest.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert manifest.exists()
            if failure == "watchdog":
                with pytest.raises(subprocess.TimeoutExpired):
                    controller.wait(timeout=0.1)
                os.killpg(controller.pid, signal.SIGKILL)
            controller.wait(timeout=10)
            original = manifest.read_text()
            record = json.loads(original)
            assert MuseClient._process_group_exists(record["pid"])
            if failure == "changed-identity":
                manifest.write_text(json.dumps({**record, "started": str(int(record["started"]) + 1)}))
                with pytest.raises(AssertionError, match="identity changed"):
                    _cleanup_isolated_fd_hosts(manifest, case)
                assert MuseClient._process_group_exists(record["pid"])
                manifest.write_text(original)
            _cleanup_isolated_fd_hosts(manifest, case)
            assert not MuseClient._process_group_exists(record["pid"])
            _cleanup_isolated_fd_hosts(manifest, case)
        finally:
            try:
                if controller.poll() is None:
                    os.killpg(controller.pid, signal.SIGKILL)
                    controller.wait(timeout=10)
            finally:
                if original is not None:
                    manifest.write_text(original)
                _cleanup_isolated_fd_hosts(manifest, case)


def retained_job(tmp_path, backend="muse"):
    source, head = repository(tmp_path)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(source, "remote", "set-url", "origin", str(remote))
    git(source, "push", "origin", "main")
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    guard = IssueDispatchGuard(tmp_path / "dispatch.sqlite3")
    slots = ImplementationSlotRepository("owner/repo", 2, tmp_path / "slots.json", tmp_path / "retired.json")
    execution = slots.start_execution(ImplementationOwner("issue", 41), generation="generation")
    identity = IssueAttemptIdentity("owner", "repo", 41, "0")
    assert guard.reserve(identity, CandidateHandoff(backend, "local")).admitted
    accepted = store.offer_issue(LocalJobOffer(LocalJobKind.ISSUE_IMPLEMENTATION, "owner/repo", 41, "0", backend, "Implement requirement REQ-001", execution), identity, guard)
    assert accepted is not None
    dead = "process:999999999:1"
    claim = store.claim(accepted.job_id, dead)
    assert claim is not None and store.mark_provider_entered(claim)
    producer = IssueJobWorkspaceProducer(store, tmp_path / "workspaces", slots)
    workspace, _, _ = producer._prepare(claim, IssueJobSource("owner/repo", source, "main", "issue-41"))
    provider = tmp_path / "provider"
    git(tmp_path, "clone", str(workspace), str(provider))
    (provider / "partial.txt").write_text("retained edit\n")
    assert store.bind_continuation(claim, "retained/session", dead, deadline_at=time.time() - 100)
    assert store.bind_continuation_writer(accepted.job_id, dead, provider, dead)
    job = store.get(accepted.job_id)
    assert job is not None and job.source_commit == head
    return source, store, slots, job, provider


@pytest.mark.parametrize("arguments, message", [(["--resume"], "--resume requires --only"), (["--only", "41", "--resume", "--retry"], "--resume requires --only and cannot be combined with --retry")])
def test_cli_rejects_invalid_resume_before_startup(arguments, message):
    parent = click.Context(click.Command("auto-coder"))
    parent.params["force"] = True
    result = CliRunner().invoke(process_issues, arguments, parent=parent)
    assert result.exit_code == 2
    assert message in result.output


def test_engine_resume_requires_explicit_issue_target(monkeypatch):
    engine = AutomationEngine(Mock(), AutomationConfig())
    with pytest.raises(ValueError, match="resume requires explicit_only and cannot be combined with retry"):
        engine.process_single("owner/repo", "issue", 41, resume=True)
    with pytest.raises(ValueError, match="resume requires explicit_only and cannot be combined with retry"):
        engine.process_single("owner/repo", "issue", 41, explicit_only=True, force=True, retry=True, resume=True)
    monkeypatch.setattr(engine, "_create_candidate_from_single", lambda *_args, **_kwargs: Candidate(type="pr", data={"number": 41}, priority=1))
    ordinary = Mock(side_effect=AssertionError("Resume cannot dispatch a PR"))
    monkeypatch.setattr(engine, "_process_single_candidate_unified", ordinary)
    result = engine.process_single("owner/repo", "pr", 41, explicit_only=True, resume=True)
    assert result["target_outcome"] == "failed"
    assert "--resume supports Issue targets only" in result["target_reason"]
    ordinary.assert_not_called()


@pytest.mark.parametrize(
    "problem, reason",
    [
        ("live_runner", "Previous runner is live or its liveness is unknown"),
        ("unknown_runner", "Previous runner is live or its liveness is unknown"),
        ("missing_session", "Retained job has no provider session"),
        ("live_host", "Previous Muse host is live or its liveness is unknown"),
        ("legacy", "Retained session lacks its provider workspace or writer identity"),
        ("missing_workspace", "Retained workspace is unavailable"),
        ("changed_head", "Retained workspace HEAD no longer matches the accepted source"),
        ("unsupported", "Explicit session resumption currently supports Muse only"),
        ("denied", "Retained continuation authority is unavailable or superseded"),
    ],
)
def test_resume_refuses_unsafe_state_without_provider_entry(tmp_path, monkeypatch, _use_real_commands, problem, reason):
    source, store, slots, job, provider = retained_job(tmp_path)
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3", timeout=10)})
    monkeypatch.setattr("auto_coder.llm_backend_config.get_llm_config", lambda **_kwargs: config)
    adapter = IssueLocalJobAdapter(issue_job_authorization_ports(Mock(github=Mock())), "owner/repo", store, slots, source)
    monkeypatch.setattr(adapter, "authorize_recovery", lambda _job: problem != "denied")
    entered = Mock()
    monkeypatch.setattr(adapter, "resume_continuation", entered)
    if problem == "live_runner":
        with store._connect() as connection:
            connection.execute("UPDATE local_jobs SET runner_owner=? WHERE job_id=?", (current_runner_owner(), job.job_id))
    elif problem == "unknown_runner":
        with store._connect() as connection:
            connection.execute("UPDATE local_jobs SET runner_owner='opaque' WHERE job_id=?", (job.job_id,))
    elif problem == "legacy":
        with store._connect() as connection:
            connection.execute("UPDATE local_job_continuations SET provider_workspace='', writer_owner='' WHERE job_id=?", (job.job_id,))
    elif problem == "missing_session":
        with store._connect() as connection:
            connection.execute("DELETE FROM local_job_continuations WHERE job_id=?", (job.job_id,))
    elif problem == "live_host":
        assert store.bind_continuation_writer(job.job_id, job.runner_owner, provider, current_runner_owner())
    elif problem == "missing_workspace":
        assert store.bind_continuation_writer(job.job_id, job.runner_owner, tmp_path / "missing", job.runner_owner)
    elif problem == "changed_head":
        git(provider, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "unexpected")
    elif problem == "unsupported":
        monkeypatch.setattr(config, "resolve_backend_type", lambda _backend: "codex")
    runner = LocalJobRunner(store, InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter}, job_ids=frozenset({job.job_id}))
    before = store.get_continuation(job.job_id)
    with pytest.raises(RuntimeError, match=re.escape(reason)):
        runner.resume(job.job_id)
    assert store.get_continuation(job.job_id) == before
    entered.assert_not_called()
    assert (provider / "partial.txt").read_text() == "retained edit\n"
    runner.close(wait=True)


@patch("auto_coder.dashboard.ui")
def test_missing_resume_job_is_deferred_and_visible_without_fresh_dispatch(mock_ui, tmp_path, monkeypatch):
    engine = AutomationEngine(Mock(), AutomationConfig())
    engine.local_job_store = LocalJobStore(tmp_path / "jobs.sqlite3")
    slots = ImplementationSlotRepository("owner/repo", 2, tmp_path / "slots.json", tmp_path / "retired.json")
    monkeypatch.setattr(engine, "_get_implementation_slots", lambda _repo: slots)
    get_trace_collector().clear()
    result = engine._resume_explicit_issue("owner/repo", 41)
    assert result.target_outcome.value == "deferred"
    assert result.success is False
    assert result.target_reason == "Cannot resume Issue #41: No unambiguous retained local job exists for this Issue"
    diagram = _mounted_detail(mock_ui, "issue", 41)
    _assert_required_stage_visible(diagram, "session resume")
    assert "outcome: deferred" in diagram


def test_confirmed_resume_result_is_recovered_without_provider_call(tmp_path, monkeypatch, _use_real_commands):
    source, store, slots, job, provider = retained_job(tmp_path)
    continuation = store.get_continuation(job.job_id)
    assert continuation is not None
    assert store.record_candidate_output(job.job_id, continuation.executor_token, continuation.frontier, "ACTION_SUMMARY: completed before interruption")
    assert store.confirm_final_output(job.job_id, continuation.executor_token, continuation.frontier)
    continuation = store.get_continuation(job.job_id)
    adapter = IssueLocalJobAdapter(issue_job_authorization_ports(Mock(github=Mock())), "owner/repo", store, slots, source)
    monkeypatch.setattr(adapter, "authorize_recovery", lambda _job: True)
    provider_call = Mock(side_effect=AssertionError("Confirmed result must not re-enter Muse"))
    monkeypatch.setattr(adapter, "resume_continuation", provider_call)
    runner = LocalJobRunner(store, InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter})
    runner.resume(job.job_id)
    assert store.get(job.job_id).state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING
    assert (Path(job.workspace_path) / "partial.txt").read_text() == "retained edit\n"
    assert store.get_continuation(job.job_id) == continuation
    provider_call.assert_not_called()
    runner.close(wait=True)


@pytest.mark.parametrize("paused", [False, True])
def test_resume_claim_fences_competing_controller_and_old_executor(tmp_path, _use_real_commands, paused):
    _, store, _, job, _ = retained_job(tmp_path)
    if paused:
        assert store.pause_continuation(job, job.runner_owner, "interrupted")
        job = store.get(job.job_id)
        assert job is not None and job.state is LocalJobState.PAUSED
    continuation = store.get_continuation(job.job_id)
    assert continuation is not None
    assert store.claim_explicit_resume(job, continuation, current_runner_owner(), "new-token")
    assert not store.claim_explicit_resume(job, continuation, current_runner_owner(), "competing-token")
    current = store.get(job.job_id)
    assert current is not None and current.execution_incarnation == job.execution_incarnation
    assert current.upstream_attempt == job.upstream_attempt
    assert not store.take_over_continuation(job.job_id, "new-token", "stale-recovery-token", expected_runner_owner=job.runner_owner)
    assert not store.record_candidate_output(job.job_id, continuation.executor_token, continuation.frontier, "ACTION_SUMMARY: stale")
    assert store.get_continuation(job.job_id).executor_token == "new-token"


@patch("auto_coder.dashboard.ui")
@pytest.mark.parametrize("backend", ["muse", "muse-retained"])
@pytest.mark.parametrize(
    "refusal, reason",
    [
        (None, None),
        ("MSP_REPLAY_PREDECESSOR", None),
        ("MSP_REPLAY_UNKNOWN", "Muse MSP terminal belongs to an incompatible turn"),
        ("MSP_REPLAY_SESSION", "Muse MSP terminal belongs to an incompatible turn"),
        ("MSP_WRONG_RESUME_ID", "Muse MSP resumed a different session identity"),
        ("MSP_STORED_WORKSPACE", "Muse MSP session belongs to an incompatible workspace"),
        ("MSP_RESUME_MODEL", "Muse MSP session omitted or uses an incompatible model"),
    ],
)
def test_only_resume_uses_retained_muse_session_and_publishes(mock_ui, tmp_path, monkeypatch, _use_real_commands, _use_real_sleep, refusal, reason, backend):
    _exercise_retained_resume(mock_ui, tmp_path, monkeypatch, refusal, reason, backend)


def _exercise_retained_resume(mock_ui, tmp_path, monkeypatch, refusal, reason, backend):
    source, store, slots, job, provider = retained_job(tmp_path, backend)
    observation = MuseBackgroundObservation(MuseObservationBinding(backend, "retained/session", job.execution_incarnation, job.upstream_attempt, Path(job.workspace_path)), "retained-predecessor", foreground_state="running")
    assert persist_muse_observation(store, job.job_id, job.runner_owner, observation)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(source)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    monkeypatch.setenv("MSP_SHELL_MUTATE", "test -f partial.txt && printf 'continued\\n' > continued.txt")
    monkeypatch.setenv("MSP_ANSWER", "ACTION_SUMMARY: resumed implementation")
    gate = tmp_path / "background-complete"
    gate.touch()
    monkeypatch.setenv("MSP_BACKGROUND_GATE", str(gate))
    monkeypatch.setenv("MSP_BACKGROUND_ITEM", "1")
    monkeypatch.setenv("MSP_NATIVE_ANSWER", "ACTION_SUMMARY: resumed implementation")
    if refusal == "MSP_REPLAY_PREDECESSOR":
        monkeypatch.setenv("MSP_REPLAY_PREDECESSOR", "retained-predecessor")
    elif refusal == "MSP_REPLAY_UNKNOWN":
        monkeypatch.setenv("MSP_REPLAY_PREDECESSOR", "foreign-turn")
    elif refusal == "MSP_REPLAY_SESSION":
        monkeypatch.setenv("MSP_REPLAY_PREDECESSOR", "retained-predecessor")
        monkeypatch.setenv("MSP_REPLAY_SESSION", "foreign-session")
    elif refusal:
        monkeypatch.setenv(refusal, "incompatible")
    config = LLMBackendConfiguration(backends={backend: BackendConfig(name=backend, backend_type="muse", model="muse-spark-1.3", timeout=10)})
    monkeypatch.setattr("auto_coder.llm_backend_config.get_llm_config", lambda **_kwargs: config)
    monkeypatch.setattr("auto_coder.muse_client.get_llm_config", lambda **_kwargs: config)
    monkeypatch.setattr(IssueLocalJobAdapter, "authorize_recovery", lambda _self, _job: True)
    engine = AutomationEngine(Mock(), AutomationConfig())
    engine.local_job_store = store
    monkeypatch.setattr(engine, "_get_implementation_slots", lambda _repo: slots)
    monkeypatch.setattr(engine, "_check_and_handle_closed_branch", lambda _repo: True)
    monkeypatch.setattr(engine, "_create_candidate_from_single", lambda *_args, **_kwargs: Candidate(type="issue", data={"number": 41}, priority=1))
    ordinary = Mock(side_effect=AssertionError("Resume must bypass fresh dispatch"))
    monkeypatch.setattr(engine, "_process_single_candidate_unified", ordinary)
    published = Mock(return_value=[SimpleNamespace(disposition="published", pr_number=99)])
    monkeypatch.setattr(engine, "_resume_local_issue_publications", published)
    traces = get_trace_collector()
    traces.clear()
    result = engine.process_single("owner/repo", "issue", 41, explicit_only=True, resume=True)
    if reason:
        assert result["target_outcome"] == "deferred"
        assert result["target_reason"] == f"Cannot resume Issue #41: {reason}"
        frames = [json.loads(line)["frame"] for line in log.read_text().splitlines()]
        assert not any(frame.get("method") == "session/start" for frame in frames)
        if refusal not in {"MSP_REPLAY_UNKNOWN", "MSP_REPLAY_SESSION"}:
            assert not any(frame.get("method") == "turn/start" for frame in frames)
        assert store.get(job.job_id).state is LocalJobState.RUNNING
        assert store.get_continuation(job.job_id).final_output == ""
        assert (provider / "partial.txt").read_text() == "retained edit\n"
        ordinary.assert_not_called()
        published.assert_not_called()
        diagram = _mounted_detail(mock_ui, "issue", 41)
        _assert_required_stage_visible(diagram, "session resume")
        assert "outcome: deferred" in diagram
        return
    assert result["target_outcome"] == "success", result
    assert result["target_reason"] == "Published PR #99 for issue #41"
    frames = [json.loads(line)["frame"] for line in log.read_text().splitlines()]
    assert [frame.get("method") for frame in frames].count("session/resume") == 1
    assert not any(frame.get("method") == "session/start" for frame in frames)
    resumed = next(frame for frame in frames if frame.get("method") == "session/resume")
    assert resumed["params"]["sessionId"] == "retained/session"
    turn = next(frame for frame in frames if frame.get("method") == "turn/start")
    assert "Implement requirement REQ-001" in turn["params"]["input"][0]["text"]
    current = store.get(job.job_id)
    assert current is not None and current.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING
    assert current.execution_incarnation == job.execution_incarnation
    assert current.implementation_execution_id == job.implementation_execution_id
    continuation = store.get_continuation(job.job_id)
    assert continuation is not None and continuation.state is ContinuationState.FINAL_CONFIRMED
    assert continuation.provider_workspace == str(provider)
    assert continuation.deadline_at > time.time()
    artifact = store.get_result_artifact(current.result_reference)
    assert artifact is not None
    manifest = json.loads(artifact.output)
    assert manifest["output"] == "ACTION_SUMMARY: resumed implementation"
    assert {"partial.txt", "continued.txt"} <= manifest["changed_files"].keys()
    assert (Path(job.workspace_path) / "partial.txt").read_text() == "retained edit\n"
    assert (Path(job.workspace_path) / "continued.txt").read_text() == "continued\n"
    ordinary.assert_not_called()
    published.assert_called_once_with("owner/repo", slots, job_id=job.job_id, origin="explicit-single-target")
    diagram = _mounted_detail(mock_ui, "issue", 41)
    _assert_required_stage_visible(diagram, "session resume")


@patch("auto_coder.dashboard.ui")
@pytest.mark.parametrize("release_low_descriptors", [False, True])
@pytest.mark.parametrize(
    "refusal, reason, backend",
    [
        (None, None, "muse"),
        ("MSP_STORED_WORKSPACE", "Muse MSP session belongs to an incompatible workspace", "muse-retained"),
        ("MSP_RESUME_MODEL", "Muse MSP session omitted or uses an incompatible model", "muse-retained"),
    ],
)
def test_explicit_resume_with_high_msp_pipe_descriptors(mock_ui, tmp_path, monkeypatch, _use_real_commands, _use_real_sleep, refusal, reason, backend, release_low_descriptors, msp_parent_descriptors, request):
    import fcntl

    if release_low_descriptors and os.environ.get(_FD_CASE) != request.node.nodeid:
        _isolated_fd_resume_case(request.node.nodeid, tmp_path)
        return

    processes = []
    original_write = MuseClient._msp_write
    original_turn = MuseClient._run_msp_turn_observed
    original_popen = subprocess.Popen
    reserved = []
    released_pipe_descriptors = []

    def release_reserved():
        while reserved:
            os.close(reserved.pop())

    def run_after_release(client, *args, **kwargs):
        def launch_after_release(*launch_args, **launch_kwargs):
            assert len(reserved) >= 3 and max(reserved) < 1024
            release_reserved()
            process = original_popen(*launch_args, **launch_kwargs)
            _record_isolated_fd_host(process)
            descriptors = tuple(getattr(process, name).fileno() for name in ("stdin", "stdout", "stderr"))
            released_pipe_descriptors.append(descriptors)
            # Establish the counterexample before the high-FD duplication:
            # the unchanged host really acquired the newly released low slots.
            return process

        with monkeypatch.context() as launch_patch:
            launch_patch.setattr("auto_coder.muse_client.subprocess.Popen", launch_after_release)
            return original_turn(client, *args, **kwargs)

    def observe_write(client, process, payload, total_bytes, deadline):
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        if process not in processes:
            if release_low_descriptors:
                assert released_pipe_descriptors and max(released_pipe_descriptors[-1]) < 1024
            # Duplicate the real parent pipe ends instead of assuming unrelated
            # low descriptors stay open between reservation and host launch.
            for name, mode in (("stdin", "wb"), ("stdout", "rb"), ("stderr", "rb")):
                stream = getattr(process, name)
                assert stat.S_ISFIFO(os.fstat(stream.fileno()).st_mode)
                descriptor = fcntl.fcntl(stream.fileno(), fcntl.F_DUPFD_CLOEXEC, 1024)
                try:
                    replacement = os.fdopen(descriptor, mode, buffering=0)
                except BaseException:
                    os.close(descriptor)
                    raise
                stack.callback(replacement.close)
                setattr(process, name, replacement)
                stream.close()
        assert min(process.stdin.fileno(), process.stdout.fileno(), process.stderr.fileno()) >= 1024
        processes.append(process)
        return original_write(client, process, payload, total_bytes, deadline)

    monkeypatch.setattr(MuseClient, "_msp_write", observe_write)
    with ExitStack() as stack:
        if release_low_descriptors:
            stack.callback(release_reserved)
            for _ in range(64):
                descriptor = os.open(os.devnull, os.O_RDONLY)
                if descriptor >= 1024:
                    os.close(descriptor)
                    break
                reserved.append(descriptor)
            assert len(reserved) >= 3
            monkeypatch.setattr(MuseClient, "_run_msp_turn_observed", run_after_release)
        _exercise_retained_resume(mock_ui, tmp_path, monkeypatch, refusal, reason, backend)
        assert not reserved
        assert bool(released_pipe_descriptors) == release_low_descriptors
    assert processes
    assert all(process.poll() is not None for process in processes)
    for process in {process.pid: process for process in processes}.values():
        assert all(getattr(process, name).closed for name in ("stdin", "stdout", "stderr"))
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
