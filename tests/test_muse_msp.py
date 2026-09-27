"""Executable protocol regressions for Muse MSP session ownership."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from src.auto_coder.cli_helpers import build_backend_manager
from src.auto_coder.exceptions import AutoCoderTimeoutError, AutoCoderUsageLimitError
from src.auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration


def _repository(path: Path) -> Path:
    repo = path / "repo"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("unchanged\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    return repo


def _host(path: Path) -> Path:
    host = path / "muse"
    host.write_text(
        r"""#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
if sys.argv[1:] == ["--version"]:
    print("Muse Code 1.3.0")
    raise SystemExit
assert sys.argv[1] == "serve"
if os.environ.get("MSP_PID_FILE"):
    Path(os.environ["MSP_PID_FILE"]).write_text(str(os.getpid()))
log = Path(os.environ["MSP_LOG"])
def emit(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    frame = json.loads(line)
    with log.open("a") as output:
        output.write(json.dumps({"argv": sys.argv[1:], "frame": frame}) + "\n")
    method = frame.get("method")
    if method == "initialize":
        if os.environ.get("MSP_PARTIAL"):
            sys.stdout.write("{")
            sys.stdout.flush()
            time.sleep(10)
        schema = {"version":1,"fingerprint":"sha256:b1e6676d624e116e2c1b150fec3192200d2cbca8ed79898e44f8921759c7872f"}
        result = {"serverInfo":{"name":"fixture","version":os.environ.get("MSP_SERVER_VERSION", "1.3.0")},"schema":schema,"capabilities":{"sessionDurability":"durable"}}
        if os.environ.get("MSP_SCHEMA_ALIAS"):
            result["schemaInfo"] = result.pop("schema")
        if os.environ.get("MSP_SCHEMA_VERSION"):
            schema["version"] = os.environ["MSP_SCHEMA_VERSION"]
        emit({"jsonrpc":"2.0","id":frame["id"],"result":result})
    elif method in ("session/start", "session/resume"):
        sid = "opaque/provider/session" if method == "session/start" else frame["params"]["sessionId"]
        if method == "session/resume" and os.environ.get("MSP_WRONG_RESUME_ID"):
            sid = "different/provider/session"
        workspace = os.environ.get("MSP_STORED_WORKSPACE", os.getcwd()) if method == "session/resume" else os.getcwd()
        session = {"sessionId":sid,"workspaceRoot":workspace}
        missing_model = os.environ.get("MSP_MISSING_MODEL") or (os.environ.get("MSP_MISSING_MODEL_RESUME") and method == "session/resume")
        if not missing_model:
            session["modelId"] = "muse-spark-1.3"
        requested_denial = method == "session/start" and frame["params"].get("approvalMode") == "denyUnmatched"
        approval_mode = os.environ.get("MSP_APPROVAL_MODE")
        if approval_mode != "omit" and (requested_denial or (method == "session/resume" and os.environ.get("MSP_RESUME_DENIED"))):
            session["approvalMode"] = {"mode":approval_mode or "denyUnmatched"}
        pending = [{"kind":"approval","approvalId":"pending-1","viewCursor":"cursor-1"}] if method == "session/resume" and os.environ.get("MSP_PENDING_RESUME") else []
        emit({"jsonrpc":"2.0","id":frame["id"],"result":{"session":session,"pendingRequests":pending}})
        if os.environ.get("MSP_STOP_READING"):
            time.sleep(10)
    elif method == "session/setApprovalMode":
        ack_case = os.environ.get("MSP_APPROVAL_ACK_CASE", "completed")
        ack = {"commandId":frame["params"]["commandId"],"status":"accepted","applyOutcome":ack_case if ack_case in ("completed", "noop") else "completed","effectiveMode":{"mode":"denyUnmatched"}}
        if ack_case == "mismatched-command":
            ack["commandId"] = "01900000-0000-7000-8000-000000000000"
        elif ack_case == "rejected":
            ack["status"] = "rejected"
        elif ack_case == "pending":
            ack["applyOutcome"] = "pending"
        elif ack_case == "missing-mode":
            ack.pop("effectiveMode")
        elif ack_case == "permissive-mode":
            ack["effectiveMode"] = {"mode":"allowAll"}
        emit({"jsonrpc":"2.0","id":frame["id"],"result":ack})
    elif method == "turn/start":
        if os.environ.get("MSP_ATTEMPT_EFFECTS"):
            required = {"--disable-write", "--disable-shell"}
            if not required.issubset(set(sys.argv[1:])):
                Path("tracked.txt").write_text("model write effect\n")
                Path(os.environ["MSP_SHELL_SENTINEL"]).write_text("shell effect\n")
        if os.environ.get("MSP_QUOTA_RESPONSE"):
            emit({"jsonrpc":"2.0","id":frame["id"],"error":{"code":429,"message":"quota exceeded"}})
            continue
        if os.environ.get("MSP_MUTATE"):
            Path("tracked.txt").write_text("mutated\n")
        if os.environ.get("MSP_SLEEP"):
            time.sleep(float(os.environ["MSP_SLEEP"]))
        turn = "turn-" + frame["params"]["commandId"]
        ack = {"commandId":frame["params"]["commandId"],"status":"accepted","turnId":turn,"startedNewTurn":True,"disposition":"started"}
        if os.environ.get("MSP_BAD_TURN_ACK"):
            ack[os.environ["MSP_BAD_TURN_ACK"]] = None
        emit({"jsonrpc":"2.0","id":frame["id"],"result":ack})
        event_session = "wrong-session" if os.environ.get("MSP_WRONG_EVENT_SESSION") else frame["params"]["sessionId"]
        item_method = "item/updated" if os.environ.get("MSP_UNFINISHED_ITEM") else "item/completed"
        item_status = "inProgress" if os.environ.get("MSP_UNFINISHED_ITEM") else "completed"
        emit({"jsonrpc":"2.0","method":item_method,"params":{"sessionId":event_session,"item":{"itemId":"answer","kind":"message","revision":1,"status":item_status,"turnId":turn,"role":"assistant","text":"answer:" + ("second" if "second" in frame["params"]["input"][0]["text"] else "first")}}})  # noqa: E501
        terminal = "failed" if os.environ.get("MSP_QUOTA_TERMINAL") else "completed"
        terminal_params = {"sessionId":event_session,"turnId":turn,"terminal":terminal}
        if terminal == "failed":
            terminal_params["reason"] = "rate limit exceeded"
        emit({"jsonrpc":"2.0","method":"turn/completed","params":terminal_params})
"""
    )
    host.chmod(0o700)
    return host


def _manager(config: LLMBackendConfiguration):
    with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.muse_client.get_llm_config", return_value=config):
        return build_backend_manager(["muse"], "muse", {"muse": "muse-spark-1.3"})


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
    assert manager.continue_session(session_id, "second", is_noedit=True) == "answer:second"
    assert manager._last_continue_session_resumed is True

    frames = [json.loads(line) for line in log.read_text().splitlines()]
    starts = [entry for entry in frames if entry["frame"].get("method") == "session/start"]
    resumes = [entry for entry in frames if entry["frame"].get("method") == "session/resume"]
    turns = [entry for entry in frames if entry["frame"].get("method") == "turn/start"]
    assert len(starts) == 1
    assert "approvalMode" not in starts[0]["frame"]["params"]
    assert [entry["frame"]["params"]["sessionId"] for entry in resumes] == [session_id]
    assert len(turns) == 2
    assert turns[1]["argv"] == ["serve", "--disable-write", "--disable-shell"]
    assert turns[1]["frame"]["params"]["input"][0]["type"] == "text"
    assert (repo / "tracked.txt").read_text() == "unchanged\n"


def test_muse_msp_explicit_disable_approval_uses_only_wire_mode(tmp_path, monkeypatch, _use_real_commands):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3", options=["--disable-approval"])})

    assert _manager(config)._clients["muse"]._run_llm_cli("first") == "answer:first"
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

    assert _manager(config)._clients["muse"]._run_llm_cli("first") == "answer:first"
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    start = next(entry for entry in entries if entry["frame"].get("method") == "session/start")
    assert start["argv"] == ["serve", "--disable-write", "--disable-shell"]
    assert "--disable-approval" not in start["argv"]
    assert start["frame"]["params"]["approvalMode"] == "denyUnmatched"
    assert any(entry["frame"].get("method") == "turn/start" for entry in entries)


@pytest.mark.parametrize("approval_mode", ["omit", "allowAll"])
def test_muse_msp_fresh_noedit_rejects_unconfirmed_approval_before_turn(tmp_path, monkeypatch, _use_real_commands, approval_mode):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    monkeypatch.setenv("MSP_APPROVAL_MODE", approval_mode)
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})

    with pytest.raises(RuntimeError, match="fresh session did not confirm approval denial"):
        _manager(config)._clients["muse"]._run_llm_cli("first", is_noedit=True)
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert any(entry["frame"].get("method") == "session/start" for entry in entries)
    assert not any(entry["frame"].get("method") == "turn/start" for entry in entries)


def _assert_uuid7(value: str) -> None:
    import uuid

    parsed = uuid.UUID(value)
    assert parsed.version == 7
    assert parsed.variant == uuid.RFC_4122


def test_muse_msp_noedit_maps_host_and_wire_options(tmp_path, monkeypatch, _use_real_commands):
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
                options_for_noedit=["--model=muse-spark-1.3", "--reasoning-effort", "high", "--no-edit"],
            )
        }
    )

    assert _manager(config)._clients["muse"]._run_llm_cli("complete prompt", is_noedit=True) == "answer:first"
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    start = next(entry for entry in entries if entry["frame"].get("method") == "session/start")
    turn = next(entry for entry in entries if entry["frame"].get("method") == "turn/start")
    assert start["argv"] == ["serve", "--disable-write", "--disable-shell"]
    assert start["frame"]["params"]["approvalMode"] == "denyUnmatched"
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

    client = _manager(config)._clients["muse"]
    assert client.continue_session("opaque/provider/session", "second", is_noedit=True) == "answer:second"
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    resume = next(entry for entry in entries if entry["frame"].get("method") == "session/resume")
    approval = next(entry for entry in entries if entry["frame"].get("method") == "session/setApprovalMode")
    turn = next(entry for entry in entries if entry["frame"].get("method") == "turn/start")
    assert resume["frame"]["params"].keys() == {"commandId", "sessionId"}
    assert approval["frame"]["params"]["mode"] == "denyUnmatched"
    assert approval["frame"]["params"]["sessionId"] == "opaque/provider/session"
    command_ids = [entry["frame"]["params"]["commandId"] for entry in (resume, approval, turn)]
    assert len(command_ids) == len(set(command_ids))
    for command_id in command_ids:
        _assert_uuid7(command_id)


@pytest.mark.parametrize(
    "ack_case",
    ["mismatched-command", "rejected", "pending", "missing-mode", "permissive-mode"],
)
def test_muse_msp_resume_rejects_unverified_approval_change_before_turn(tmp_path, monkeypatch, _use_real_commands, ack_case):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    monkeypatch.setenv("MSP_APPROVAL_ACK_CASE", ack_case)
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})

    with pytest.raises(RuntimeError):
        _manager(config).continue_session("opaque/provider/session", "second", is_noedit=True)
    methods = [json.loads(line)["frame"].get("method") for line in log.read_text().splitlines()]
    assert methods.count("session/resume") == 1
    assert methods.count("session/setApprovalMode") == 1
    assert "session/start" not in methods
    assert "turn/start" not in methods


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
        _manager(config)._clients["muse"]._run_llm_cli("prompt")
    assert not log.exists()


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({"MSP_SERVER_VERSION": "1.3.1"}, "host version"),
        ({"MSP_SCHEMA_ALIAS": "1"}, "omitted schema"),
        ({"MSP_SCHEMA_VERSION": "1"}, "schema is incompatible"),
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
        _manager(config)._clients["muse"]._run_llm_cli("prompt")
    methods = [json.loads(line)["frame"].get("method") for line in log.read_text().splitlines()]
    assert methods == ["initialize"]


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
        _manager(config)._clients["muse"]._run_llm_cli("prompt")


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

    assert client._run_llm_cli("first") == "answer:first"
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
    client = _manager(config)._clients["muse"]
    prompt = "x" * (2 * 1024 * 1024) if mode == "MSP_STOP_READING" else "first"

    started = time.monotonic()
    with pytest.raises(AutoCoderTimeoutError):
        client._run_llm_cli(prompt)
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
    client = _manager(config)._clients["muse"]

    with pytest.raises(AutoCoderUsageLimitError):
        client._run_llm_cli("first")
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
        client.continue_session("opaque/provider/session", "second")
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
        client.continue_session("opaque/provider/session", "second")
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

    with pytest.raises(RuntimeError, match="omitted or uses an incompatible model"):
        manager.continue_session("opaque/provider/session", "second")
    assert manager._last_continue_session_resumed is False
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
        client.continue_session("opaque/provider/session", "second")
    assert client.get_last_session_id() is None
    failed_methods = [json.loads(line)["frame"].get("method") for line in log.read_text().splitlines()]
    assert failed_methods.count("session/resume") == 1
    assert "turn/start" not in failed_methods

    log.unlink()
    with pytest.raises(RuntimeError, match="incompatible workspace"):
        manager.continue_session("opaque/provider/session", "second")
    assert manager._last_continue_session_resumed is False
    methods = [json.loads(line)["frame"].get("method") for line in log.read_text().splitlines()]
    assert methods.count("session/resume") == 1
    assert "session/start" not in methods
    assert "turn/start" not in methods
    assert manager.get_last_session_id() is None
    if remove_original:
        assert not old_repo.exists()
    else:
        assert old_tracked.read_text() == "unchanged\n"


def test_muse_noedit_continuation_prevents_write_and_shell_effects(tmp_path, monkeypatch, _use_real_commands):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    shell_sentinel = tmp_path / "shell-effect"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)
    client = manager._clients["muse"]

    assert client._run_llm_cli("first") == "answer:first"
    session_id = client.get_last_session_id()
    assert session_id == "opaque/provider/session"
    before = client._snapshot_at(repo)
    monkeypatch.setenv("MSP_ATTEMPT_EFFECTS", "1")
    monkeypatch.setenv("MSP_SHELL_SENTINEL", str(shell_sentinel))

    started = time.monotonic()
    assert client.continue_session(session_id, "second", is_noedit=True) == "answer:second"
    assert time.monotonic() - started < 5
    assert client._snapshot_at(repo) == before
    assert (repo / "tracked.txt").read_text() == "unchanged\n"
    assert not shell_sentinel.exists()
    turns = [json.loads(line) for line in log.read_text().splitlines() if json.loads(line)["frame"].get("method") == "turn/start"]
    assert len(turns) == 2
    assert turns[1]["argv"] == ["serve", "--disable-write", "--disable-shell"]


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
        client.continue_session("opaque/provider/session", "second")
    assert time.monotonic() - started < 5
    assert client.get_last_session_id() is None
    failed_methods = [json.loads(line)["frame"].get("method") for line in log.read_text().splitlines()]
    assert failed_methods.count("session/resume") == 1
    assert "turn/start" not in failed_methods

    log.unlink()
    with pytest.raises(RuntimeError, match=error_pattern):
        manager.continue_session("opaque/provider/session", "second", is_noedit=True)
    assert manager._last_continue_session_resumed is False
    methods = [json.loads(line)["frame"].get("method") for line in log.read_text().splitlines()]
    assert methods.count("session/resume") == 1
    assert "session/start" not in methods
    assert "turn/start" not in methods
