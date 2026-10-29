"""Executable protocol regressions for Muse MSP session ownership."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.auto_coder.cli_helpers import build_backend_manager
from src.auto_coder.durable_repair_allowance import RepairAllowanceLedger
from src.auto_coder.exceptions import AutoCoderTimeoutError, AutoCoderUsageLimitError
from src.auto_coder.execution_trace import EventKind, Outcome, get_trace_collector
from src.auto_coder.implementation_slots import ImplementationSlotRepository
from src.auto_coder.invocation_admission import InvocationAdmissionGate
from src.auto_coder.issue_dispatch import CandidateHandoff, IssueAttemptIdentity, IssueDispatchGuard
from src.auto_coder.issue_local_job import IssueJobAuthorizationPorts, IssueLocalJobAdapter
from src.auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
from src.auto_coder.local_job_handoff import ContinuationState, InvocationOutcome, LocalJobKind, LocalJobOffer, LocalJobStore
from src.auto_coder.local_job_runner import LocalJobRunner
from src.auto_coder.local_review_repair import LocalReviewRepairRequest, LocalReviewRepairStore, admit_local_repair_allowance
from src.auto_coder.local_session_continuation import LocalContinuationError
from src.auto_coder.muse_background_observation import bind_muse_job_observation, recover_muse_observation
from src.auto_coder.pr_correction_job import PRCorrectionJobAdapter, offer_pr_correction_job
from src.auto_coder.utils import bind_command_execution_cwd, reset_command_execution_cwd
from tests.utils.workspace import write_target_test_script


def _repository(path: Path) -> Path:
    repo = path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("unchanged\n")
    write_target_test_script(repo)
    subprocess.run(["git", "add", "tracked.txt", "scripts/test.sh"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.mark.parametrize("kind", ["ci", "conflict"])
@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("unresolved", [False, True])
@pytest.mark.parametrize("multiple", [False, True])
def test_pr_repair_runner_records_real_muse_failure_for_cleanup(tmp_path, monkeypatch, _use_real_commands, _use_real_sleep, kind, interrupted, unresolved, multiple):
    """Exercise the adapter, real fixture host, runner, finalizer, and cleanup."""
    from contextlib import contextmanager

    from src.auto_coder.automation_config import AutomationConfig
    from src.auto_coder.local_ci_repair_execution import CorrectionPorts, LocalCIRepairRequest
    from src.auto_coder.local_ci_repair_job import DurableCICorrection, LocalCIRepairJobPorts, build_local_ci_repair_job_adapter
    from src.auto_coder.local_conflict_repair_job import LocalConflictRepairJobAdapter
    from src.auto_coder.local_job_handoff import PRRepairAdmission
    from src.auto_coder.local_job_workspace_cleanup import reclaim_finalized_workspaces
    from src.auto_coder.pr_repair_finalizer import PRRepairJobFinalizer
    from src.auto_coder.pr_repair_workspace import PRRepairSource, PRRepairWorkspaceProducer
    from tests.test_local_conflict_repair_job import history

    source, head, base = history(tmp_path)
    host = _host(tmp_path)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "pr-cleanup-msp.jsonl"))
    monkeypatch.setenv("MSP_SERVER_VERSION", "1.4.6")
    monkeypatch.setenv("MSP_BACKGROUND_ITEM", "1")
    if not multiple:
        monkeypatch.setenv("MSP_EXIT_NONZERO", "1")
    if unresolved and not multiple:
        monkeypatch.setenv("MSP_LATE_BACKGROUND_ACTIVITY", "1")
    monkeypatch.setenv("AUTO_CODER_DEFER_LOCAL_TESTS", "1")
    monkeypatch.chdir(source)
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3", timeout=10)})
    manager = _manager(config, automatic_session_resume=False)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    job_kind = LocalJobKind.PR_CI_FAILURE if kind == "ci" else LocalJobKind.PR_MERGE_CONFLICT
    offer = LocalJobOffer(job_kind, "owner/repo", 7, "attempt", "muse", "repair")
    admission = PRRepairAdmission("owner/repo", "feature", head, "owner/repo", "main", base, "ci_failure" if kind == "ci" else "merge_conflict", "evidence", "upstream", ("checks / tests",) if kind == "ci" else (), True, True, True, True, kind == "conflict", kind == "ci")
    assert store.offer_pr_repair(offer, admission, LocalReviewRepairStore(tmp_path / "reviews.sqlite3")) is not None
    metadata = {"state": "open", "head": {"repo": {"full_name": "owner/repo"}, "ref": "feature", "sha": head}, "base": {"repo": {"full_name": "owner/repo"}, "ref": "main", "sha": base}}

    def edit(_prompt, workspace):
        token = bind_command_execution_cwd(str(workspace))
        try:
            try:
                if multiple:
                    assert manager._run_llm_cli("first")
                    monkeypatch.setenv("MSP_EXIT_NONZERO", "1")
                    if unresolved:
                        monkeypatch.setenv("MSP_LATE_BACKGROUND_ACTIVITY", "1")
                return manager._run_llm_cli("repair")
            except Exception:
                if interrupted:
                    raise KeyboardInterrupt("synthetic interruption after host failure")
                raise
        finally:
            reset_command_execution_cwd(token)

    if kind == "conflict":
        adapter = LocalConflictRepairJobAdapter(store, PRRepairSource(source), lambda *_: metadata, lambda *_: True, lambda *_: True, lambda *_: edit, workspace_producer=PRRepairWorkspaceProducer(store, tmp_path / "workspaces"))
    else:

        @contextmanager
        def authority(*_args):
            yield SimpleNamespace(allowed=True, failure_identities=("checks / tests",), reason="")

        def correction(_job, prepared):
            request = LocalCIRepairRequest("owner/repo", 7, head, "feature", True, (), AutomationConfig(), SimpleNamespace())
            ports = CorrectionPorts(lambda: True, lambda: manager, lambda: manager, lambda _d, _b, _t, started: (started(), edit("repair", prepared.workspace)), lambda _: None, lambda *_: ((), ""), lambda: True, lambda: False, lambda: None, lambda: True, lambda: None)
            return DurableCICorrection(request, "failure", [], ports, lambda _: [])

        ports = LocalCIRepairJobPorts(lambda _: PRRepairSource(source), lambda _: SimpleNamespace(get_pull_request_metadata_strict=lambda *_: metadata), authority, lambda *_: True, correction)
        adapter = build_local_ci_repair_job_adapter(store, ports, workspace_root=tmp_path / "workspaces")
    runner = LocalJobRunner(store, InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters={job_kind: adapter})
    try:
        assert runner.poll() == 1
        deadline = time.monotonic() + 20
        while runner.active_count():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        record = store.get(offer.job_id)
        assert record is not None and record.invocation_outcome is (InvocationOutcome.INTERRUPTED if interrupted else InvocationOutcome.FAILED)
        continuation = store.get_continuation(record.job_id)
        assert continuation is not None
        if multiple:
            assert continuation.writer_owner == "ambiguous:multiple-provider-sessions"
        else:
            assert continuation.state.value == record.invocation_outcome.value
        assert Path(continuation.provider_workspace).is_relative_to(Path(record.workspace_path).parent)
        PRRepairJobFinalizer(store, lambda *_: None, lambda *_: False).finalize(record.job_id)
        assert store.get(record.job_id).state.value == "settled"
        result = reclaim_finalized_workspaces(LocalJobStore(store.path), retention_seconds=0, pr_root=tmp_path / "workspaces")
        if unresolved or multiple:
            assert result[0].decision == "retained"
            assert Path(record.workspace_path).is_dir()
            assert store.get_result_artifact(record.result_reference) is not None
        else:
            assert result[0].decision == "reclaimed"
            assert not Path(record.workspace_path).parent.exists()
    finally:
        runner.close(wait=True)


def _host(path: Path) -> Path:
    host = path / "muse"
    host.write_text(
        r"""#!/usr/bin/env python3
import json, os, re, subprocess, sys, time
from pathlib import Path
if sys.argv[1:] == ["--version"]:
    print("Muse Code 1.3.0")
    raise SystemExit
assert sys.argv[1] == "serve"
if os.environ.get("MSP_REQUIRE_INITIAL_TESTS"):
    assert Path("node_modules/prepared.txt").read_text() == "dependencies-ready"
    assert Path(".agent-tmp/initial-tests.log").read_text().startswith("exit_code=1\ninitial-tests-complete")
if os.environ.get("MSP_PID_FILE"):
    Path(os.environ["MSP_PID_FILE"]).write_text(str(os.getpid()))
log = Path(os.environ["MSP_LOG"])
def emit(value):
    print(json.dumps(value), flush=True)
effective_mode = None
last_session = None
last_turn = None
def retain(payload):
    journal = os.environ.get("MSP_RETAINED_JOURNAL")
    if journal:
        stamp = time.time_ns() // 1000
        with Path(journal).open("a") as stream:
            stream.write(json.dumps({"id":str(stamp),"recorded_at":stamp,"payload":payload}) + "\n")
