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
    print("Muse Code 1.3.1")
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
        emit({"jsonrpc":"2.0","id":frame["id"],"result":{"serverInfo":{"name":"fixture","version":"1.3.1"},"schemaInfo":{"fingerprint":"sha256:b1e6676d624e116e2c1b150fec3192200d2cbca8ed79898e44f8921759c7872f"},"capabilities":{"sessionDurability":"durable"}}})
    elif method in ("session/start", "session/resume"):
        sid = "opaque/provider/session" if method == "session/start" else frame["params"]["sessionId"]
        session = {"sessionId":sid,"workspaceRoot":os.getcwd()}
        missing_model = os.environ.get("MSP_MISSING_MODEL") or (os.environ.get("MSP_MISSING_MODEL_RESUME") and method == "session/resume")
        if not missing_model:
            session["modelId"] = "muse-spark-1.3"
        emit({"jsonrpc":"2.0","id":frame["id"],"result":{"session":session,"pendingRequests":[]}})
        if os.environ.get("MSP_STOP_READING"):
            time.sleep(10)
    elif method == "turn/start":
        if os.environ.get("MSP_QUOTA_RESPONSE"):
            emit({"jsonrpc":"2.0","id":frame["id"],"error":{"code":429,"message":"quota exceeded"}})
            continue
        if os.environ.get("MSP_MUTATE"):
            Path("tracked.txt").write_text("mutated\n")
        if os.environ.get("MSP_SLEEP"):
            time.sleep(float(os.environ["MSP_SLEEP"]))
        turn = "turn-" + frame["params"]["commandId"]
        emit({"jsonrpc":"2.0","id":frame["id"],"result":{"turnId":turn,"disposition":"started"}})
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
    assert [entry["frame"]["params"]["sessionId"] for entry in resumes] == [session_id]
    assert len(turns) == 2
    assert turns[1]["argv"] == ["serve", "--disable-write", "--disable-shell", "--disable-approval"]
    assert (repo / "tracked.txt").read_text() == "unchanged\n"


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


def test_muse_missing_resume_model_falls_back_without_continuity(tmp_path, monkeypatch, _use_real_commands):
    repo = _repository(tmp_path)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    monkeypatch.setenv("MSP_MISSING_MODEL_RESUME", "1")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config)

    assert manager.continue_session("opaque/provider/session", "second") == "answer:second"
    assert manager._last_continue_session_resumed is False
    methods = [json.loads(line)["frame"].get("method") for line in log.read_text().splitlines()]
    assert methods.count("session/resume") == 1
    assert methods.count("session/start") == 1
    assert methods.count("turn/start") == 1
