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

import datetime
import json
import os
import select
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__ as AUTO_CODER_VERSION
from .exceptions import AutoCoderUsageLimitError
from .graphrag_mcp_integration import GraphRAGMCPIntegration
from .llm_backend_config import get_llm_config
from .llm_client_base import LLMClientBase
from .logger_config import get_logger

logger = get_logger(__name__)


def _safe_debug(msg: Any) -> None:
    """Safely call logger.debug, ignoring errors during shutdown."""
    try:
        # Check if logger handlers are still valid
        if not logger._core.handlers:
            return

        # Skip logging of Mock/MagicMock objects to avoid spam
        if "MagicMock" in str(msg) or "Mock" in str(msg):
            return

        logger.debug(msg)
    except Exception:
        # Silently ignore any logging errors during cleanup
        pass


def _is_running_under_pytest() -> bool:
    """Check if we're running under pytest."""
    return "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ


def _pump_bytes(stream: Any, log_fn: Any) -> None:
    try:
        for line in iter(stream.readline, b""):
            try:
                log_fn(line.decode(errors="ignore").rstrip("\n"))
            except (ValueError, BrokenPipeError):
                # Ignore "I/O operation on closed file" errors during shutdown
                pass
            except Exception:
                # Ignore other logging errors
                pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


class CodexMCPClient(LLMClientBase):
    """Codex MCP client maintaining a persistent MCP subprocess.

    Exposes a GeminiClient-compatible API used by AutomationEngine:
    - _run_gemini_cli(prompt: str) -> str
    - switch_to_conflict_model() / switch_to_default_model() as no-ops
    - close() to terminate the persistent MCP process
    """

    def __init__(
        self,
        model_name: Optional[str] = None,
        enable_graphrag: bool = False,
    ) -> None:
        config = get_llm_config()
        config_backend = config.get_backend_config("codex-mcp")

        # Use provided value, fall back to config, then to default
        self.model_name = model_name or (config_backend and config_backend.model) or "codex-mcp"
        self.default_model = self.model_name
        self.conflict_model = self.model_name
        self.proc: Optional[subprocess.Popen] = None
        self.enable_graphrag = enable_graphrag
        self.graphrag_integration: Optional[GraphRAGMCPIntegration] = None

        # Initialize GraphRAG integration if enabled
        if self.enable_graphrag:
            logger.info("GraphRAG integration enabled for CodexMCPClient")
            self.graphrag_integration = GraphRAGMCPIntegration()

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
                bufsize=0,
                env=env,
            )
            logger.info(f"spawned MCP process pid={self.proc.pid}; cmd={' '.join(cmd)}")

            # Keep only stderr pump for diagnostics; stdout is used for JSON-RPC
            # Skip this during tests to avoid shutdown race conditions with loguru's enqueue=True
            if self.proc.stderr is not None and not _is_running_under_pytest():
                threading.Thread(
                    target=_pump_bytes,
                    args=(self.proc.stderr, _safe_debug),
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
        self._default_timeout = float(os.environ.get("AUTOCODER_MCP_TIMEOUT", "60"))
        self._handshake_timeout = float(os.environ.get("AUTOCODER_MCP_HANDSHAKE_TIMEOUT", "1.0"))

        try:
            _ = self._rpc_call(
                method="initialize",
                params={
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "auto-coder", "version": AUTO_CODER_VERSION},
                },
                timeout=self._handshake_timeout,
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
    def _wait_for_stdout(self, timeout: Optional[float]) -> bool:
        if self._stdout is None:
            raise RuntimeError("MCP stdout not available")
        if timeout is not None and timeout < 0:
            timeout = 0.0
        try:
            ready, _, _ = select.select([self._stdout], [], [], timeout)
        except (ValueError, OSError) as exc:
            raise RuntimeError(f"Failed waiting on MCP stdout: {exc}")
        return bool(ready)

    def _read_headers(self, deadline: Optional[float]) -> int:
        if self._stdout is None:
            raise RuntimeError("MCP stdout not available")
        # Read until CRLFCRLF
        raw_headers = b""
        while True:
            timeout = None
            if deadline is not None:
                timeout = deadline - time.time()
                if timeout <= 0:
                    raise TimeoutError("Timed out waiting for MCP headers")
            if not self._wait_for_stdout(timeout):
                raise TimeoutError("Timed out waiting for MCP headers")
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

    def _read_n(self, n: int, deadline: Optional[float]) -> bytes:
        if self._stdout is None:
            raise RuntimeError("MCP stdout not available")
        buf = b""
        while len(buf) < n:
            timeout = None
            if deadline is not None:
                timeout = deadline - time.time()
                if timeout <= 0:
                    raise TimeoutError("Timed out waiting for MCP body")
            if not self._wait_for_stdout(timeout):
                raise TimeoutError("Timed out waiting for MCP body")
            chunk = self._stdout.read(n - len(buf))
            if not chunk:
                raise RuntimeError("EOF while reading MCP message body")
            buf += chunk
        return buf

    def _read_message(self, timeout: Optional[float]) -> Dict[str, Any]:
        deadline = None
        if timeout is not None:
            deadline = time.time() + timeout
        length = self._read_headers(deadline)
        body = self._read_n(length, deadline)
        try:
            parsed = json.loads(body.decode("utf-8"))
            result: Dict[str, Any] = parsed if isinstance(parsed, dict) else {}
            return result
        except Exception as e:
            raise RuntimeError(f"Invalid JSON-RPC body: {e}")

    def _send_message(self, obj: Dict[str, Any]) -> None:
        if self._stdin is None:
            raise RuntimeError("MCP stdin not available")
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
        self._stdin.write(header + data)
        self._stdin.flush()

    def _rpc_call(
        self,
        method: str,
        params: Dict[str, Any] | None = None,
        req_id: int | None = None,
        timeout: Optional[float] = None,
    ) -> Any:
        if self.proc is None or self.proc.poll() is not None:
            raise RuntimeError("MCP process not running")
        self._req_id = (self._req_id + 1) if req_id is None else req_id
        message = {"jsonrpc": "2.0", "id": self._req_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send_message(message)
        effective_timeout = timeout if timeout is not None else self._default_timeout
        resp = self._read_message(effective_timeout)
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

    def _log_jsonrpc_event(self, event_type: str, method: str, params: Dict[str, Any] | None, result: Any = None, error: str | None = None) -> None:
        """Log JSON-RPC events in structured JSON format."""
        log_entry: Dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "type": event_type,
            "method": method,
        }
        if params is not None:
            log_entry["params"] = params
        if result is not None:
            log_entry["result"] = self._extract_text_from_result(result) if not isinstance(result, str) else result
        if error is not None:
            log_entry["error"] = error
        logger.info(json.dumps(log_entry, ensure_ascii=False))

    def _log_fallback_event(self, cmd: List[str], output: str, return_code: int) -> None:
        """Log fallback exec events in structured JSON format."""
        log_entry = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "type": "fallback_exec",
            "command": " ".join(cmd),
            "output": output,
            "return_code": return_code,
        }
        logger.info(json.dumps(log_entry, ensure_ascii=False))

    def _run_llm_cli(self, prompt: str) -> str:
        """Prefer MCP single-shot methods; fallback to echo; then to codex exec.

        Attempt order when MCP handshake succeeded:
        1) prompts/call (name=default, arguments.input)
        2) inference/create (arguments.input)
        3) tools/call name in [run, execute, workspace-write]
        4) tools/call name=echo
        Finally, fallback to `codex exec`.
        """
        # Ensure GraphRAG environment is ready if enabled
        if self.graphrag_integration:
            try:
                if not self.graphrag_integration.ensure_ready():
                    logger.warning("GraphRAG environment not ready, continuing without it")
            except Exception as e:
                logger.warning(f"Failed to ensure GraphRAG environment: {e}")

        escaped_prompt = self._escape_prompt(prompt)

        # Try MCP single-shot first
        if getattr(self, "_initialized", False):
            # 1) prompts/call (default)
            try:
                params = {"name": "default", "arguments": {"input": escaped_prompt}}
                self._log_jsonrpc_event("jsonrpc_call", "prompts/call", params)
                res = self._rpc_call(
                    method="prompts/call",
                    params=params,
                )
                self._log_jsonrpc_event("jsonrpc_result", "prompts/call", params, result=res)
                return self._extract_text_from_result(res)
            except Exception as e:
                self._log_jsonrpc_event("jsonrpc_error", "prompts/call", None, error=str(e))
            # 2) inference/create
            try:
                params = {"arguments": {"input": escaped_prompt}}
                self._log_jsonrpc_event("jsonrpc_call", "inference/create", params)
                res = self._rpc_call(
                    method="inference/create",
                    params=params,
                )
                self._log_jsonrpc_event("jsonrpc_result", "inference/create", params, result=res)
                return self._extract_text_from_result(res)
            except Exception as e:
                self._log_jsonrpc_event("jsonrpc_error", "inference/create", None, error=str(e))
            # 3) tools/call with common names
            for tool_name in ("run", "execute", "workspace-write"):
                try:
                    params = {
                        "name": tool_name,
                        "arguments": {
                            "text": escaped_prompt,
                            "input": escaped_prompt,
                        },
                    }
                    self._log_jsonrpc_event("jsonrpc_call", "tools/call", params)
                    res = self._rpc_call(
                        method="tools/call",
                        params=params,
                    )
                    self._log_jsonrpc_event("jsonrpc_result", "tools/call", params, result=res)
                    return self._extract_text_from_result(res)
                except Exception as e:
                    self._log_jsonrpc_event("jsonrpc_error", "tools/call", None, error=str(e))
                    continue
            # 4) tools/call echo as last MCP attempt
            try:
                self._log_jsonrpc_event("jsonrpc_call", "tools/call", {"name": "echo", "arguments": {"text": escaped_prompt}})
                result = self._call_echo_tool(escaped_prompt)
                self._log_jsonrpc_event("jsonrpc_result", "tools/call", {"name": "echo", "arguments": {"text": escaped_prompt}}, result=result)
                return result
            except Exception as e:
                self._log_jsonrpc_event("jsonrpc_error", "tools/call", None, error=str(e))
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

            usage_markers = (
                "rate limit",
                "usage limit",
                "upgrade to pro",
                "too many requests",
            )

            # Capture output without streaming to logger
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
                output_lines.append(line)
            return_code = proc.wait()
            output = "\n".join(output_lines).strip()

            # Log full response once using JSON format
            self._log_fallback_event(cmd, output, return_code)

            if return_code != 0:
                raise RuntimeError(f"codex exec failed with return code {return_code}")
            return output
        except Exception as e:
            raise RuntimeError(f"Failed to run codex exec under MCP session: {e}")

    def close(self) -> None:
        """Terminate the persistent MCP process if running."""
        try:
            # Cleanup GraphRAG integration
            if self.graphrag_integration:
                try:
                    self.graphrag_integration.cleanup()
                except Exception as e:
                    logger.warning(f"Error cleaning up GraphRAG integration: {e}")

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

    def check_mcp_server_configured(self, server_name: str) -> bool:
        """Check if a specific MCP server is configured for Codex CLI.

        Args:
            server_name: Name of the MCP server to check (e.g., 'graphrag', 'mcp-pdb')

        Returns:
            True if the MCP server is configured, False otherwise
        """
        try:
            result = subprocess.run(
                ["codex", "mcp", "list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                output = result.stdout.lower()
                if server_name.lower() in output:
                    logger.info(f"Found MCP server '{server_name}' via 'codex mcp list'")
                    return True
                logger.debug(f"MCP server '{server_name}' not found via 'codex mcp list'")
                return False
            else:
                logger.debug(f"'codex mcp list' command failed with return code {result.returncode}")
                return False
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.debug(f"Failed to check Codex MCP config: {e}")
            return False

    def add_mcp_server_config(self, server_name: str, command: str, args: list[str]) -> bool:
        """Add MCP server configuration to Codex CLI config.

        Args:
            server_name: Name of the MCP server (e.g., 'graphrag', 'mcp-pdb')
            command: Command to run the MCP server (e.g., 'uv', '/path/to/script.sh')
            args: Arguments for the command (e.g., ['run', 'main.py'] or [])

        Returns:
            True if configuration was added successfully, False otherwise
        """
        try:
            # Use ~/.codex/config.json as primary location
            config_dir = Path.home() / ".codex"
            config_path = config_dir / "config.json"

            # Create directory if it doesn't exist
            config_dir.mkdir(parents=True, exist_ok=True)

            # Read existing config or create new one
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
            else:
                config = {}

            # Add MCP server
            if "mcpServers" not in config:
                config["mcpServers"] = {}

            config["mcpServers"][server_name] = {"command": command, "args": args}

            # Write config
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)

            logger.info(f"Added MCP server '{server_name}' to {config_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to add Codex MCP config: {e}")
            return False