for line in sys.stdin:
    frame = json.loads(line)
    with log.open("a") as output:
        output.write(json.dumps({"argv": sys.argv[1:], "frame": frame}) + "\n")
    method = frame.get("method")
    if method == "initialize":
        params = frame.get("params")
        client_info = params.get("clientInfo") if isinstance(params, dict) else None
        client_name = client_info.get("name") if isinstance(client_info, dict) else None
        if os.environ.get("MSP_REJECT_INITIALIZE") or not isinstance(client_name, str) or re.fullmatch(r"[a-z0-9_]+", client_name) is None:
            emit({"jsonrpc":"2.0","id":frame["id"],"error":{"code":-32602,"data":{"kind":"invalidParams"},"message":"invalid initialize params: clientInfo.name must be a machine identifier matching ^[a-z0-9_]+$ (SS1.4.1)"}})
            continue
        if os.environ.get("MSP_PARTIAL"):
            sys.stdout.write("{")
            sys.stdout.flush()
            time.sleep(10)
        schema = {"version":1,"fingerprint":"sha256:b1e6676d624e116e2c1b150fec3192200d2cbca8ed79898e44f8921759c7872f"}
        result = {"serverInfo":{"name":"fixture","version":os.environ.get("MSP_SERVER_VERSION", "1.3.0")},"schema":schema,"capabilities":{"sessionDurability":"durable"}}
        if os.environ.get("MSP_OMIT_SERVER_INFO"):
            result.pop("serverInfo")
        if os.environ.get("MSP_SCHEMA_ALIAS"):
            result["schemaInfo"] = result.pop("schema")
        if os.environ.get("MSP_SCHEMA_NON_OBJECT"):
            result["schema"] = []
        if os.environ.get("MSP_SCHEMA_VERSION"):
            schema["version"] = os.environ["MSP_SCHEMA_VERSION"]
        if os.environ.get("MSP_SCHEMA_BOOLEAN"):
            schema["version"] = True
        if os.environ.get("MSP_SCHEMA_FLOAT"):
            schema["version"] = 1.0
        if os.environ.get("MSP_SCHEMA_FINGERPRINT"):
            schema["fingerprint"] = os.environ["MSP_SCHEMA_FINGERPRINT"]
        if os.environ.get("MSP_SCHEMA_FINGERPRINT_CASE") == "missing":
            schema.pop("fingerprint")
        elif os.environ.get("MSP_SCHEMA_FINGERPRINT_CASE") == "null":
            schema["fingerprint"] = None
        elif os.environ.get("MSP_SCHEMA_FINGERPRINT_CASE") == "blank":
            schema["fingerprint"] = " \t"
        emit({"jsonrpc":"2.0","id":frame["id"],"result":result})
        if os.environ.get("MSP_UNKNOWN_NOTIFICATION"):
            emit({"jsonrpc":"2.0","method":"host/information","params":{"extra":True}})
    elif method in ("session/start", "session/resume"):
        sid = "opaque/provider/session" if method == "session/start" else frame["params"]["sessionId"]
        if method == "session/resume" and os.environ.get("MSP_WRONG_RESUME_ID"):
            sid = "different/provider/session"
        workspace = os.environ.get("MSP_STORED_WORKSPACE", os.getcwd()) if method == "session/resume" else os.getcwd()
        session = {"sessionId":sid,"workspaceRoot":workspace}
        missing_model = os.environ.get("MSP_MISSING_MODEL") or (os.environ.get("MSP_MISSING_MODEL_RESUME") and method == "session/resume")
        if not missing_model:
            session["modelId"] = os.environ.get("MSP_RESUME_MODEL", "muse-spark-1.3") if method == "session/resume" else os.environ.get("MSP_FRESH_MODEL", "muse-spark-1.3")
        requested_mode = frame["params"].get("approvalMode") if method == "session/start" else None
        approval_mode = os.environ.get("MSP_APPROVAL_MODE")
        if approval_mode != "omit" and (requested_mode or (method == "session/resume" and os.environ.get("MSP_RESUME_DENIED"))):
            session["approvalMode"] = {"mode":approval_mode or requested_mode or "denyUnmatched"}
        if os.environ.get("MSP_REPORTED_APPROVAL_MODE"):
            session["approvalMode"] = {"mode":os.environ["MSP_REPORTED_APPROVAL_MODE"]}
        effective_mode = session.get("approvalMode", {}).get("mode")
        last_session = sid
        pending = [{"kind":"approval","approvalId":"pending-1","viewCursor":"cursor-1"}] if method == "session/resume" and os.environ.get("MSP_PENDING_RESUME") else []
        emit({"jsonrpc":"2.0","id":frame["id"],"result":{"session":session,"pendingRequests":pending}})
        if method == "session/resume" and os.environ.get("MSP_REPLAY_PREDECESSOR"):
            emit({"jsonrpc":"2.0","method":"turn/completed","params":{"sessionId":os.environ.get("MSP_REPLAY_SESSION",sid),"turnId":os.environ["MSP_REPLAY_PREDECESSOR"],"terminal":"cancelled"}})
        if os.environ.get("MSP_STOP_READING"):
            time.sleep(10)
    elif method == "session/setApprovalMode":
        ack_case = os.environ.get("MSP_APPROVAL_ACK_CASE", "completed")
        ack = {"commandId":frame["params"]["commandId"],"status":"accepted","applyOutcome":ack_case if ack_case in ("completed", "noop") else "completed","effectiveMode":{"mode":frame["params"]["mode"]}}
        if ack_case == "mismatched-command":
            ack["commandId"] = "01900000-0000-7000-8000-000000000000"
        elif ack_case == "rejected":
            ack["status"] = "rejected"
        elif ack_case == "pending":
            ack["applyOutcome"] = "pending"
        elif ack_case == "missing-mode":
            ack.pop("effectiveMode")
        elif ack_case == "permissive-mode":
            ack["effectiveMode"] = {"mode":"allowAll" if frame["params"]["mode"] == "denyUnmatched" else "denyUnmatched"}
        emit({"jsonrpc":"2.0","id":frame["id"],"result":ack})
        effective_mode = ack.get("effectiveMode", {}).get("mode")
    elif method == "task/list":
        tasks = [] if (os.environ.get("MSP_EMPTY_INVENTORY") or os.environ.get("MSP_AUTO_BACKGROUND_ITEM")) else [{"taskId":"background-task","kind":"tool","status":"inProgress","tool":"bash"}]
        emit({"jsonrpc":"2.0","id":frame["id"],"result":{"tasks":tasks}})
        if os.environ.get("MSP_BACKGROUND_ITEM") or os.environ.get("MSP_AUTO_BACKGROUND_ITEM"):
            gate = os.environ.get("MSP_BACKGROUND_GATE")
            while gate and not Path(gate).exists():
                time.sleep(0.01)
            bg_item = {"itemId":"background-task","kind":"toolCall","revision":2,"status":"completed" if os.environ.get("MSP_AUTO_BACKGROUND_ITEM") else "failed","turnId":last_turn,"tool":"bash","visibleOutput":"MUSE_BACKGROUND_RESULT\n"}
            if not os.environ.get("MSP_AUTO_BACKGROUND_ITEM"):
                bg_item["background"] = True
            emit({"jsonrpc":"2.0","method":"item/completed","params":{"sessionId":last_session,"item":bg_item}})
            native = "native-continuation"
            started_params = {"sessionId":last_session,"turnId":native,"viewCursor":"native-1"}
            if not os.environ.get("MSP_AUTO_BACKGROUND_ITEM"):
                started_params["resultReferences"] = ["background-task@2"]
            emit({"jsonrpc":"2.0","method":"turn/started","params":started_params})
            emit({"jsonrpc":"2.0","method":"item/completed","params":{"sessionId":last_session,"item":{"itemId":"native-answer","kind":"agentMessage","revision":1,"status":"completed","turnId":native,"text":os.environ.get("MSP_NATIVE_ANSWER", "answer:native")}}})
            emit({"jsonrpc":"2.0","method":"turn/completed","params":{"sessionId":last_session,"turnId":native,"terminal":"completed"}})
    elif method == "session/read":
        replay_items = []
        if os.environ.get("MSP_AUTO_BACKGROUND_ITEM"):
            replay_items = [
                {"itemId":"background-task","kind":"toolCall","revision":2,"status":"completed","turnId":last_turn,"tool":"bash","visibleOutput":"MUSE_BACKGROUND_RESULT\n"},
                {"itemId":"native-answer","kind":"agentMessage","revision":1,"status":"completed","turnId":"native-continuation","text":os.environ.get("MSP_NATIVE_ANSWER", "answer:native")},
            ]
        emit({"jsonrpc":"2.0","id":frame["id"],"result":{"session":{"sessionId":last_session},"viewCursor":"replay-1","history":{"mode":"inline","items":replay_items},"lastTurn":{"turnId":"native-continuation","terminal":"completed"}}})
        if os.environ.get("MSP_LATE_BACKGROUND_ACTIVITY"):
            emit({"jsonrpc":"2.0","method":"turn/started","params":{"sessionId":last_session,"turnId":"late-turn","viewCursor":"late-1"}})
            emit({"jsonrpc":"2.0","method":"item/updated","params":{"sessionId":last_session,"viewCursor":"late-2","item":{"itemId":"late-background-task","kind":"toolCall","background":True,"revision":1,"status":"inProgress","turnId":"late-turn","tool":"bash"}}})
    elif method == "turn/start":
        if os.environ.get("MSP_CHILD_WRITER"):
            subprocess.Popen([sys.executable, "-c", "import os,time; from pathlib import Path; time.sleep(1); Path(os.environ['MSP_CHILD_SENTINEL']).write_text('late write')"])
        if os.environ.get("MSP_ATTEMPT_EFFECTS"):
            assert effective_mode == "allowAll", "Read-only inspection must never park for approval"
            if "--disable-write" not in sys.argv[1:]:
                Path("tracked.txt").write_text("model write effect\n")
            assert "--disable-shell" not in sys.argv[1:]
            assert os.environ.get("GIT_OPTIONAL_LOCKS") == "0"
            assert os.environ.get("MUSE_DISABLE_APPROVAL_JUDGE") == "1"
            inspection = subprocess.run(["bash", "-c", "git log -1 --format=%s && git diff --exit-code && git ls-files && git status --porcelain && cat tracked.txt"], check=True, capture_output=True, text=True)
            Path(os.environ["MSP_SHELL_SENTINEL"]).write_text(inspection.stdout)
        if os.environ.get("MSP_SHELL_MUTATE"):
            assert "--disable-shell" not in sys.argv[1:]
            subprocess.run(["bash", "-c", os.environ["MSP_SHELL_MUTATE"]], check=True)
        if os.environ.get("MSP_QUOTA_RESPONSE"):
            emit({"jsonrpc":"2.0","id":frame["id"],"error":{"code":429,"message":"quota exceeded"}})
            continue
        if os.environ.get("MSP_MUTATE"):
            Path("tracked.txt").write_text("mutated\n")
        if os.environ.get("MSP_GENERATED_IGNORED"):
            Path("build").mkdir(exist_ok=True)
            Path("build/generated.js").write_text("ignored build output\n")
        if os.environ.get("MSP_SLEEP"):
            time.sleep(float(os.environ["MSP_SLEEP"]))
        turn = "turn-" + frame["params"]["commandId"]
        last_turn = turn
        retain({"kind":"run","run_id":turn,"event":{"kind":"started"}})
        ack = {"commandId":frame["params"]["commandId"],"status":"accepted","turnId":turn,"startedNewTurn":True,"disposition":"started"}
        if os.environ.get("MSP_BAD_TURN_ACK"):
            ack[os.environ["MSP_BAD_TURN_ACK"]] = None
        if not os.environ.get("MSP_EVENTS_BEFORE_ACK"):
            emit({"jsonrpc":"2.0","id":frame["id"],"result":ack})
        if os.environ.get("MSP_INTERACTIVE_METHOD"):
            request = {"jsonrpc":"2.0","method":os.environ["MSP_INTERACTIVE_METHOD"],"params":{"sessionId":frame["params"]["sessionId"],"approvalId":"pending-1","subject":{"command":"do-not-log-this-command"}}}
            if os.environ.get("MSP_INTERACTIVE_REQUEST_ID"):
                request["id"] = "server-request"
            emit(request)
            time.sleep(30)
            continue
        event_session = "wrong-session" if os.environ.get("MSP_WRONG_EVENT_SESSION") else frame["params"]["sessionId"]
        item_method = "item/updated" if os.environ.get("MSP_UNFINISHED_ITEM") else "item/completed"
        item_status = "inProgress" if os.environ.get("MSP_UNFINISHED_ITEM") else "completed"
        item_kind = os.environ.get("MSP_ITEM_KIND", "message")
        if os.environ.get("MSP_BACKGROUND_ITEM"):
            emit({"jsonrpc":"2.0","method":"item/updated","params":{"sessionId":event_session,"item":{"itemId":"background-task","kind":"toolCall","background":True,"revision":1,"status":"inProgress","turnId":turn,"tool":"bash"}}})
        if os.environ.get("MSP_AUTO_BACKGROUND_ITEM"):
            bg_out = json.dumps({"execution_state": "background_running", "session_id": 1})
            bg_upd = {"itemId": "background-task", "kind": "toolCall", "revision": 1, "status": "inProgress", "statusLine": "terminal process running", "turnId": turn, "tool": "bash", "visibleOutput": bg_out}
            emit({"jsonrpc": "2.0", "method": "item/updated", "params": {"sessionId": event_session, "item": bg_upd}})
        if not os.environ.get("MSP_OMIT_FOREGROUND_ANSWER"):
            item = {"itemId":"answer","kind":item_kind,"revision":1,"status":item_status,"turnId":turn,"text":os.environ.get("MSP_ANSWER", "answer:" + ("second" if "second" in frame["params"]["input"][0]["text"] else "first"))}
            retain({"kind":"run","run_id":turn,"event":{"kind":"assistant_message_committed","text":item["text"]}})
            if item_kind == "message":
                item["role"] = "assistant"
            emit({"jsonrpc":"2.0","method":item_method,"params":{"sessionId":event_session,"item":item}})
        terminal = "failed" if os.environ.get("MSP_QUOTA_TERMINAL") else "completed"
        terminal_params = {"sessionId":event_session,"turnId":turn,"terminal":terminal}
        if terminal == "failed":
            terminal_params["reason"] = "rate limit exceeded"
        if os.environ.get("MSP_WRONG_TERMINAL_TURN"):
            terminal_params["turnId"] = "wrong-turn"
        emit({"jsonrpc":"2.0","method":"turn/completed","params":terminal_params})
        retain({"kind":"run","run_id":turn,"event":{"kind":"terminal","terminal":terminal}})
        extra_terminal_mismatch = os.environ.get("MSP_EXTRA_TERMINAL_MISMATCH")
        if extra_terminal_mismatch and not os.environ.get("MSP_EXTRA_TERMINAL_AFTER_ACK"):
            extra_terminal_params = dict(terminal_params)
            if extra_terminal_mismatch == "session":
                extra_terminal_params["sessionId"] = "wrong-session"
            elif extra_terminal_mismatch == "turn":
                extra_terminal_params["turnId"] = "wrong-turn"
            else:
                raise AssertionError("unsupported extra terminal mismatch")
            emit({"jsonrpc":"2.0","method":"turn/completed","params":extra_terminal_params})
        if os.environ.get("MSP_EVENTS_BEFORE_ACK"):
            emit({"jsonrpc":"2.0","id":frame["id"],"result":ack})
        if extra_terminal_mismatch and os.environ.get("MSP_EXTRA_TERMINAL_AFTER_ACK"):
            time.sleep(0.05)
            extra_terminal_params = dict(terminal_params)
            if extra_terminal_mismatch == "session":
                extra_terminal_params["sessionId"] = "wrong-session"
            elif extra_terminal_mismatch == "turn":
                extra_terminal_params["turnId"] = "wrong-turn"
            else:
                raise AssertionError("unsupported extra terminal mismatch")
            emit({"jsonrpc":"2.0","method":"turn/completed","params":extra_terminal_params})
        if os.environ.get("MSP_WRONG_TERMINAL_TURN"):
            time.sleep(10)
