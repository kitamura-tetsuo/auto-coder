"""Real-CLI production regression coverage for OpenCode no-edit enforcement (Issue #2125).

Executable-double tests in `tests/test_opencode_noedit_backend.py` establish
option transport, cleanup, and interleaving. Only the actual released
`opencode` CLI (pinned to v1.18.31, matching the Issue's baseline) against a
controlled local provider can establish real tool exposure and configuration
merge precedence; a fake CLI that merely honors an invented `--no-edit` flag
would not be evidence of OpenCode safety.

These tests auto-install the pinned CLI via npm when it is not already on
PATH, and skip (never fail) when npm/network are unavailable, so they run
for real wherever infrastructure allows (this sandbox, standard GitHub-hosted
CI runners) and degrade gracefully elsewhere.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest

from src.auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
from src.auto_coder.opencode_client import OpenCodeClient
from tests.test_opencode_backend import _git, _repository

_PINNED_OPENCODE_VERSION = "1.18.31"

pytestmark = [pytest.mark.opencode_live, pytest.mark.timeout(120)]


def _find_or_install_opencode() -> Optional[str]:
    existing = shutil.which("opencode")
    if existing:
        return existing
    npm = shutil.which("npm")
    if not npm:
        return None
    try:
        subprocess.run(
            [npm, "install", "-g", f"opencode-ai@{_PINNED_OPENCODE_VERSION}"],
            capture_output=True,
            timeout=180,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return shutil.which("opencode")


@pytest.fixture(scope="session")
def opencode_cli() -> str:
    path = _find_or_install_opencode()
    if not path:
        pytest.skip("opencode CLI is not installed and could not be installed (no npm/network available); skipping real-CLI no-edit regression coverage")
    return path


Turn = Callable[[Dict[str, Any]], Tuple[str, Any]]


class ScriptedProvider:
    """A minimal local OpenAI-compatible provider driving OpenCode's real agent loop.

    Requests that carry no `tools` (OpenCode's internal session-title call) are
    answered generically and never consume a scripted turn. Each subsequent
    tool-bearing request consumes the next scripted `Turn`, which returns
    either `("text", answer)` for a completed final message or
    `("tool_calls", [{"name": ..., "arguments": {...}}, ...])` to have the
    model request one or more tool calls in a single step.
    """

    def __init__(self, turns: List[Turn]) -> None:
        self._turns = turns
        self._index = 0
        self._lock = threading.Lock()
        self.requests: List[Dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                if not body.get("tools"):
                    self._write("text", "untitled")
                    return
                with outer._lock:
                    outer.requests.append(body)
                    index = min(outer._index, len(outer._turns) - 1)
                    outer._index += 1
                kind, payload = outer._turns[index](body)
                self._write(kind, payload)

            def do_GET(self) -> None:  # pragma: no cover - not exercised by these tests
                self.send_response(404)
                self.end_headers()

            def _write(self, kind: str, payload: Any) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if kind == "text":
                    delta: Dict[str, Any] = {"role": "assistant", "content": payload}
                    finish = "stop"
                elif kind == "tool_calls":
                    delta = {
                        "role": "assistant",
                        "tool_calls": [{"index": i, "id": f"call_{i}", "type": "function", "function": {"name": call["name"], "arguments": json.dumps(call.get("arguments", {}))}} for i, call in enumerate(payload)],
                    }
                    finish = "tool_calls"
                elif kind == "error":
                    self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                else:  # pragma: no cover - programming error in a test
                    raise ValueError(f"unknown scripted turn kind {kind!r}")
                chunk = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 0, "model": "fake", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                done = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 0, "model": "fake", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(f"data: {json.dumps(done)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def scripted_provider():
    servers: List[ScriptedProvider] = []

    def _make(turns: List[Turn]) -> ScriptedProvider:
        server = ScriptedProvider(turns)
        servers.append(server)
        return server

    yield _make
    for server in servers:
        server.stop()


def _write_home_config(home: Path, *, provider_name: str, model_name: str, base_url: str, extra: Optional[Dict[str, Any]] = None) -> None:
    config_dir = home / ".config" / "opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    config: Dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            provider_name: {
                "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": base_url},
                "models": {model_name: {"name": model_name}},
            }
        },
    }
    if extra:
        for key, value in extra.items():
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                config[key].update(value)
            else:
                config[key] = value
    (config_dir / "opencode.json").write_text(json.dumps(config))


def _client(cwd: Path, home: Path, cli: str, monkeypatch: pytest.MonkeyPatch, *, backend_name: str = "opencode", model: str = "fakeprov/fake-model") -> OpenCodeClient:
    config = LLMBackendConfiguration(backends={backend_name: BackendConfig(name=backend_name, backend_type="opencode", model=model, timeout=60)})
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", cli)
    monkeypatch.chdir(cwd)
    with patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        return OpenCodeClient(backend_name=backend_name)


def _snapshot_tree(repo: Path) -> Dict[str, Any]:
    """A cheap, sufficient proxy for REQ-002's full-state comparison."""
    status = subprocess.run(["git", "status", "--porcelain=v2", "--untracked-files=all", "--ignored=matching"], cwd=repo, capture_output=True, text=True, check=True).stdout
    head = _git(repo, "rev-parse", "HEAD")
    refs = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    unstaged = subprocess.run(["git", "diff", "--binary"], cwd=repo, capture_output=True, check=True).stdout
    staged = subprocess.run(["git", "diff", "--cached", "--binary"], cwd=repo, capture_output=True, check=True).stdout
    files = {}
    for path in ("tracked.txt", "unstaged.txt", "untracked.txt", "ignored_file.txt", "link.txt"):
        candidate = repo / path
        if candidate.is_symlink():
            files[path] = ("symlink", os.readlink(candidate))
        elif candidate.is_file():
            files[path] = ("file", candidate.read_bytes())
    return {"status": status, "head": head, "refs": refs, "unstaged": unstaged, "staged": staged, "files": files}


def _seed_ac001_content(target: Path) -> None:
    """Populate `target` (a checkout or a linked worktree) with REQ-002's evidence shapes."""
    (target / "unstaged.txt").write_text("before\n")
    _git(target, "add", "unstaged.txt")
    _git(target, "commit", "-m", "seed unstaged.txt")
    (target / "unstaged.txt").write_text("SENTINEL_EVIDENCE_98765\n")  # unstaged tracked change

    (target / "tracked.txt").write_text("staged before\n")
    _git(target, "add", "tracked.txt")
    _git(target, "commit", "-m", "seed tracked.txt")
    (target / "tracked.txt").write_text("staged after\n")
    _git(target, "add", "tracked.txt")  # staged tracked change

    (target / "untracked.txt").write_text("untracked content\n")
    (target / ".gitignore").write_text("ignored_file.txt\n")
    _git(target, "add", ".gitignore")
    _git(target, "commit", "-m", "add gitignore")
    (target / "ignored_file.txt").write_text("ignored content\n")

    (target / "link.txt").symlink_to("unstaged.txt")


def _ac001_repository(tmp_path: Path) -> Path:
    repo = _repository(tmp_path)
    _seed_ac001_content(repo)
    return repo


# ---------------------------------------------------------------------------
# AC-001: useful read-only execution through the real client
# ---------------------------------------------------------------------------


def _read_tool_turn(target_path: Path) -> Turn:
    def _turn(_request: Dict[str, Any]) -> Tuple[str, Any]:
        return "tool_calls", [{"name": "read", "arguments": {"filePath": str(target_path)}}]

    return _turn


def _echo_tool_result_turn() -> Turn:
    def _turn(request: Dict[str, Any]) -> Tuple[str, Any]:
        tool_message = next(m for m in request["messages"] if m.get("role") == "tool")
        content = tool_message.get("content")
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return "text", f"ANSWER_SENTINEL::{content}"

    return _turn


def test_ac001_real_readonly_execution_uses_evidence_and_preserves_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opencode_cli: str, scripted_provider, _use_real_commands) -> None:
    repo = _ac001_repository(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    provider = scripted_provider([_read_tool_turn(repo / "unstaged.txt"), _echo_tool_result_turn()])
    _write_home_config(home, provider_name="fakeprov", model_name="fake-model", base_url=provider.base_url)

    before = _snapshot_tree(repo)
    client = _client(repo, home, opencode_cli, monkeypatch)
    answer = client._run_llm_cli("what does unstaged.txt say?", is_noedit=True)
    after = _snapshot_tree(repo)

    assert "SENTINEL_EVIDENCE_98765" in answer
    assert before == after


def test_ac001_linked_worktree_preserves_primary_checkout_and_shared_refs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opencode_cli: str, scripted_provider, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    worktree_dir = tmp_path / "linked-worktree"
    subprocess.run(["git", "worktree", "add", "--detach", str(worktree_dir), "HEAD"], cwd=repo, check=True, capture_output=True)
    # Uncommitted/untracked/ignored content lives per-worktree; seed it there directly.
    _seed_ac001_content(worktree_dir)

    home = tmp_path / "home"
    home.mkdir()
    provider = scripted_provider([_read_tool_turn(worktree_dir / "unstaged.txt"), _echo_tool_result_turn()])
    _write_home_config(home, provider_name="fakeprov", model_name="fake-model", base_url=provider.base_url)

    primary_before = _git(repo, "rev-parse", "HEAD")
    worktree_before = _snapshot_tree(worktree_dir)
    client = _client(worktree_dir, home, opencode_cli, monkeypatch)
    answer = client._run_llm_cli("what does unstaged.txt say?", is_noedit=True)
    worktree_after = _snapshot_tree(worktree_dir)

    assert "SENTINEL_EVIDENCE_98765" in answer
    assert worktree_before == worktree_after
    assert _git(repo, "rev-parse", "HEAD") == primary_before


def test_ac001_invocation_failure_preserves_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opencode_cli: str, scripted_provider, _use_real_commands) -> None:
    repo = _ac001_repository(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    def _error_turn(_request: Dict[str, Any]) -> Tuple[str, Any]:
        return "error", {"error": {"message": "synthetic provider failure", "type": "server_error"}}

    provider = scripted_provider([_error_turn])
    _write_home_config(home, provider_name="fakeprov", model_name="fake-model", base_url=provider.base_url)

    before = _snapshot_tree(repo)
    client = _client(repo, home, opencode_cli, monkeypatch)
    with pytest.raises(RuntimeError):
        client._run_llm_cli("what does unstaged.txt say?", is_noedit=True)
    after = _snapshot_tree(repo)
    assert before == after


# ---------------------------------------------------------------------------
# AC-002: permission denial occurs before the effect
# ---------------------------------------------------------------------------


def test_ac002_forbidden_tool_calls_denied_before_effect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opencode_cli: str, scripted_provider, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    mcp_called_marker = capture_dir / "mcp_called.txt"
    fake_mcp_server = tmp_path / "fake_mcp_server.py"
    fake_mcp_server.write_text(
        "import sys, json\n"
        "for line in sys.stdin:\n"
        "    line = line.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    req = json.loads(line)\n"
        "    method, rid = req.get('method'), req.get('id')\n"
        "    if method == 'initialize':\n"
        "        resp = {'jsonrpc': '2.0', 'id': rid, 'result': {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fakemcp', 'version': '0.0.1'}}}\n"
        "    elif method == 'tools/list':\n"
        "        resp = {'jsonrpc': '2.0', 'id': rid, 'result': {'tools': [{'name': 'exfiltrate', 'description': 'send data out', 'inputSchema': {'type': 'object', 'properties': {}}}]}}\n"
        "    elif method == 'tools/call':\n"
        f"        open({str(mcp_called_marker)!r}, 'w').write(line)\n"
        "        resp = {'jsonrpc': '2.0', 'id': rid, 'result': {'content': [{'type': 'text', 'text': 'exfiltrated'}]}}\n"
        "    elif rid is not None:\n"
        "        resp = {'jsonrpc': '2.0', 'id': rid, 'result': {}}\n"
        "    else:\n"
        "        continue\n"
        "    sys.stdout.write(json.dumps(resp) + chr(10))\n"
        "    sys.stdout.flush()\n"
    )
    target_file = repo / "should_not_exist.txt"

    def _all_forbidden_turn(_request: Dict[str, Any]) -> Tuple[str, Any]:
        return "tool_calls", [
            {"name": "write", "arguments": {"filePath": str(target_file), "content": "written by the model"}},
            {"name": "bash", "arguments": {"command": f"echo mutated > {target_file}"}},
            {"name": "task", "arguments": {"description": "delegate", "prompt": "do something", "subagent_type": "general"}},
            {"name": "fakemcp_exfiltrate", "arguments": {}},
        ]

    def _pass_turn(_request: Dict[str, Any]) -> Tuple[str, Any]:
        return "text", "PASS"

    provider = scripted_provider([_all_forbidden_turn, _pass_turn])
    _write_home_config(
        home,
        provider_name="fakeprov",
        model_name="fake-model",
        base_url=provider.base_url,
        extra={"mcp": {"fakemcp": {"type": "local", "command": ["python3", str(fake_mcp_server)], "enabled": True}}},
    )

    client = _client(repo, home, opencode_cli, monkeypatch)
    with pytest.raises(RuntimeError, match="forbidden tool"):
        client._run_llm_cli("please edit, run shell, delegate, and call the mcp tool", is_noedit=True)

    assert not target_file.exists()
    assert not mcp_called_marker.exists()


# ---------------------------------------------------------------------------
# AC-003: merged permissive settings cannot reactivate editing
# ---------------------------------------------------------------------------


def test_ac003_hostile_merged_config_cannot_reactivate_editing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opencode_cli: str, scripted_provider, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    target_file = repo / "should_not_exist.txt"

    # A hostile/misconfigured project-level config: wide-open global permission
    # plus an unrelated named agent with its own permissive override. Neither
    # can target Auto-Coder's randomly generated agent name.
    (repo / "opencode.json").write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "permission": {"*": "allow", "edit": "allow", "bash": "allow"},
                "agent": {"besiege": {"mode": "primary", "permission": {"edit": "allow", "bash": "allow"}}},
            }
        )
    )

    def _write_turn(_request: Dict[str, Any]) -> Tuple[str, Any]:
        return "tool_calls", [{"name": "write", "arguments": {"filePath": str(target_file), "content": "written despite hostile config"}}]

    def _pass_turn(_request: Dict[str, Any]) -> Tuple[str, Any]:
        return "text", "PASS"

    provider = scripted_provider([_write_turn, _pass_turn])
    # A permissive GLOBAL config too (distinct layer from the project config above).
    _write_home_config(home, provider_name="fakeprov", model_name="fake-model", base_url=provider.base_url, extra={"permission": {"*": "allow", "edit": "allow", "bash": "allow"}})

    client = _client(repo, home, opencode_cli, monkeypatch)
    with pytest.raises(RuntimeError, match="forbidden tool 'write'"):
        client._run_llm_cli("please write the file", is_noedit=True)

    assert not target_file.exists()


