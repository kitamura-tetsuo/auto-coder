"""Executable protocol regressions for Muse MSP session ownership."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from src.auto_coder.cli_helpers import build_backend_manager
from src.auto_coder.exceptions import AutoCoderTimeoutError, AutoCoderUsageLimitError
from src.auto_coder.execution_trace import EventKind, Outcome, get_trace_collector
from src.auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
from src.auto_coder.local_session_continuation import LocalContinuationError
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
        pending = [{"kind":"approval","approvalId":"pending-1","viewCursor":"cursor-1"}] if method == "session/resume" and os.environ.get("MSP_PENDING_RESUME") else []
        emit({"jsonrpc":"2.0","id":frame["id"],"result":{"session":session,"pendingRequests":pending}})
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
    elif method == "turn/start":
        if os.environ.get("MSP_CHILD_WRITER"):
            subprocess.Popen([sys.executable, "-c", "import os,time; from pathlib import Path; time.sleep(1); Path(os.environ['MSP_CHILD_SENTINEL']).write_text('late write')"])
        if os.environ.get("MSP_ATTEMPT_EFFECTS"):
            assert effective_mode == "onRequest", "Read-only shell inspection must not use denyUnmatched"
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
        item = {"itemId":"answer","kind":item_kind,"revision":1,"status":item_status,"turnId":turn,"text":"answer:" + ("second" if "second" in frame["params"]["input"][0]["text"] else "first")}
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


def test_muse_msp_explicit_disable_approval_uses_only_wire_mode(tmp_path, monkeypatch, _use_real_commands):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3", options=["--disable-approval"])})

    assert _manager(config)._run_llm_cli("first") == "answer:first"
    start = next(json.loads(line) for line in log.read_text().splitlines() if json.loads(line)["frame"].get("method") == "session/start")
    assert start["argv"] == ["serve"]
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
    assert start["frame"]["params"]["approvalMode"] == "onRequest"
    assert any(entry["frame"].get("method") == "turn/start" for entry in entries)


@pytest.mark.parametrize("approval_mode", ["omit", "allowAll", "denyUnmatched"])
def test_muse_msp_fresh_noedit_rejects_unconfirmed_approval_before_turn(tmp_path, monkeypatch, _use_real_commands, approval_mode):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    monkeypatch.setenv("MSP_APPROVAL_MODE", approval_mode)
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})

    with pytest.raises(RuntimeError, match="fresh session did not confirm approval mode onRequest"):
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
    assert starts[0]["params"]["approvalMode"] == ("denyUnmatched" if extra_option == "--disable-approval" else "onRequest")
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
    assert start["frame"]["params"]["approvalMode"] == "onRequest"
    assert "--model" not in start["argv"]
    assert turn["frame"]["params"]["reasoningEffort"] == "high"
    assert isinstance(turn["frame"]["params"]["input"], list)
    for command in (start, turn):
        _assert_uuid7(command["frame"]["params"]["commandId"])
    assert start["frame"]["params"]["commandId"] != turn["frame"]["params"]["commandId"]


@pytest.mark.parametrize("apply_outcome", ["completed", "noop"])
def test_muse_msp_resume_reestablishes_approval_denial(tmp_path, monkeypatch, _use_real_commands, apply_outcome):
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
    assert approval["frame"]["params"]["mode"] == "onRequest"
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
def test_muse_msp_editable_resume_establishes_allow_all(tmp_path, monkeypatch, _use_real_commands, stored_mode):
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
    assert manager.continue_session(session_id, "second") == "answer:second"

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
        "requested_approval_policy": "onRequest" if is_noedit else "allowAll",
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
