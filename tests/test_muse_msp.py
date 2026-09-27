"""Executable protocol regressions for Muse MSP session ownership."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from src.auto_coder.cli_helpers import build_backend_manager
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
import json, os, sys
from pathlib import Path
if sys.argv[1:] == ["--version"]:
    print("Muse Code 1.3.1")
    raise SystemExit
assert sys.argv[1] == "serve"
log = Path(os.environ["MSP_LOG"])
def emit(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    frame = json.loads(line)
    with log.open("a") as output:
        output.write(json.dumps({"argv": sys.argv[1:], "frame": frame}) + "\n")
    method = frame.get("method")
    if method == "initialize":
        emit({"jsonrpc":"2.0","id":frame["id"],"result":{"serverInfo":{"name":"fixture","version":"1.3.1"},"schemaInfo":{"fingerprint":"sha256:b1e6676d624e116e2c1b150fec3192200d2cbca8ed79898e44f8921759c7872f"},"capabilities":{"sessionDurability":"durable"}}})
    elif method in ("session/start", "session/resume"):
        sid = "opaque/provider/session" if method == "session/start" else frame["params"]["sessionId"]
        emit({"jsonrpc":"2.0","id":frame["id"],"result":{"session":{"sessionId":sid,"workspaceRoot":os.getcwd(),"modelId":"muse-spark-1.3"},"pendingRequests":[]}})
    elif method == "turn/start":
        turn = "turn-" + frame["params"]["commandId"]
        emit({"jsonrpc":"2.0","id":frame["id"],"result":{"turnId":turn,"disposition":"started"}})
        emit({"jsonrpc":"2.0","method":"item/completed","params":{"sessionId":frame["params"]["sessionId"],"item":{"itemId":"answer","kind":"message","revision":1,"status":"completed","turnId":turn,"role":"assistant","text":"answer:" + ("second" if "second" in frame["params"]["input"][0]["text"] else "first")}}})  # noqa: E501
        emit({"jsonrpc":"2.0","method":"turn/completed","params":{"sessionId":frame["params"]["sessionId"],"turnId":turn,"terminal":"completed"}})
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
