import pathlib
import sys

from src.auto_coder.codex_mcp_client import CodexMCPClient


def test_codex_mcp_jsonrpc_handshake_and_tool_call_e2e(tmp_path, monkeypatch):
    # Use Python as codex CLI override for availability check
    monkeypatch.setenv("AUTOCODER_CODEX_CLI", sys.executable)

    # Point MCP command to the in-repo echo server
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    server_path = repo_root / "tests" / "support" / "mcp_echo_server.py"
    monkeypatch.setenv("AUTOCODER_MCP_COMMAND", f"{sys.executable} {server_path}")

    client = CodexMCPClient()
    try:
        out = client._run_llm_cli("hello world")
        # MCP単発API優先（prompts/call -> inference/create -> tools/run/execute/workspace-write -> echo）
        assert "PROMPT: hello world" in out
    finally:
        client.close()
