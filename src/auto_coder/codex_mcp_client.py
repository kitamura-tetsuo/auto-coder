"""
Codex MCP client with persistent session for Auto-Coder.

Maintains a single "codex mcp" subprocess during the session and provides a
GeminiClient-compatible surface (notably _run_gemini_cli) so it can be plugged
into AutomationEngine the same way as CodexClient.

Note: This skeleton keeps the MCP transport/process alive and logs I/O. JSON-RPC
handshake/tool invocations are intentionally omitted for simplicity. For now,
_code_run_gemini_cli falls back to running a one-off "codex exec" while the MCP
process remains alive to satisfy the requirement of keeping a session for
"one PR" or "one error-fix" flows.
"""

from __future__ import annotations

import os
import subprocess
import threading
import json
import shlex
import time
from typing import Optional, List, Any, Dict

from .logger_config import get_logger

logger = get_logger(__name__)


def _pump_bytes(stream, log_fn) -> None:
    try:
        for line in iter(stream.readline, b""):
            try:
                log_fn(line.decode(errors="ignore").rstrip("\n"))
            except Exception:
                pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


class CodexMCPClient:
    """Codex MCP client maintaining a persistent MCP subprocess.

    Exposes a GeminiClient-compatible API used by AutomationEngine:
    - _run_gemini_cli(prompt: str) -> str
    - switch_to_conflict_model() / switch_to_default_model() as no-ops
    - close() to terminate the persistent MCP process
    """

    def __init__(self, model_name: str = "codex-mcp") -> None:
        self.model_name = model_name or "codex-mcp"
        self.default_model = self.model_name
        self.conflict_model = self.model_name
        self.proc: Optional[subprocess.Popen] = None

        # Verify codex CLI is available
        try:
            chk = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=10)
            if chk.returncode != 0:
                raise RuntimeError("codex CLI not available or not working")
        except Exception as e:
            raise RuntimeError(f"codex CLI not available: {e}")

        # Spawn MCP server as a child process and keep the connection
        env = os.environ.copy()
        try:
            # Allow overriding the MCP command for testing (e2e) via env var
            # Default remains: codex mcp
            mcp_cmd_env = os.environ.get("AUTOCODER_MCP_COMMAND")
            if mcp_cmd_env:
                cmd = shlex.split(mcp_cmd_env)
            else:
                cmd = ["codex", "mcp"]

            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            logger.info(f"spawned MCP process pid={self.proc.pid}; cmd={' '.join(cmd)}")

            # Keep only stderr pump for diagnostics; stdout is used for JSON-RPC
            if self.proc.stderr is not None:
                threading.Thread(
                    target=_pump_bytes,
                    args=(self.proc.stderr, lambda s: logger.debug(s)),
                    daemon=True,
                ).start()
        except Exception as e:
            raise RuntimeError(f"Failed to start MCP subprocess: {e}")

        # Prepare JSON-RPC state
        self._stdin = getattr(self.proc, "stdin", None) if self.proc is not None else None
        self._stdout = getattr(self.proc, "stdout", None) if self.proc is not None else None
        self._req_id = 0
        self._initialized = False

        # Try minimal JSON-RPC handshake (non-fatal if it fails)
        try:
            _ = self._rpc_call(
                method="initialize",
                params={
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "auto-coder", "version": "0.1.0"},
                },
            )
            self._initialized = True
            logger.info("MCP JSON-RPC initialized successfully")
        except Exception as e:
            logger.warning(f"MCP initialize failed or not supported; will fallback to 'codex exec' for actions: {e}")

    # Compatibility no-ops (Codex has no model switching)
    def switch_to_conflict_model(self) -> None:  # pragma: no cover - trivial
        logger.info("CodexMCPClient: switch_to_conflict_model noop")

    def switch_to_default_model(self) -> None:  # pragma: no cover - trivial
        logger.info("CodexMCPClient: switch_to_default_model noop")

    # --- Minimal JSON-RPC helpers (single-flight; no concurrent calls) ---
    def _read_headers(self) -> int:
        if self._stdout is None:
            raise RuntimeError("MCP stdout not available")
        # Read until CRLFCRLF
        raw_headers = b""
        while True:
            line = self._stdout.readline()
            if not line:
                raise RuntimeError("EOF while reading MCP headers")
            raw_headers += line
            if raw_headers.endswith(b"\r\n\r\n"):
                break
        # Parse Content-Length
        headers_text = raw_headers.decode("utf-8", errors="ignore")
        content_length = None
        for hline in headers_text.split("\r\n"):
            if hline.lower().startswith("content-length:"):
                try:
                    content_length = int(hline.split(":", 1)[1].strip())
                except Exception:
                    pass
        if content_length is None:
            raise RuntimeError("Missing Content-Length header")
        return content_length

    def _read_n(self, n: int) -> bytes:
        if self._stdout is None:
            raise RuntimeError("MCP stdout not available")
        buf = b""
        while len(buf) < n:
            chunk = self._stdout.read(n - len(buf))
            if not chunk:
                raise RuntimeError("EOF while reading MCP message body")
            buf += chunk
        return buf

    def _read_message(self) -> Dict[str, Any]:
        length = self._read_headers()
        body = self._read_n(length)
        try:
            return json.loads(body.decode("utf-8"))
        except Exception as e:
            raise RuntimeError(f"Invalid JSON-RPC body: {e}")

    def _send_message(self, obj: Dict[str, Any]) -> None:
        if self._stdin is None:
            raise RuntimeError("MCP stdin not available")
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
        self._stdin.write(header + data)
        self._stdin.flush()

    def _rpc_call(self, method: str, params: Dict[str, Any] | None = None, req_id: int | None = None) -> Any:
        if self.proc is None or self.proc.poll() is not None:
            raise RuntimeError("MCP process not running")
        self._req_id = (self._req_id + 1) if req_id is None else req_id
        message = {"jsonrpc": "2.0", "id": self._req_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send_message(message)
        resp = self._read_message()
        if "error" in resp:
            raise RuntimeError(f"MCP error: {resp['error']}")
        if resp.get("id") != self._req_id:
            # Not matching; extremely simple client expects strict ordering
            raise RuntimeError("MCP response id mismatch")
        return resp.get("result")

    def _extract_text_from_result(self, result: Any) -> str:
        # MCP tool result commonly: { content: [{type:'text', text:'...'}] }
        try:
            content = result.get("content")
            if isinstance(content, list) and content:
                first = content[0]
                if isinstance(first, dict) and first.get("type") == "text":
                    text = first.get("text", "")
                    if isinstance(text, str):
                        return text
        except Exception:
            pass
        # Fallbacks
        if isinstance(result, str):
            return result
        if isinstance(result, dict) and "text" in result and isinstance(result["text"], str):
            return result["text"]
        return json.dumps(result, ensure_ascii=False)

    def _call_echo_tool(self, text: str) -> str:
        """Call a minimal echo tool over MCP if available.

        This is used to validate handshake and synchronous tool invocation. In
        production, the client still falls back to `codex exec` for complex ops.
        """
        result = self._rpc_call(
            method="tools/call",
            params={"name": "echo", "arguments": {"text": text}},
        )
        return self._extract_text_from_result(result)

    def _escape_prompt(self, prompt: str) -> str:
        return prompt.replace("@", "\\@").strip()

    def _run_gemini_cli(self, prompt: str) -> str:
        """Prefer MCP single-shot methods; fallback to echo; then to codex exec.

        Attempt order when MCP handshake succeeded:
        1) prompts/call (name=default, arguments.input)
        2) inference/create (arguments.input)
        3) tools/call name in [run, execute, workspace-write]
        4) tools/call name=echo
        Finally, fallback to `codex exec`.
        """
        escaped_prompt = self._escape_prompt(prompt)

        # Try MCP single-shot first
        if getattr(self, "_initialized", False):
            # 1) prompts/call (default)
            try:
                res = self._rpc_call(
                    method="prompts/call",
                    params={"name": "default", "arguments": {"input": escaped_prompt}},
                )
                return self._extract_text_from_result(res)
            except Exception:
                pass
            # 2) inference/create
            try:
                res = self._rpc_call(
                    method="inference/create",
                    params={"arguments": {"input": escaped_prompt}},
                )
                return self._extract_text_from_result(res)
            except Exception:
                pass
            # 3) tools/call with common names
            for tool_name in ("run", "execute", "workspace-write"):
                try:
                    res = self._rpc_call(
                        method="tools/call",
                        params={"name": tool_name, "arguments": {"text": escaped_prompt, "input": escaped_prompt}},
                    )
                    return self._extract_text_from_result(res)
                except Exception:
                    continue
            # 4) tools/call echo as last MCP attempt
            try:
                return self._call_echo_tool(escaped_prompt)
            except Exception as e:
                logger.warning(f"MCP attempts failed, will fallback to codex exec: {e}")

        # Fallback: codex exec
        try:
            cmd: List[str] = [
                "codex",
                "exec",
                "-s",
                "workspace-write",
                "--dangerously-bypass-approvals-and-sandbox",
                escaped_prompt,
            ]
            logger.warning("LLM invocation: codex-mcp (codex exec) is being called. Keep LLM calls minimized.")
            logger.debug(f"Running codex exec with prompt length: {len(prompt)} characters (MCP session kept alive)")
            logger.info(
                "🤖 Running under MCP session: codex exec -s workspace-write --dangerously-bypass-approvals-and-sandbox [prompt]"
            )

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            output_lines: List[str] = []
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                logger.info(line)
                output_lines.append(line)
            return_code = proc.wait()
            if return_code != 0:
                raise RuntimeError(f"codex exec failed with return code {return_code}")
            return "\n".join(output_lines).strip()
        except Exception as e:
            raise RuntimeError(f"Failed to run codex exec under MCP session: {e}")

    def close(self) -> None:
        """Terminate the persistent MCP process if running."""
        try:
            if self.proc is not None:
                try:
                    # Try a graceful wait first (short)
                    self.proc.wait(timeout=0.2)
                except Exception:
                    pass
                try:
                    self.proc.terminate()
                except Exception:
                    pass
        finally:
            self.proc = None