retain({"kind":"session_end","record":{"session_id":last_session,"exit_reason":"clean"}})
if os.environ.get("MSP_EXIT_NONZERO"):
    raise SystemExit(7)
"""
    )
    host.chmod(0o700)
    return host


def _manager(
    config: LLMBackendConfiguration,
    backend_name: str = "muse",
    automatic_session_resume: bool = True,
):
    with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.muse_client.get_llm_config", return_value=config):
        return build_backend_manager(
            [backend_name],
            backend_name,
            {backend_name: "muse-spark-1.3"},
            automatic_session_resume=automatic_session_resume,
        )


def _initialize_host(host: Path, log: Path, client_info: object) -> dict[str, object]:
    env = os.environ.copy()
    env["MSP_LOG"] = str(log)
    process = subprocess.Popen(
        [str(host), "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": client_info}}
    stdout, _ = process.communicate(json.dumps(request) + "\n", timeout=5)
    assert process.returncode == 0
    return json.loads(stdout)


@pytest.mark.parametrize("is_noedit", [False, True])
@pytest.mark.parametrize("events_before_ack", [False, True])
@pytest.mark.parametrize("method", ["approval/requested", "approval/updated", "userInput/requested"])
@pytest.mark.parametrize("server_request", [False, True])
def test_muse_interactive_notification_fails_without_waiting(tmp_path, monkeypatch, _use_real_commands, is_noedit, events_before_ack, method, server_request):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    monkeypatch.setenv("MSP_INTERACTIVE_METHOD", method)
    # Both editable and no-edit default modes refuse interactive requests immediately.
    monkeypatch.setattr("src.auto_coder.muse_client._MUSE_APPROVAL_SETTLEMENT_SECONDS", 0.5)
    if server_request:
        monkeypatch.setenv("MSP_INTERACTIVE_REQUEST_ID", "1")
    if events_before_ack:
        monkeypatch.setenv("MSP_EVENTS_BEFORE_ACK", "1")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)
    client = manager._clients["muse"]
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="cannot wait for interactive request"):
        manager._run_llm_cli("first", is_noedit=is_noedit)
    assert time.monotonic() - started < 5
    assert client.get_last_session_id() is None
    assert manager.get_last_session_id() is None
    frames = [json.loads(line)["frame"] for line in log.read_text().splitlines()]
    assert sum(frame.get("method") == "turn/start" for frame in frames) == 1
    assert not any(frame.get("method") == "approval/decide" for frame in frames)


def test_implementation_workspace_runs_target_tests_before_muse(tmp_path, monkeypatch, _use_real_commands):
    repo = _repository(tmp_path)
    write_target_test_script(repo, '#!/bin/bash\nset -eu\ntest "$INSIDE_TARGET_EXECUTION" = true\ntest "$AM_I_AUTOCODER_CONTAINER" = false\nmkdir -p node_modules\nprintf dependencies-ready > node_modules/prepared.txt\nprintf initial-tests-complete\nexit 1\n')
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    monkeypatch.setenv("MSP_REQUIRE_INITIAL_TESTS", "1")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    assert _manager(config)._run_llm_cli("first") == "answer:first"
    assert not (repo / "node_modules").exists()
    assert not (repo / ".agent-tmp").exists()
    frames = [json.loads(line)["frame"] for line in log.read_text().splitlines()]
    assert sum(frame.get("method") == "turn/start" for frame in frames) == 1


@pytest.mark.parametrize("configured_noedit", [False, True])
def test_noedit_workspace_never_runs_initial_tests(tmp_path, monkeypatch, _use_real_commands, configured_noedit):
    repo = _repository(tmp_path)
    write_target_test_script(repo, "#!/bin/bash\nprintf mutated > tracked.txt\nexit 127\n")
    host = _host(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3", options=["--no-edit"] if configured_noedit else [])})
    assert _manager(config)._run_llm_cli("first", is_noedit=not configured_noedit) == "answer:first"
    assert (repo / "tracked.txt").read_text() == "unchanged\n"


def test_initial_test_launch_failure_prevents_provider_submission(tmp_path, monkeypatch, _use_real_commands):
    from src.auto_coder.worktree_utils import WorkspacePreparationError

    repo = _repository(tmp_path)
    write_target_test_script(repo, "#!/bin/bash\nexit 127\n")
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    with pytest.raises(WorkspacePreparationError, match="test script could not complete"):
        _manager(config)._run_llm_cli("first")
    assert not log.exists()


def test_initial_test_failure_cannot_report_a_previous_session(tmp_path, monkeypatch, _use_real_commands):
    from src.auto_coder.worktree_utils import WorkspacePreparationError

    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config, automatic_session_resume=False)
    client = manager._clients["muse"]
    assert manager._run_llm_cli("first") == "answer:first"
    previous_session = manager.get_last_session_id()
    assert previous_session == "opaque/provider/session"
    assert client.get_last_session_id() == previous_session
    previous_root = manager._retained_local_sessions[previous_session].binding.workspace.parent
    previous_protocol = log.read_text()
    write_target_test_script(repo, "#!/bin/bash\nexit 127\n")
    with pytest.raises(WorkspacePreparationError, match="test script could not complete"):
        manager._run_llm_cli("second")
    assert manager.get_last_session_id() is None
    assert client.get_last_session_id() is None
    assert not manager.has_retained_local_session(previous_session)
    assert not previous_root.exists()
    assert log.read_text() == previous_protocol


@pytest.mark.parametrize("returncode", [-1, 126, 127])
def test_initial_test_runner_failure_records_failure_without_launching_muse(tmp_path, monkeypatch, _use_real_commands, returncode):
    from src.auto_coder.utils import CommandResult
    from src.auto_coder.worktree_utils import WorkspacePreparationError

    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    with patch("src.auto_coder.worktree_utils.CommandExecutor.run_command", return_value=CommandResult(False, "", "runner failed", returncode)) as run:
        with pytest.raises(WorkspacePreparationError, match=f"exit_code={returncode}"):
            _manager(config)._run_llm_cli("first")
    assert run.call_count == 1
    assert run.call_args.args == (["bash", "scripts/test.sh"],)
    assert Path(run.call_args.kwargs["cwd"]) != repo
    assert not log.exists()


@pytest.mark.parametrize(
    "client_info",
    [
        {"name": "auto-coder", "version": "1"},
        {"name": "", "version": "1"},
        {"version": "1"},
        {"name": None, "version": "1"},
        {"name": 2, "version": "1"},
        {"name": "Auto_coder", "version": "1"},
        {"name": "auto_coder\n", "version": "1"},
    ],
)
def test_muse_msp_strict_host_rejects_invalid_client_names(tmp_path, client_info):
    response = _initialize_host(_host(tmp_path), tmp_path / "msp.jsonl", client_info)

    assert response["error"] == {
        "code": -32602,
        "data": {"kind": "invalidParams"},
        "message": "invalid initialize params: clientInfo.name must be a machine identifier matching ^[a-z0-9_]+$ (SS1.4.1)",
    }


def test_muse_msp_strict_host_accepts_compliant_control_name(tmp_path):
    response = _initialize_host(
        _host(tmp_path),
        tmp_path / "msp.jsonl",
        {"name": "another_client_2", "version": "1"},
    )

    assert response["result"]["serverInfo"] == {"name": "fixture", "version": "1.3.0"}


def test_muse_msp_fresh_then_exact_resume(tmp_path, monkeypatch, _use_real_commands):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)

    assert manager._run_llm_cli("first") == "answer:first"
    session_id = manager.get_last_session_id()
    assert session_id == "opaque/provider/session"
    manager.authorize_retained_local_session_reuse(session_id)
    assert manager.continue_session(session_id, "second", is_noedit=True) == "answer:second"
    assert manager._last_continue_session_resumed is True

    frames = [json.loads(line) for line in log.read_text().splitlines()]
    initializations = [entry for entry in frames if entry["frame"].get("method") == "initialize"]
    starts = [entry for entry in frames if entry["frame"].get("method") == "session/start"]
    resumes = [entry for entry in frames if entry["frame"].get("method") == "session/resume"]
    turns = [entry for entry in frames if entry["frame"].get("method") == "turn/start"]
    assert [entry["frame"]["params"]["clientInfo"]["name"] for entry in initializations] == ["auto_coder", "auto_coder"]
    assert len(starts) == 1
    assert starts[0]["frame"]["params"]["approvalMode"] == "allowAll"
    assert [entry["frame"]["params"]["sessionId"] for entry in resumes] == [session_id]
    assert len(turns) == 2
    assert turns[1]["argv"] == ["serve", "--disable-write"]
    assert turns[1]["frame"]["params"]["input"][0]["type"] == "text"
    assert (repo / "tracked.txt").read_text() == "unchanged\n"


@pytest.mark.parametrize("background_env", ["MSP_BACKGROUND_ITEM", "MSP_AUTO_BACKGROUND_ITEM"])
def test_accepted_job_production_transport_persists_background_frontier(tmp_path, monkeypatch, _use_real_commands, background_env):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    monkeypatch.setenv("MSP_SERVER_VERSION", "1.4.6")
    monkeypatch.setenv(background_env, "1")
    if background_env == "MSP_AUTO_BACKGROUND_ITEM":
        monkeypatch.setenv("MSP_OMIT_FOREGROUND_ANSWER", "1")
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    guard = IssueDispatchGuard(tmp_path / "dispatch.sqlite3")
    identity = IssueAttemptIdentity("owner", "repo", 42, "attempt-1")
    guard.reserve(identity, CandidateHandoff("muse", "local"))
    offer = LocalJobOffer(LocalJobKind.ISSUE_IMPLEMENTATION, "owner/repo", 42, "attempt-1", "muse", "implement")
    assert store.offer_issue(offer, identity, guard) is not None
    claim = store.claim(offer.job_id, "test-runner")
    assert claim is not None and claim.acquired
    assert store.bind_workspace(
        claim,
        workspace_path=repo,
        source_commit="a" * 40,
        source_ref="main",
        work_branch="work",
        publication_remote="origin",
        owner_incarnation="owner",
        owner_generation=None,
    )
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)
    bound_job = store.get(offer.job_id)
    assert bound_job is not None

    with bind_muse_job_observation(store, bound_job, bound_job.runner_owner):
        assert manager._run_llm_cli("background", is_noedit=True) == "answer:native"

    observation = recover_muse_observation(store, offer.job_id)
    assert observation is not None
    assert observation.binding.provider_session == "opaque/provider/session"
    assert observation.tasks[0].task_id == "background-task"
    assert observation.tasks[0].result_delivered
    assert observation.incorporated_results == ("background-task@2",)
    assert observation.final_assistant == "answer:native"
    assert observation.final_assistant_turn == "native-continuation"
    continuation = store.get_continuation(offer.job_id)
    assert continuation is not None
    assert continuation.state is ContinuationState.FINAL_CONFIRMED
    assert continuation.final_output == "answer:native"


def test_bound_job_rejects_final_output_when_shutdown_drain_observes_new_work(tmp_path, monkeypatch, _use_real_commands):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    monkeypatch.setenv("MSP_SERVER_VERSION", "1.4.6")
    monkeypatch.setenv("MSP_BACKGROUND_ITEM", "1")
    monkeypatch.setenv("MSP_LATE_BACKGROUND_ACTIVITY", "1")
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    guard = IssueDispatchGuard(tmp_path / "dispatch.sqlite3")
    identity = IssueAttemptIdentity("owner", "repo", 42, "attempt-late")
    guard.reserve(identity, CandidateHandoff("muse", "local"))
    offer = LocalJobOffer(LocalJobKind.ISSUE_IMPLEMENTATION, "owner/repo", 42, "attempt-late", "muse", "implement")
    assert store.offer_issue(offer, identity, guard) is not None
    claim = store.claim(offer.job_id, "test-runner")
    assert claim is not None and claim.acquired
    assert store.bind_workspace(
        claim,
        workspace_path=repo,
        source_commit="a" * 40,
        source_ref="main",
        work_branch="work",
        publication_remote="origin",
        owner_incarnation="owner",
        owner_generation=None,
    )
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    bound_job = store.get(offer.job_id)
    assert bound_job is not None

    with bind_muse_job_observation(store, bound_job, bound_job.runner_owner):
        with pytest.raises(RuntimeError, match="final output became stale"):
            _manager(config)._run_llm_cli("background", is_noedit=True)

    observation = recover_muse_observation(store, offer.job_id)
    assert observation is not None
    assert observation.final_assistant == ""
    assert observation.active_turns == ("late-turn",)
    assert [(task.task_id, task.state) for task in observation.tasks] == [
        ("background-task", "terminal"),
        ("late-background-task", "running"),
    ]
    continuation = store.get_continuation(offer.job_id)
    assert continuation is not None
    assert continuation.state is not ContinuationState.FINAL_CONFIRMED
    assert continuation.final_output == ""


@pytest.mark.parametrize("late_activity", [False, True])
def test_issue_adapter_runner_delivers_gate_controlled_muse_background_result(tmp_path, monkeypatch, _use_real_commands, _use_real_sleep, late_activity):
    """Cross admission, private workspace, Muse, runner, and downstream wake."""
    source = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=source, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=source, check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=source, check=True, capture_output=True)
    host = _host(tmp_path)
    gate = tmp_path / "release-background"
    monkeypatch.chdir(source)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    monkeypatch.setenv("MSP_SERVER_VERSION", "1.4.6")
    monkeypatch.setenv("MSP_BACKGROUND_ITEM", "1")
    monkeypatch.setenv("MSP_BACKGROUND_GATE", str(gate))
    monkeypatch.setenv("MSP_NATIVE_ANSWER", "ACTION_SUMMARY: completed after background result")
    if late_activity:
        monkeypatch.setenv("MSP_LATE_BACKGROUND_ACTIVITY", "1")
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    guard = IssueDispatchGuard(tmp_path / "dispatch.sqlite3")
    identity = IssueAttemptIdentity("owner", "repo", 73, "attempt-production")
    reserved = guard.reserve(identity, CandidateHandoff("muse", "local"))
    assert reserved.admitted
    offer = LocalJobOffer(LocalJobKind.ISSUE_IMPLEMENTATION, "owner/repo", 73, "attempt-production", "muse", "implement")
    accepted = store.offer_issue(offer, identity, guard)
    assert accepted is not None
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3", timeout=10)})
    manager = _manager(config)
    monkeypatch.setattr("auto_coder.cli_helpers.build_backend_manager", lambda **_kwargs: manager)
    ports = IssueJobAuthorizationPorts(
        github=SimpleNamespace(),
        main_branch="main",
        author_allowed=lambda _snapshot: True,
        authorize_dispatch=lambda _repository, _number, snapshot: snapshot,
        parent_number=lambda _repository, _number, _snapshot: None,
        decomposition_set=lambda _repository, _parent: None,
        implementation_generation=lambda _repository, _snapshot, _family: "generation",
    )
    slots = ImplementationSlotRepository("owner/repo", 2, tmp_path / "slots.json", tmp_path / "retired.json")
    adapter = IssueLocalJobAdapter(ports, "owner/repo", store, slots, source)
    monkeypatch.setattr(adapter, "authorize_provider_entry", lambda _job: True)
    monkeypatch.setattr(adapter._start_notifier, "observe_and_publish", lambda *_args, **_kwargs: None)
    wakes: list[str] = []
    runner = LocalJobRunner(
        store,
        InvocationAdmissionGate(),
        repository="owner/repo",
        capacity=2,
        adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter},
        completion_wake=lambda job: wakes.append(job.job_id),
    )

    assert runner.poll() == 1
    deadline = time.monotonic() + 10
    while True:
        continuation = store.get_continuation(offer.job_id)
        observation = recover_muse_observation(store, offer.job_id)
        if continuation is not None and observation is not None and any(task.state == "running" for task in observation.tasks):
            break
        assert time.monotonic() < deadline
        time.sleep(0.01)
    waiting = store.get(offer.job_id)
    assert waiting is not None and waiting.workspace_path and waiting.state.value == "running"
    assert wakes == []
    gate.touch()
    while runner.active_count():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    completed = store.get(offer.job_id)
    continuation = store.get_continuation(offer.job_id)
    assert continuation is not None and continuation.provider_session == "opaque/provider/session"
    if late_activity:
        assert completed is not None and completed.state.value == "downstream_effects_pending"
        artifact = store.get_result_artifact(completed.result_reference)
        assert artifact is not None and artifact.outcome is InvocationOutcome.FAILED
        assert artifact.output == "RuntimeError: Muse final output became stale while newer session activity remained unresolved"
        assert continuation.state is ContinuationState.FAILED
        assert continuation.final_output == ""
        observation = recover_muse_observation(store, offer.job_id)
        assert observation is not None and any(task.task_id == "late-background-task" and task.state == "running" for task in observation.tasks)
        while not wakes:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert wakes == [offer.job_id]
        runner.close(wait=True)
        return
    assert completed is not None and completed.state.value == "downstream_effects_pending"
    artifact = store.get_result_artifact(completed.result_reference)
    assert artifact is not None
    manifest = json.loads(artifact.output)
    assert manifest["output"] == "ACTION_SUMMARY: completed after background result"
    while not wakes:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert wakes == [offer.job_id]
    assert continuation.state is ContinuationState.FINAL_CONFIRMED
    runner.close(wait=True)


def test_pr_correction_adapter_runner_delivers_gate_controlled_muse_background_result(tmp_path, monkeypatch, _use_real_commands, _use_real_sleep):
    """Cross PR admission, exact-head worktree, Muse, runner, and validation wake."""
    source = _repository(tmp_path)
    remote = tmp_path / "pr-remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=source, check=True)
    subprocess.run(["git", "branch", "-M", "repair"], cwd=source, check=True)
    subprocess.run(["git", "push", "origin", "repair"], cwd=source, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True).stdout.strip()
    host = _host(tmp_path)
    gate = tmp_path / "release-pr-background"
    monkeypatch.chdir(source)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "pr-msp.jsonl"))
    monkeypatch.setenv("MSP_SERVER_VERSION", "1.4.6")
    monkeypatch.setenv("MSP_BACKGROUND_ITEM", "1")
    monkeypatch.setenv("MSP_BACKGROUND_GATE", str(gate))
    monkeypatch.setenv("MSP_NATIVE_ANSWER", "ACTION_SUMMARY: corrected after background result")
    jobs = LocalJobStore(tmp_path / "pr-jobs.sqlite3")
    repairs = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    ledger = RepairAllowanceLedger(tmp_path / "allowance.sqlite3")
    request = LocalReviewRepairRequest("owner/repo", 91, "owner/repo", "repair", head, ("finding-1",), "correct the finding")
    authority, reason = admit_local_repair_allowance(request, ledger)
    assert authority is not None, reason
    accepted = offer_pr_correction_job(request, "muse", store=jobs, repair_store=repairs, allowance_ledger=ledger)
    assert accepted is not None
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3", timeout=10)})
    manager = _manager(config)

    def executor_factory(_backend_name, _request):  # type: ignore[no-untyped-def]
        def execute(_current_request, worktree):  # type: ignore[no-untyped-def]
            token = bind_command_execution_cwd(worktree)
            try:
                return manager._run_llm_cli(_current_request.prompt)
            finally:
                reset_command_execution_cwd(token)

        return execute

    class CurrentPRAdapter(PRCorrectionJobAdapter):
        def _authority_is_current(self, _job):  # type: ignore[no-untyped-def]
            return True

    adapter = CurrentPRAdapter(SimpleNamespace(), executor_factory, jobs)
    wakes: list[str] = []
    runner = LocalJobRunner(
        jobs,
        InvocationAdmissionGate(),
        repository="owner/repo",
        capacity=2,
        adapters={LocalJobKind.PR_REVIEW_CORRECTION: adapter},
        completion_wake=lambda job: wakes.append(job.job_id),
    )
    with (
        patch("src.auto_coder.pr_correction_job.LocalReviewRepairStore", return_value=repairs),
        patch("src.auto_coder.pr_correction_job.RepairAllowanceLedger", return_value=ledger),
        patch("src.auto_coder.llm_backend_config.get_llm_config", return_value=config),
    ):
        assert runner.poll() == 1
        deadline = time.monotonic() + 10
        while True:
            observation = recover_muse_observation(jobs, accepted.job_id)
            if observation is not None and any(task.state == "running" for task in observation.tasks):
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        waiting = jobs.get(accepted.job_id)
        assert waiting is not None and waiting.workspace_path and waiting.state.value == "running"
        assert wakes == []
        gate.touch()
        while runner.active_count():
            assert time.monotonic() < deadline
            time.sleep(0.01)
    completed = jobs.get(accepted.job_id)
    assert completed is not None and completed.state.value == "downstream_effects_pending"
    artifact = jobs.get_result_artifact(completed.result_reference)
    assert artifact is not None
    outcome = json.loads(artifact.output)
    assert outcome["phase"] == "completed_no_change"
    continuation = jobs.get_continuation(accepted.job_id)
    assert continuation is not None
    assert continuation.provider_session == "opaque/provider/session"
    assert continuation.state is ContinuationState.FINAL_CONFIRMED
    while not wakes:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert wakes == [accepted.job_id]
    retained = repairs.get(request)
    assert retained is not None and retained.invocation_entered
    runner.close(wait=True)


def test_muse_msp_named_backend_uses_protocol_client_name(tmp_path, monkeypatch, _use_real_commands):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    config = LLMBackendConfiguration(
        backends={
            "muse-review": BackendConfig(
                name="muse-review",
                backend_type="muse",
                model="muse-spark-1.3",
            )
        }
    )
    manager = _manager(config, "muse-review")

    assert manager._run_llm_cli("first") == "answer:first"
    session_id = manager.get_last_session_id()
    assert session_id == "opaque/provider/session"
    manager.authorize_retained_local_session_reuse(session_id)
    assert manager.continue_session(session_id, "second", is_noedit=True) == "answer:second"

    frames = [json.loads(line)["frame"] for line in log.read_text().splitlines()]
    initializations = [frame for frame in frames if frame.get("method") == "initialize"]
    assert [frame["params"]["clientInfo"]["name"] for frame in initializations] == ["auto_coder", "auto_coder"]
    assert [frame["method"] for frame in frames].count("session/start") == 1
    assert [frame["params"]["sessionId"] for frame in frames if frame.get("method") == "session/resume"] == [session_id]
    assert [frame["method"] for frame in frames].count("turn/start") == 2


@pytest.mark.parametrize("continuation", [False, True])
def test_muse_msp_initialize_refusal_clears_prior_session_and_stops_protocol(
    tmp_path,
    monkeypatch,
    _use_real_commands,
    continuation,
):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config, automatic_session_resume=False)
    client = manager._clients["muse"]
    assert manager._run_llm_cli("first") == "answer:first"
    session_id = client.get_last_session_id()
    assert session_id == "opaque/provider/session"
    if continuation:
        manager.authorize_retained_local_session_reuse(session_id)

    log.unlink()
    monkeypatch.setenv("MSP_REJECT_INITIALIZE", "1")
    with pytest.raises(RuntimeError) as raised:
        if continuation:
            manager.continue_session(session_id, "second", is_noedit=True)
        else:
            manager._run_llm_cli("second", is_noedit=True)

    error = str(raised.value)
    assert "-32602" in error
    assert "invalidParams" in error
    assert "clientInfo.name must be a machine identifier" in error
    assert client.get_last_session_id() is None
    assert manager.get_last_session_id() is None
    methods = [json.loads(line)["frame"].get("method") for line in log.read_text().splitlines()]
    assert methods == ["initialize"]


@pytest.mark.parametrize("is_noedit", [False, True])
def test_muse_msp_explicit_disable_approval_uses_only_wire_mode(tmp_path, monkeypatch, _use_real_commands, is_noedit):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3", options=["--disable-approval"])})

    assert _manager(config)._run_llm_cli("first", is_noedit=is_noedit) == "answer:first"
    start = next(json.loads(line) for line in log.read_text().splitlines() if json.loads(line)["frame"].get("method") == "session/start")
    assert start["argv"] == (["serve", "--disable-write"] if is_noedit else ["serve"])
    assert start["frame"]["params"]["approvalMode"] == "denyUnmatched"


def test_muse_msp_configured_no_edit_maps_all_restrictions_for_editable_caller(tmp_path, monkeypatch, _use_real_commands):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3", options=["--no-edit"])})

    assert _manager(config)._run_llm_cli("first") == "answer:first"
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    start = next(entry for entry in entries if entry["frame"].get("method") == "session/start")
    assert start["argv"] == ["serve", "--disable-write"]
    assert "--disable-approval" not in start["argv"]
    assert start["frame"]["params"]["approvalMode"] == "allowAll"
    assert any(entry["frame"].get("method") == "turn/start" for entry in entries)


@pytest.mark.parametrize("approval_mode", ["omit", "denyUnmatched", "onRequest", "promptUnmatched"])
def test_muse_msp_fresh_noedit_rejects_unconfirmed_approval_before_turn(tmp_path, monkeypatch, _use_real_commands, approval_mode):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    monkeypatch.setenv("MSP_APPROVAL_MODE", approval_mode)
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})

    with pytest.raises(RuntimeError, match="fresh session did not confirm approval mode allowAll"):
        _manager(config)._run_llm_cli("first", is_noedit=True)
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert any(entry["frame"].get("method") == "session/start" for entry in entries)
    assert not any(entry["frame"].get("method") == "turn/start" for entry in entries)


@pytest.mark.parametrize("approval_mode", ["omit", "promptUnmatched", "denyUnmatched", "onRequest"])
def test_muse_msp_editable_fresh_rejects_unconfirmed_allow_all_before_turn(tmp_path, monkeypatch, _use_real_commands, approval_mode):
    repo = _repository(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(_host(tmp_path)))
    monkeypatch.setenv("MSP_LOG", str(log))
    monkeypatch.setenv("MSP_APPROVAL_MODE", approval_mode)
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)

    with pytest.raises(RuntimeError, match="fresh session did not confirm approval mode allowAll"):
        manager._run_llm_cli("first")
    frames = [json.loads(line)["frame"] for line in log.read_text().splitlines()]
    start = next(frame for frame in frames if frame.get("method") == "session/start")
    assert start["params"]["approvalMode"] == "allowAll"
    assert not any(frame.get("method") == "turn/start" for frame in frames)
    assert manager.get_last_session_id() is None


@pytest.mark.parametrize(
    ("extra_option", "expected_argv"),
    [
        ("--disable-approval", ["serve"]),
        ("--no-edit", ["serve", "--disable-write"]),
    ],
)
def test_muse_msp_one_shot_denial_does_not_leak(tmp_path, monkeypatch, _use_real_commands, extra_option, expected_argv):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)
    client = manager._clients["muse"]

    client.set_extra_args([extra_option])
    assert manager._run_llm_cli("first") == "answer:first"
    assert manager._run_llm_cli("second") == "answer:second"
    starts = [json.loads(line)["frame"] for line in log.read_text().splitlines() if json.loads(line)["frame"].get("method") == "session/start"]
    assert starts[0]["params"]["approvalMode"] == ("denyUnmatched" if extra_option == "--disable-approval" else "allowAll")
    first_start = next(json.loads(line) for line in log.read_text().splitlines() if json.loads(line)["frame"] == starts[0])
    assert first_start["argv"] == expected_argv
    assert starts[1]["params"]["approvalMode"] == "allowAll"


def _assert_uuid7(value: str) -> None:
    import uuid

    parsed = uuid.UUID(value)
    assert parsed.version == 7
    assert parsed.variant == uuid.RFC_4122


@pytest.mark.parametrize("disable_shell", [False, True])
def test_muse_msp_noedit_maps_host_and_wire_options(tmp_path, monkeypatch, _use_real_commands, disable_shell):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    config = LLMBackendConfiguration(
        backends={
            "muse": BackendConfig(
                name="muse",
                backend_type="muse",
                model="muse-spark-1.3",
                options_for_noedit=["--model=muse-spark-1.3", "--reasoning-effort", "high", "--no-edit", *(["--disable-shell"] if disable_shell else [])],
            )
        }
    )

    assert _manager(config)._clients["muse"]._run_llm_cli("complete prompt", is_noedit=True) == "answer:first"
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    start = next(entry for entry in entries if entry["frame"].get("method") == "session/start")
    turn = next(entry for entry in entries if entry["frame"].get("method") == "turn/start")
    assert start["argv"] == (["serve", "--disable-shell", "--disable-write"] if disable_shell else ["serve", "--disable-write"])
    assert start["frame"]["params"]["approvalMode"] == "allowAll"
    assert "--model" not in start["argv"]
    assert turn["frame"]["params"]["reasoningEffort"] == "high"
    assert isinstance(turn["frame"]["params"]["input"], list)
    prompt = turn["frame"]["params"]["input"][0]["text"]
    assert "submit simple read-only commands with literal arguments" in prompt
    assert "Read exit codes from the tool result" in prompt
    assert "including `$?`" in prompt
    assert "Quote literal path arguments" in prompt
    assert "'client/src/routes/[project]/[page]/diff/+layout.svelte'" in prompt
    assert "allowAll for implementation and no-edit" in prompt
    assert "complete prompt" in prompt
    for command in (start, turn):
        _assert_uuid7(command["frame"]["params"]["commandId"])
    assert start["frame"]["params"]["commandId"] != turn["frame"]["params"]["commandId"]


@pytest.mark.parametrize("apply_outcome", ["completed", "noop"])
def test_muse_msp_resume_reestablishes_automatic_approval(tmp_path, monkeypatch, _use_real_commands, apply_outcome):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    monkeypatch.setenv("MSP_APPROVAL_ACK_CASE", apply_outcome)
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})

    manager = _manager(config)
    client = manager._clients["muse"]
    assert client.continue_session("opaque/provider/session", "second", is_noedit=True) == "answer:second"
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    resume = next(entry for entry in entries if entry["frame"].get("method") == "session/resume")
    approval = next(entry for entry in entries if entry["frame"].get("method") == "session/setApprovalMode")
    turn = next(entry for entry in entries if entry["frame"].get("method") == "turn/start")
    assert resume["frame"]["params"].keys() == {"commandId", "sessionId"}
    assert approval["frame"]["params"]["mode"] == "allowAll"
    assert approval["frame"]["params"]["sessionId"] == "opaque/provider/session"
    command_ids = [entry["frame"]["params"]["commandId"] for entry in (resume, approval, turn)]
    assert len(command_ids) == len(set(command_ids))
    for command_id in command_ids:
        _assert_uuid7(command_id)


@pytest.mark.parametrize(
    "ack_case",
    ["mismatched-command", "rejected", "pending", "missing-mode", "permissive-mode"],
)
@pytest.mark.parametrize("is_noedit", [False, True])
def test_muse_msp_resume_rejects_unverified_approval_change_before_turn(tmp_path, monkeypatch, _use_real_commands, ack_case, is_noedit):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)
    client = manager._clients["muse"]

    assert manager._run_llm_cli("first") == "answer:first"
    session_id = manager.get_last_session_id()
    assert session_id == "opaque/provider/session"
    manager.authorize_retained_local_session_reuse(session_id)
    log.unlink()
    monkeypatch.setenv("MSP_APPROVAL_ACK_CASE", ack_case)

    with pytest.raises(RuntimeError):
        manager.continue_session(session_id, "second", is_noedit=is_noedit)

    frames = [json.loads(line)["frame"] for line in log.read_text().splitlines()]
    assert [frame.get("method") for frame in frames].count("session/resume") == 1
    assert [frame.get("method") for frame in frames].count("session/setApprovalMode") == 1
    assert not any(frame.get("method") == "turn/start" for frame in frames)
    assert client.get_last_session_id() is None
    assert manager.get_last_session_id() is None


@pytest.mark.parametrize("stored_mode", ["onRequest", "denyUnmatched", "allowAll"])
@pytest.mark.parametrize("is_noedit", [False, True])
def test_muse_msp_resume_establishes_allow_all(tmp_path, monkeypatch, _use_real_commands, stored_mode, is_noedit):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)

    assert manager._run_llm_cli("first") == "answer:first"
    session_id = manager.get_last_session_id()
    assert session_id == "opaque/provider/session"
    manager.authorize_retained_local_session_reuse(session_id)
    monkeypatch.setenv("MSP_REPORTED_APPROVAL_MODE", stored_mode)
    assert manager.continue_session(session_id, "second", is_noedit=is_noedit) == "answer:second"

    frames = [json.loads(line)["frame"] for line in log.read_text().splitlines()]
    assert sum(frame.get("method") == "session/resume" for frame in frames) == 1
    changes = [frame for frame in frames if frame.get("method") == "session/setApprovalMode"]
    assert len(changes) == (0 if stored_mode == "allowAll" else 1)
    if changes:
        assert changes[0]["params"]["mode"] == "allowAll"
        assert changes[0]["params"]["sessionId"] == session_id
    assert sum(frame.get("method") == "turn/start" for frame in frames) == 2


@pytest.mark.parametrize(
    "ack_case",
    ["mismatched-command", "rejected", "pending", "missing-mode", "permissive-mode"],
)
def test_muse_manager_refuses_unretained_session_before_approval_protocol(tmp_path, monkeypatch, _use_real_commands, ack_case):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    monkeypatch.setenv("MSP_APPROVAL_ACK_CASE", ack_case)
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})

    with pytest.raises(LocalContinuationError, match="no retained controller-owned binding"):
        _manager(config).continue_session("opaque/provider/session", "second", is_noedit=True)
    assert not log.exists()


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (["--trust-workspace"], "without independent PR-review authorization"),
        (["--yolo"], "not representable through MSP"),
        (["--disable-sandbox"], "not representable through MSP"),
        (["--reasoning-effort=extreme"], "reasoning effort is not supported"),
    ],
)
def test_muse_msp_rejects_unmapped_semantics_before_host(tmp_path, monkeypatch, _use_real_commands, options, message):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3", options=options)})

    with pytest.raises(RuntimeError, match=message):
        _manager(config)._run_llm_cli("prompt")
    assert not log.exists()


@pytest.mark.parametrize("is_noedit", [False, True])
def test_muse_msp_continuation_rejects_workspace_trust_before_protocol_setup(tmp_path, monkeypatch, _use_real_commands, is_noedit):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3", options=["--trust-workspace"])})

    client = _manager(config)._clients["muse"]
    with pytest.raises(RuntimeError, match="without independent PR-review authorization"):
        client.continue_session("opaque/provider/session", "prompt", is_noedit=is_noedit)
    assert not log.exists()


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({"MSP_SCHEMA_ALIAS": "1"}, "missing or non-object schema"),
        ({"MSP_SCHEMA_NON_OBJECT": "1"}, "missing or non-object schema"),
        ({"MSP_SCHEMA_VERSION": "1"}, "unsupported schema.version"),
        ({"MSP_SCHEMA_BOOLEAN": "1"}, "unsupported schema.version"),
        ({"MSP_SCHEMA_FLOAT": "1"}, "unsupported schema.version"),
        ({"MSP_SCHEMA_FINGERPRINT_CASE": "missing"}, "invalid schema.fingerprint"),
        ({"MSP_SCHEMA_FINGERPRINT_CASE": "null"}, "invalid schema.fingerprint"),
        ({"MSP_SCHEMA_FINGERPRINT_CASE": "blank"}, "invalid schema.fingerprint"),
    ],
)
def test_muse_msp_rejects_incompatible_initialization_before_session(tmp_path, monkeypatch, _use_real_commands, environment, message):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})

    with pytest.raises(RuntimeError, match=message):
        _manager(config)._run_llm_cli("prompt")
    methods = [json.loads(line)["frame"].get("method") for line in log.read_text().splitlines()]
    assert methods == ["initialize"]


@pytest.mark.parametrize("host_version", ["1.3.0", "1.4.1", "9.8.7-diagnostic-only"])
@pytest.mark.parametrize(
    "fingerprint",
    [
        "sha256:b1e6676d624e116e2c1b150fec3192200d2cbca8ed79898e44f8921759c7872f",
        "sha256:e0e163db6ccf00dbe68402ce55d6319b3edc33c421f31e9583b587b2de8a118f",
    ],
)
def test_muse_msp_host_version_does_not_control_schema_admission(tmp_path, monkeypatch, _use_real_commands, host_version, fingerprint):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    monkeypatch.setenv("MSP_SERVER_VERSION", host_version)
    monkeypatch.setenv("MSP_SCHEMA_FINGERPRINT", fingerprint)
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})

    assert _manager(config)._run_llm_cli("first", is_noedit=True) == "answer:first"


@pytest.mark.parametrize(
    "fingerprint",
    [
        "sha256:61afea3112e0906e9dc3a536144278a74cb4b36fc6e20901a91d4432ba3568e2",
        "peer-generated-one",
        "opaque value",
    ],
)
def test_muse_msp_unknown_fingerprint_warns_and_runs_contract_checks(tmp_path, monkeypatch, _use_real_commands, fingerprint):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    warnings: list[str] = []
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    monkeypatch.setenv("MSP_SCHEMA_FINGERPRINT", fingerprint)
    monkeypatch.setenv("MSP_UNKNOWN_NOTIFICATION", "1")
    monkeypatch.setenv("MSP_OMIT_SERVER_INFO", "1")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})

    with patch("src.auto_coder.muse_client.logger.warning", side_effect=lambda message: warnings.append(str(message))):
        assert _manager(config)._run_llm_cli("first", is_noedit=True) == "answer:first"

    methods = [json.loads(line)["frame"].get("method") for line in log.read_text().splitlines()]
    assert methods == ["initialize", "initialized", "session/start", "turn/start"]
    compatibility_warnings = [message for message in warnings if "diagnostic reference set" in message]
    assert len(compatibility_warnings) == 1
    assert fingerprint in compatibility_warnings[0]
    assert "schema version=1" in compatibility_warnings[0]
    assert "host version=None" in compatibility_warnings[0]
    assert "continues with required runtime contract checks" in compatibility_warnings[0]


@pytest.mark.parametrize("resumed_model, accepted", [("muse-spark-1.3-contributor", True), ("other-model", False)])
def test_muse_resume_accepts_only_explicit_effective_model_alias_independent_of_fingerprint(tmp_path, monkeypatch, _use_real_commands, resumed_model, accepted):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    monkeypatch.setenv("MSP_SERVER_VERSION", "unfamiliar-build")
    monkeypatch.setenv("MSP_SCHEMA_FINGERPRINT", "opaque-unknown-fingerprint")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config, automatic_session_resume=False)

    assert manager._run_llm_cli("first", is_noedit=True) == "answer:first"
    session_id = manager.get_last_session_id()
    manager.authorize_retained_local_session_reuse(session_id)
    monkeypatch.setenv("MSP_RESUME_MODEL", resumed_model)
    if accepted:
        assert manager.continue_session(session_id, "second", is_noedit=True) == "answer:second"
        assert manager.get_last_session_id() == session_id
    else:
        with pytest.raises(RuntimeError, match="incompatible model"):
            manager.continue_session(session_id, "second", is_noedit=True)
        assert manager.get_last_session_id() is None
        methods = [json.loads(line)["frame"].get("method") for line in log.read_text().splitlines()]
        assert methods.count("turn/start") == 1


def test_muse_fresh_session_accepts_explicit_model_alias_with_unknown_fingerprint(tmp_path, monkeypatch, _use_real_commands):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    monkeypatch.setenv("MSP_SCHEMA_FINGERPRINT", "fresh-unknown")
    monkeypatch.setenv("MSP_FRESH_MODEL", "muse-spark-1.3-contributor")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})

    assert _manager(config)._run_llm_cli("first", is_noedit=True) == "answer:first"


def test_muse_141_schema_agent_message_and_buffered_terminal_reach_caller(tmp_path, monkeypatch, _use_real_commands):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    monkeypatch.setenv("MSP_SERVER_VERSION", "1.4.1")
    monkeypatch.setenv("MSP_SCHEMA_FINGERPRINT", "sha256:e0e163db6ccf00dbe68402ce55d6319b3edc33c421f31e9583b587b2de8a118f")
    monkeypatch.setenv("MSP_ITEM_KIND", "agentMessage")
    monkeypatch.setenv("MSP_EVENTS_BEFORE_ACK", "1")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})

    assert _manager(config)._run_llm_cli("first", is_noedit=True) == "answer:first"


@pytest.mark.parametrize("field", ["commandId", "status", "turnId", "startedNewTurn", "disposition"])
def test_muse_msp_rejects_invalid_turn_acknowledgement(tmp_path, monkeypatch, _use_real_commands, field):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    monkeypatch.setenv("MSP_BAD_TURN_ACK", field)
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})

    with pytest.raises(RuntimeError, match="acknowledgement|acknowledge"):
        _manager(config)._run_llm_cli("prompt")


def test_muse_failed_post_turn_invariant_does_not_expose_session(tmp_path, monkeypatch, _use_real_commands):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    monkeypatch.setenv("MSP_MUTATE", "1")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)
    client = manager._clients["muse"]

    with pytest.raises(RuntimeError, match="Git-state invariant"):
        client._run_llm_cli("first", is_noedit=True)

    assert client.get_last_session_id() is None
    assert (repo / "tracked.txt").read_text() == "unchanged\n"


def test_muse_editable_git_state_is_preserved_for_shared_handoff(tmp_path, monkeypatch, _use_real_commands):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    monkeypatch.setenv("MSP_MUTATE", "1")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})

    assert _manager(config)._run_llm_cli("edit the source") == "answer:first"
    assert (repo / "tracked.txt").read_text() == "mutated\n"

    turn = next(json.loads(line) for line in log.read_text().splitlines() if json.loads(line)["frame"].get("method") == "turn/start")
    prompt = turn["frame"]["params"]["input"][0]["text"]
    assert "local Git operations" in prompt
    assert "original result root" in prompt


def test_muse_direct_edit_refuses_before_provider_submission(tmp_path, monkeypatch, _use_real_commands):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    monkeypatch.setenv("MSP_MUTATE", "1")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)
    client = manager._clients["muse"]

    with pytest.raises(RuntimeError, match="controller-owned local workspace binding"):
        client._run_llm_cli("edit the source")

    assert not log.exists()
    assert (repo / "tracked.txt").read_text() == "unchanged\n"


@pytest.mark.parametrize("times_out", [False, True])
def test_muse_settles_descendant_writer_before_return(tmp_path, monkeypatch, _use_real_commands, times_out):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    sentinel = tmp_path / "late-child-write"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    monkeypatch.setenv("MSP_CHILD_WRITER", "1")
    monkeypatch.setenv("MSP_CHILD_SENTINEL", str(sentinel))
    if times_out:
        monkeypatch.setenv("MSP_SLEEP", "2")
    config = LLMBackendConfiguration(
        backends={
            "muse": BackendConfig(
                name="muse",
                backend_type="muse",
                model="muse-spark-1.3",
                timeout=1 if times_out else 30,
            )
        }
    )
    manager = _manager(config)

    if times_out:
        with pytest.raises(AutoCoderTimeoutError):
            manager._run_llm_cli("edit the source")
    else:
        assert manager._run_llm_cli("edit the source") == "answer:first"

    time.sleep(1.2)
    assert not sentinel.exists()


def test_muse_wrong_terminal_turn_is_immediate_protocol_failure(tmp_path, monkeypatch, _use_real_commands):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    monkeypatch.setenv("MSP_WRONG_TERMINAL_TURN", "1")
    config = LLMBackendConfiguration(
        backends={
            "muse": BackendConfig(
                name="muse",
                backend_type="muse",
                model="muse-spark-1.3",
                timeout=10,
            )
        }
    )

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="terminal belongs to an incompatible turn"):
        _manager(config)._run_llm_cli("inspect", is_noedit=True)
    assert time.monotonic() - started < 5


@pytest.mark.parametrize(
    ("mismatch", "message"),
    [
        ("session", "terminal belongs to an incompatible session"),
        ("turn", "terminal belongs to an incompatible turn"),
    ],
)
def test_muse_buffered_terminal_after_valid_terminal_still_fails_continuation(tmp_path, monkeypatch, _use_real_commands, mismatch, message):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config, automatic_session_resume=False)
    client = manager._clients["muse"]

    assert manager._run_llm_cli("first", is_noedit=True) == "answer:first"
    session_id = manager.get_last_session_id()
    assert session_id == "opaque/provider/session"
    manager.authorize_retained_local_session_reuse(session_id)
    monkeypatch.setenv("MSP_EVENTS_BEFORE_ACK", "1")
    monkeypatch.setenv("MSP_EXTRA_TERMINAL_MISMATCH", mismatch)

    with pytest.raises(RuntimeError, match=message):
        manager.continue_session(session_id, "second", is_noedit=True)

    assert client.get_last_session_id() is None
    assert manager.get_last_session_id() is None
    assert manager._last_continue_session_resumed is False


@pytest.mark.parametrize(
    ("mismatch", "message"),
    [
        ("session", "terminal belongs to an incompatible session"),
        ("turn", "terminal belongs to an incompatible turn"),
    ],
)
def test_muse_post_ack_terminal_after_valid_terminal_still_fails(tmp_path, monkeypatch, _use_real_commands, mismatch, message):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    monkeypatch.setenv("MSP_EXTRA_TERMINAL_MISMATCH", mismatch)
    monkeypatch.setenv("MSP_EXTRA_TERMINAL_AFTER_ACK", "1")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config, automatic_session_resume=False)
    client = manager._clients["muse"]

    with pytest.raises(RuntimeError, match=message):
        manager._run_llm_cli("first", is_noedit=True)

    assert client.get_last_session_id() is None
    assert manager.get_last_session_id() is None


def test_muse_nonzero_exit_invalidates_completed_protocol(tmp_path, monkeypatch, _use_real_commands):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    monkeypatch.setenv("MSP_EXIT_NONZERO", "1")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)

    with pytest.raises(RuntimeError, match="nonzero status 7"):
        manager._run_llm_cli("edit the source")
    assert manager.get_last_session_id() is None


def test_muse_timeout_classification_survives_invariant_failure(tmp_path, monkeypatch, _use_real_commands):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    monkeypatch.setenv("MSP_MUTATE", "1")
    monkeypatch.setenv("MSP_SLEEP", "2")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3", timeout=1)})
    manager = _manager(config)
    client = manager._clients["muse"]

    with pytest.raises(AutoCoderTimeoutError, match="timed out after 1 seconds") as raised:
        client._run_llm_cli("first", is_noedit=True)

    assert any("repository invariant check also failed" in note for note in raised.value.__notes__)
    assert client.get_last_session_id() is None
    assert (repo / "tracked.txt").read_text() == "unchanged\n"


def test_muse_pre_host_option_failure_clears_previous_session(tmp_path, monkeypatch, _use_real_commands):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)
    client = manager._clients["muse"]

    assert manager._run_llm_cli("first") == "answer:first"
    assert client.get_last_session_id() == "opaque/provider/session"
    frames_before_failure = log.read_text()
    client.set_extra_args(["--unsupported-msp-option"])

    with pytest.raises(RuntimeError, match="not representable through MSP"):
        client._run_llm_cli("second")

    assert client.get_last_session_id() is None
    assert log.read_text() == frames_before_failure


@pytest.mark.parametrize("mode", ["MSP_PARTIAL", "MSP_STOP_READING"])
def test_muse_transport_io_obeys_deadline(tmp_path, monkeypatch, _use_real_commands, mode):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    monkeypatch.setenv(mode, "1")
    pid_file = tmp_path / "host.pid"
    monkeypatch.setenv("MSP_PID_FILE", str(pid_file))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3", timeout=1)})
    manager = _manager(config)
    client = manager._clients["muse"]
    prompt = "x" * (2 * 1024 * 1024) if mode == "MSP_STOP_READING" else "first"

    started = time.monotonic()
    with pytest.raises(AutoCoderTimeoutError):
        manager._run_llm_cli(prompt)
    assert time.monotonic() - started < 5
    assert client.get_last_session_id() is None
    host_pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(host_pid, 0)


@pytest.mark.parametrize("mode", ["MSP_QUOTA_RESPONSE", "MSP_QUOTA_TERMINAL"])
def test_muse_protocol_quota_failure_preserves_usage_classification(tmp_path, monkeypatch, _use_real_commands, mode):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    monkeypatch.setenv(mode, "1")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)
    client = manager._clients["muse"]

    with pytest.raises(AutoCoderUsageLimitError):
        manager._run_llm_cli("first")
    assert client.get_last_session_id() is None


def test_muse_resume_rejects_missing_model_before_turn(tmp_path, monkeypatch, _use_real_commands):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    monkeypatch.setenv("MSP_MISSING_MODEL", "1")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    client = _manager(config)._clients["muse"]

    with pytest.raises(RuntimeError, match="omitted or uses an incompatible model"):
        client.continue_session("opaque/provider/session", "second", is_noedit=True)
    methods = [json.loads(line)["frame"].get("method") for line in log.read_text().splitlines()]
    assert "session/resume" in methods
    assert "turn/start" not in methods
    assert client.get_last_session_id() is None


@pytest.mark.parametrize("mode", ["MSP_UNFINISHED_ITEM", "MSP_WRONG_EVENT_SESSION"])
def test_muse_rejects_unverified_final_answer_events(tmp_path, monkeypatch, _use_real_commands, mode):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    monkeypatch.setenv(mode, "1")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    client = _manager(config)._clients["muse"]

    with pytest.raises(RuntimeError):
        client.continue_session("opaque/provider/session", "second", is_noedit=True)
    assert client.get_last_session_id() is None


def test_muse_missing_resume_model_fails_without_fresh_fallback(tmp_path, monkeypatch, _use_real_commands):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    monkeypatch.setenv("MSP_MISSING_MODEL_RESUME", "1")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)
    client = manager._clients["muse"]

    with pytest.raises(RuntimeError, match="omitted or uses an incompatible model"):
        client.continue_session("opaque/provider/session", "second", is_noedit=True)
    methods = [json.loads(line)["frame"].get("method") for line in log.read_text().splitlines()]
    assert methods.count("session/resume") == 1
    assert "session/start" not in methods
    assert "turn/start" not in methods


@pytest.mark.parametrize("remove_original", [False, True])
def test_muse_foreign_or_removed_workspace_fails_without_fresh_fallback(tmp_path, monkeypatch, _use_real_commands, remove_original):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    old_repo = _repository(first_root)
    current_repo = _repository(second_root)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    old_tracked = old_repo / "tracked.txt"
    if remove_original:
        import shutil

        shutil.rmtree(old_repo)
    monkeypatch.chdir(current_repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    monkeypatch.setenv("MSP_STORED_WORKSPACE", str(old_repo))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)
    client = manager._clients["muse"]

    with pytest.raises(RuntimeError, match="incompatible workspace"):
        client.continue_session("opaque/provider/session", "second", is_noedit=True)
    assert client.get_last_session_id() is None
    failed_methods = [json.loads(line)["frame"].get("method") for line in log.read_text().splitlines()]
    assert failed_methods.count("session/resume") == 1
    assert "turn/start" not in failed_methods

    log.unlink()
    with pytest.raises(LocalContinuationError, match="no retained controller-owned binding"):
        manager.continue_session("opaque/provider/session", "second")
    assert manager._last_continue_session_resumed is False
    assert not log.exists()
    assert manager.get_last_session_id() is None
    if remove_original:
        assert not old_repo.exists()
    else:
        assert old_tracked.read_text() == "unchanged\n"


@pytest.mark.parametrize("invocation", ["fresh", "configured", "continuation"])
def test_muse_noedit_continuation_allows_shell_inspection(tmp_path, monkeypatch, _use_real_commands, invocation):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    shell_sentinel = tmp_path / "shell-effect"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3", options=["--no-edit"] if invocation == "configured" else [])})
    manager = _manager(config)
    client = manager._clients["muse"]

    session_id = None
    if invocation == "continuation":
        assert manager._run_llm_cli("first") == "answer:first"
        session_id = client.get_last_session_id()
        assert session_id == "opaque/provider/session"
    before = client._snapshot_at(repo)
    monkeypatch.setenv("MSP_ATTEMPT_EFFECTS", "1")
    monkeypatch.setenv("MSP_SHELL_SENTINEL", str(shell_sentinel))

    started = time.monotonic()
    if session_id is None:
        assert client._run_llm_cli("second", is_noedit=invocation == "fresh") == "answer:second"
    else:
        assert client.continue_session(session_id, "second", is_noedit=True) == "answer:second"
    assert time.monotonic() - started < 5
    assert client._snapshot_at(repo) == before
    assert (repo / "tracked.txt").read_text() == "unchanged\n"
    assert shell_sentinel.read_text() == "initial\nscripts/test.sh\ntracked.txt\nunchanged\n"
    turns = [json.loads(line) for line in log.read_text().splitlines() if json.loads(line)["frame"].get("method") == "turn/start"]
    assert len(turns) == (2 if invocation == "continuation" else 1)
    assert turns[-1]["argv"] == ["serve", "--disable-write"]
    prompt = turns[-1]["frame"]["params"]["input"][0]["text"]
    assert "Shell inspection is available" in prompt.replace("\n", " ")


@pytest.mark.parametrize("command", ["printf 'modified\\n' > tracked.txt", "git checkout -b forbidden && git checkout -"])
def test_muse_noedit_shell_mutation_rejects_result(tmp_path, monkeypatch, _use_real_commands, command):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    monkeypatch.setenv("MSP_SHELL_MUTATE", command)
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    client = _manager(config)._clients["muse"]
    before = client._snapshot_at(repo)

    with pytest.raises(RuntimeError, match="Git-state invariant"):
        client._run_llm_cli("inspect", is_noedit=True)
    assert client.get_last_session_id() is None
    assert client._snapshot_at(repo) == before


@pytest.mark.parametrize(
    ("mode", "error_pattern"),
    [
        ("MSP_WRONG_RESUME_ID", "different session identity"),
        ("MSP_PENDING_RESUME", "pending interactive requests"),
    ],
)
def test_muse_incompatible_resume_state_fails_without_fresh_fallback(tmp_path, monkeypatch, _use_real_commands, mode, error_pattern):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    monkeypatch.setenv(mode, "1")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)
    client = manager._clients["muse"]

    started = time.monotonic()
    with pytest.raises(RuntimeError, match=error_pattern):
        client.continue_session("opaque/provider/session", "second", is_noedit=True)
    assert time.monotonic() - started < 5
    assert client.get_last_session_id() is None
    failed_methods = [json.loads(line)["frame"].get("method") for line in log.read_text().splitlines()]
    assert failed_methods.count("session/resume") == 1
    assert "turn/start" not in failed_methods

    log.unlink()
    with pytest.raises(LocalContinuationError, match="no retained controller-owned binding"):
        manager.continue_session("opaque/provider/session", "second", is_noedit=True)
    assert manager._last_continue_session_resumed is False
    assert not log.exists()


@pytest.mark.parametrize("is_noedit", [False, True])
@pytest.mark.parametrize("reported_mode", [None, "denyUnmatched"])
def test_muse_pending_resume_reports_requested_and_effective_approval_policy(
    tmp_path,
    monkeypatch,
    _use_real_commands,
    is_noedit,
    reported_mode,
):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)
    client = manager._clients["muse"]

    assert manager._run_llm_cli("first") == "answer:first"
    session_id = manager.get_last_session_id()
    assert session_id == "opaque/provider/session"
    manager.authorize_retained_local_session_reuse(session_id)
    log.unlink()
    monkeypatch.setenv("MSP_PENDING_RESUME", "1")
    if reported_mode is not None:
        monkeypatch.setenv("MSP_REPORTED_APPROVAL_MODE", reported_mode)

    collector = get_trace_collector()
    case_number = 23960 + (2 if is_noedit else 0) + (1 if reported_mode else 0)
    with collector.start_execution("owner/repo", "issue", case_number, origin="worker") as execution:
        with pytest.raises(RuntimeError, match="pending interactive requests"):
            manager.continue_session(session_id, "second", is_noedit=is_noedit)
        execution.finish(Outcome.FAILED)

    frames = [json.loads(line)["frame"] for line in log.read_text().splitlines()]
    assert [frame.get("method") for frame in frames] == ["initialize", "initialized", "session/resume"]
    assert client.get_last_session_id() is None
    assert manager.get_last_session_id() is None
    events = [event for event in collector.get_snapshot(repository="owner/repo", item_type="issue", item_number=case_number).events if event.kind == EventKind.STAGE_RESULT.value and event.stage_id == "llm.muse-interactive-request"]
    assert len(events) == 1
    assert events[0].outcome == Outcome.BLOCKED.value
    assert events[0].facts == {
        "method": "session/resume",
        "requested_approval_policy": "allowAll",
        "effective_approval_policy": reported_mode or "unknown",
        "sessionId": session_id,
        "reason": "pending interactive requests",
    }


def test_muse_ignored_build_output_allows_handoff_and_retained_review(tmp_path, monkeypatch, _use_real_commands):
    repo = _repository(tmp_path)
    (repo / ".gitignore").write_text("build/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(_host(tmp_path)))
    log = tmp_path / "msp.jsonl"
    monkeypatch.setenv("MSP_LOG", str(log))
    monkeypatch.setenv("MSP_MUTATE", "1")
    monkeypatch.setenv("MSP_GENERATED_IGNORED", "1")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)
    try:
        assert manager._run_llm_cli("first") == "answer:first"
        session_id = manager.get_last_session_id()
        assert session_id == "opaque/provider/session"
        assert (repo / "tracked.txt").read_text() == "mutated\n"
        assert not (repo / "build").exists()
        retained = manager._retained_local_sessions[session_id]
        artifact = retained.binding.workspace / "build/generated.js"
        assert artifact.read_text() == "ignored build output\n"
        assert "build/generated.js" not in {state.relative_path for state in retained.binding.initial_files}
        manager.authorize_retained_local_session_reuse(session_id)
        monkeypatch.delenv("MSP_MUTATE")
        monkeypatch.delenv("MSP_GENERATED_IGNORED")
        assert manager.continue_session(session_id, "second review", is_noedit=True) == "answer:second"
        assert manager._last_continue_session_resumed is True
        assert artifact.read_text() == "ignored build output\n"
        frames = [json.loads(line)["frame"] for line in log.read_text().splitlines()]
        assert sum(frame.get("method") == "session/start" for frame in frames) == 1
        assert sum(frame.get("method") == "session/resume" for frame in frames) == 1
        assert sum(frame.get("method") == "turn/start" for frame in frames) == 2
    finally:
        for retained_id in tuple(manager._retained_local_sessions):
            manager.release_local_session(retained_id)


def test_muse_editable_large_context_does_not_allocate_recovery_snapshot(tmp_path, monkeypatch, _use_real_commands):
    import tracemalloc

    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    (repo / ".gitignore").write_text("runtime/\n")
    (repo / "runtime").mkdir()
    large_size = 48 * 1024 * 1024
    with (repo / "runtime" / "context.bin").open("wb") as stream:
        stream.truncate(large_size)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    monkeypatch.setenv("MSP_MUTATE", "1")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)

    tracemalloc.start()
    try:
        assert manager._run_llm_cli("edit the source") == "answer:first"
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        for retained_id in tuple(manager._retained_local_sessions):
            manager.release_local_session(retained_id)

    assert peak < 8 * 1024 * 1024, f"Unused recovery capture allocated {peak / 1024**2:.2f} MiB"
    assert (repo / "tracked.txt").read_text() == "mutated\n"
    assert (repo / "runtime" / "context.bin").stat().st_size == large_size
    assert manager.get_last_session_id() == "opaque/provider/session"


@pytest.mark.parametrize("cleanup", ["fresh", "close", "abandoned"])
def test_muse_retained_workspace_has_bounded_manager_lifetime(tmp_path, monkeypatch, _use_real_commands, cleanup):
    import gc
    import weakref

    repo = _repository(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(_host(tmp_path)))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)
    assert manager._run_llm_cli("first") == "answer:first"
    retained = manager._retained_local_sessions[manager.get_last_session_id()]
    parent = retained.binding.workspace.parent
    assert (parent / "source-snapshot").is_dir()
    assert retained.workspace_fd is not None
    if cleanup == "fresh":
        for _ in range(3):
            assert manager._run_llm_cli("new task") == "answer:first"
            assert not parent.exists()
            assert retained.workspace_fd is None
            assert len(manager._retained_local_sessions) == 1
            retained = manager._retained_local_sessions[manager.get_last_session_id()]
            parent = retained.binding.workspace.parent
        # Latest session still supports an explicit exact-root continuation.
        session_id = manager.get_last_session_id()
        manager.authorize_retained_local_session_reuse(session_id)
        assert manager.continue_session(session_id, "second review", is_noedit=True) == "answer:second"
        manager.close()
    elif cleanup == "close":
        manager.close()
        manager.close()
        assert manager._retained_local_sessions == {}
    else:
        reference = weakref.ref(manager)
        del manager
        gc.collect()
        assert reference() is None
    assert not parent.exists()
    assert retained.workspace_fd is None
    assert (repo / "tracked.txt").read_text() == "unchanged\n"


def test_muse_new_task_cannot_dispose_active_continuation(tmp_path, monkeypatch, _use_real_commands):
    repo = _repository(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(_host(tmp_path)))
    log = tmp_path / "msp.jsonl"
    monkeypatch.setenv("MSP_LOG", str(log))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)
    assert manager._run_llm_cli("first") == "answer:first"
    retained = manager._retained_local_sessions[manager.get_last_session_id()]
    original_log = log.read_bytes()
    retained.active_turn_id = "pending-turn"
    try:
        with pytest.raises(LocalContinuationError, match="turn is active"):
            manager._run_llm_cli("new task")
        assert retained.binding.workspace.exists()
        assert log.read_bytes() == original_log
        assert retained.workspace_fd is not None
    finally:
        retained.fail()
        manager.close()
    assert not retained.binding.workspace.parent.exists()


def test_muse_new_task_and_close_preserve_unsettled_writer_root(tmp_path, monkeypatch, _use_real_commands):
    from dataclasses import replace

    from src.auto_coder.exceptions import LocalWriterSettlementError
    from src.auto_coder.local_execution_boundary import EvidenceStatus

    repo = _repository(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(_host(tmp_path)))
    log = tmp_path / "msp.jsonl"
    monkeypatch.setenv("MSP_LOG", str(log))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)
    assert manager._run_llm_cli("first") == "answer:first"
    retained = manager._retained_local_sessions[manager.get_last_session_id()]
    settled = retained.predecessor
    original_log = log.read_bytes()
    retained.predecessor = replace(settled, writer_completion=EvidenceStatus.UNKNOWN)
    retained.binding.ownership.begin_execution()
    try:
        with pytest.raises(LocalWriterSettlementError, match="writer settlement is uncertain"):
            manager._run_llm_cli("new task")
        manager.close()
        assert retained.binding.workspace.exists()
        assert retained.workspace_fd is not None
        assert log.read_bytes() == original_log
    finally:
        retained.predecessor = settled
        retained.binding.ownership.release_execution()
        manager.close()
    assert not retained.binding.workspace.parent.exists()


def test_muse_fresh_session_collision_preserves_reserved_workspace(tmp_path, monkeypatch, _use_real_commands):
    repo = _repository(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(_host(tmp_path)))
    log = tmp_path / "msp.jsonl"
    monkeypatch.setenv("MSP_LOG", str(log))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)
    assert manager._run_llm_cli("first") == "answer:first"
    session_id = manager.get_last_session_id()
    retained = manager._retained_local_sessions[session_id]
    manager.authorize_retained_local_session_reuse(session_id)
    monkeypatch.setenv("MSP_MUTATE", "1")
    try:
        # This host deliberately returns the same provider ID for fresh tasks.
        with pytest.raises(LocalContinuationError, match="collides with a reserved"):
            manager._run_llm_cli("unrelated task")
        starts = [json.loads(line)["frame"] for line in log.read_text().splitlines() if json.loads(line)["frame"].get("method") == "session/start"]
        assert len(starts) == 2
        new_root = Path(starts[1]["params"]["workspaceRoot"])
        assert new_root != retained.binding.workspace
        assert not new_root.parent.exists()
        assert manager._retained_local_sessions[session_id] is retained
        assert retained.binding.workspace.exists()
        assert retained.workspace_fd is not None
        assert (repo / "tracked.txt").read_text() == "unchanged\n"
    finally:
        manager.close()
    assert not retained.binding.workspace.parent.exists()


@pytest.mark.parametrize("failure", ["initial-tests", "provider", "capacity"])
def test_muse_failed_invocation_releases_settled_workspace(tmp_path, monkeypatch, _use_real_commands, failure):
    import src.auto_coder.worktree_utils as worktree_utils

    repo = _repository(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(_host(tmp_path)))
    log = tmp_path / "msp.jsonl"
    monkeypatch.setenv("MSP_LOG", str(log))
    parents = []
    original = worktree_utils.tempfile.mkdtemp

    def record_parent(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get("prefix") == "auto_coder_llm_":
            parents.append(Path(result))
        return result

    monkeypatch.setattr(worktree_utils.tempfile, "mkdtemp", record_parent)
    if failure == "capacity":
        from types import SimpleNamespace

        monkeypatch.setattr(worktree_utils.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
        message = "insufficient disk space"
    elif failure == "initial-tests":

        def fail_initial_tests(*args):
            raise RuntimeError("initial tests could not start")

        monkeypatch.setattr("src.auto_coder.backend_manager.run_implementation_workspace_tests", fail_initial_tests)
        message = "initial tests could not start"
    else:
        monkeypatch.setenv("MSP_EXIT_NONZERO", "1")
        message = "nonzero status 7"
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)
    collector = get_trace_collector()
    case_number = {"initial-tests": 30001, "provider": 30002, "capacity": 30003}[failure]
    with collector.start_execution("owner/storage", "issue", case_number, origin="worker") as execution:
        with pytest.raises(RuntimeError, match=message):
            manager._run_llm_cli("task")
        execution.finish(Outcome.FAILED)
    assert parents
    assert all(not parent.exists() for parent in parents)
    assert manager._retained_local_sessions == {}
    assert (repo / "tracked.txt").read_text() == "unchanged\n"
    if failure in {"initial-tests", "capacity"}:
        assert not log.exists()
        assert not any(event.stage_id == "llm.local-execution" for event in collector.get_snapshot(repository="owner/storage", item_type="issue", item_number=case_number).events)
    if failure == "capacity":
        assert not any(event.stage_id == "local.workspace-tests" for event in collector.get_snapshot(repository="owner/storage", item_type="issue", item_number=case_number).events)