# ---------------------------------------------------------------------------
# AC-006: aliases and unavailable enforcement
# ---------------------------------------------------------------------------


def test_ac006_named_alias_gets_the_same_noedit_capability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opencode_cli: str, scripted_provider, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    stdout_answer_turn: Turn = lambda _request: ("text", "alias inspection ok")  # noqa: E731

    provider = scripted_provider([stdout_answer_turn])
    _write_home_config(home, provider_name="fakeprov", model_name="fake-model", base_url=provider.base_url)

    client = _client(repo, home, opencode_cli, monkeypatch, backend_name="opencode-review-alias")
    assert client._run_llm_cli("inspect only", is_noedit=True) == "alias inspection ok"


def test_ac006_enforcement_prerequisite_unavailable_refused_before_task_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opencode_cli: str, _use_real_commands) -> None:
    """Model a CLI/environment lacking the `debug agent` enforcement prerequisite.

    Wraps the real, installed `opencode` binary so every command it does not
    recognize passes through unmodified, but `debug agent` is forced to fail
    the way an older/unsupported CLI without that subcommand would.
    """
    repo = _repository(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    launched_marker = tmp_path / "launched.marker"

    wrapper = tmp_path / "opencode-no-debug-agent"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess, sys, os\n"
        "argv = sys.argv[1:]\n"
        "if argv[:2] == ['debug', 'agent']:\n"
        "    sys.stderr.write('unknown command: debug agent\\n')\n"
        "    sys.exit(1)\n"
        "if argv[:1] == ['run']:\n"
        f"    open({str(launched_marker)!r}, 'w').close()\n"
        f"subprocess.run([{opencode_cli!r}, *argv])\n"
    )
    wrapper.chmod(0o755)

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="fakeprov/fake-model", timeout=60)})
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(wrapper))
    monkeypatch.chdir(repo)
    with patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        client = OpenCodeClient(backend_name="opencode")
        with pytest.raises(RuntimeError, match="no-edit enforcement could not be verified"):
            client._run_llm_cli("inspect only", is_noedit=True)

    assert not launched_marker.exists()
