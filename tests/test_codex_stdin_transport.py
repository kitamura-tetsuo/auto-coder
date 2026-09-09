"""Process-boundary tests for Codex task input transport."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from src.auto_coder.codex_client import CodexClient
from src.auto_coder.codex_mcp_client import CodexMCPClient
from src.auto_coder.llm_backend_config import BackendConfig

pytestmark = pytest.mark.usefixtures("_use_real_commands")


CODEX_FIXTURE = r"""#!/usr/bin/env python3
import hashlib
import json
import pathlib
import sys

if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.101.0")
    raise SystemExit(0)
if "exec" not in sys.argv or sys.argv[-1] != "-":
    raise SystemExit(64)
sys.stderr.write("fixture diagnostic before input\n")
sys.stderr.flush()
payload = sys.stdin.buffer.read()
print(json.dumps({
    "argv": sys.argv[1:],
    "bytes": len(payload),
    "digest": hashlib.sha256(payload).hexdigest(),
    "thread_id": "fixture-session",
}))
if "--output-last-message" in sys.argv:
    index = sys.argv.index("--output-last-message")
    pathlib.Path(sys.argv[index + 1]).write_text("fixture final message", encoding="utf-8")
"""


def _backend(options: list[str] | None = None) -> BackendConfig:
    values = options if options is not None else ["exec", "--json", "--model", "fixture"]
    return BackendConfig(
        name="codex",
        backend_type="codex",
        model="fixture",
        options=values,
        options_for_noedit=values,
    )


@pytest.fixture
def codex_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    executable = tmp_path / "codex"
    executable.write_text(CODEX_FIXTURE, encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    return executable


@pytest.mark.parametrize("size,is_noedit", [(256 * 1024 + 17, False), (2 * 1024 * 1024 + 31, True)])
def test_codex_client_delivers_large_prepared_prompt_once_through_stdin(
    codex_path: Path,
    size: int,
    is_noedit: bool,
) -> None:
    prompt = " \t-@'\";$()\r\n雪" + ("x" * size) + "\n "
    prepared = prompt.replace("@", "\\@").strip()
    config = type("Config", (), {"get_backend_config": lambda self, name: _backend()})()

    with patch("src.auto_coder.codex_client.get_llm_config", return_value=config):
        client = CodexClient(backend_name="codex")
        result = json.loads(client._run_llm_cli(prompt, is_noedit=is_noedit))

    assert result["bytes"] == len(prepared.encode("utf-8"))
    assert result["digest"] == hashlib.sha256(prepared.encode("utf-8")).hexdigest()
    assert result["argv"].count("exec") == 1
    assert result["argv"][-1] == "-"
    assert prompt not in result["argv"]
    assert client.get_last_session_id() == "fixture-session"


def test_codex_explicit_resume_keeps_id_separate_from_stdin(codex_path: Path) -> None:
    config = type("Config", (), {"get_backend_config": lambda self, name: _backend(["--json"])})()
    with patch("src.auto_coder.codex_client.get_llm_config", return_value=config):
        client = CodexClient(backend_name="codex")
        result = json.loads(client.continue_session("session-123", "  follow @up  "))

    assert result["argv"][:3] == ["exec", "resume", "session-123"]
    assert result["argv"][-1] == "-"
    assert result["digest"] == hashlib.sha256(b"follow \\@up").hexdigest()


def test_conflicting_configured_input_fails_before_launch(codex_path: Path, tmp_path: Path) -> None:
    marker = tmp_path / "launched"
    config = type("Config", (), {"get_backend_config": lambda self, name: _backend(["exec", "-"])})()
    with patch("src.auto_coder.codex_client.get_llm_config", return_value=config):
        client = CodexClient(backend_name="codex")
        with patch("src.auto_coder.codex_client.CommandExecutor.run_command") as execute:
            with pytest.raises(RuntimeError, match="conflicting task-input source"):
                client._run_llm_cli("payload")
    execute.assert_not_called()
    assert not marker.exists()


def test_mcp_unavailable_fallback_uses_separate_exec_stdin(
    codex_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = tmp_path / "mcp-server"
    mcp.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    mcp.chmod(0o755)
    monkeypatch.setenv("AUTOCODER_MCP_COMMAND", str(mcp))
    monkeypatch.setenv("AUTOCODER_MCP_HANDSHAKE_TIMEOUT", "0.01")
    config = type("Config", (), {"get_backend_config": lambda self, name: _backend([])})()

    with patch("src.auto_coder.codex_mcp_client.get_llm_config", return_value=config):
        client = CodexMCPClient()
        persistent_process = client.proc
        try:
            result = json.loads(client._run_llm_cli("  MCP @ fallback  "))
            assert result["argv"] == ["exec", "-"]
            assert result["digest"] == hashlib.sha256(b"MCP \\@ fallback").hexdigest()
            assert persistent_process is not None
            assert persistent_process.poll() is None
        finally:
            client.close()
