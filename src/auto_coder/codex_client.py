"""
Codex CLI client for Auto-Coder.
"""

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from .exceptions import AutoCoderRetryableBackendError, AutoCoderTimeoutError, AutoCoderUsageLimitError
from .llm_backend_config import get_llm_config
from .llm_client_base import LLMClientBase
from .llm_output_logger import LLMOutputLogger
from .logger_config import get_logger
from .usage_marker_utils import has_usage_marker_match
from .utils import CommandExecutor

logger = get_logger(__name__)


class CodexClient(LLMClientBase):
    """Codex CLI client for analyzing issues and generating solutions."""

    def __init__(
        self,
        backend_name: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        openai_api_key: Optional[str] = None,
        openai_base_url: Optional[str] = None,
        use_noedit_options: bool = False,
        allow_isolated_noedit_sandbox_fallback: bool = False,
        capture_final_message: bool = False,
    ) -> None:
        """Initialize Codex CLI client.

        Args:
            backend_name: Backend name to use for configuration lookup (optional).
                         If provided, will use config for this backend.
            api_key: API key for the backend (optional, for custom backends).
            base_url: Base URL for the backend (optional, for custom backends).
            openai_api_key: OpenAI API key (optional, for OpenAI-compatible backends).
            openai_base_url: OpenAI base URL (optional, for OpenAI-compatible backends).
            use_noedit_options: If True, use options_for_noedit instead of options.
            allow_isolated_noedit_sandbox_fallback: Allow a no-edit review running in
                a disposable worktree to bypass a broken local Linux sandbox.
            capture_final_message: Use Codex's dedicated final-message output for
                structured no-edit review responses.
        """
        super().__init__()
        config = get_llm_config()

        # If backend_name is provided, get config from that backend
        if backend_name:
            self.config_backend = config.get_backend_config(backend_name)
            # Use backend config model, fall back to default "codex"
            self.model_name = (self.config_backend and self.config_backend.model) or "codex"
            # Use options_for_noedit if use_noedit_options is True
            if use_noedit_options and self.config_backend and self.config_backend.options_for_noedit:
                self.options = self.config_backend.options_for_noedit
            else:
                self.options = (self.config_backend and self.config_backend.options) or []
            self.options_for_noedit = (self.config_backend and self.config_backend.options_for_noedit) or []
            self.api_key = api_key or (self.config_backend and self.config_backend.api_key)
            self.base_url = base_url or (self.config_backend and self.config_backend.base_url)
            self.openai_api_key = openai_api_key or (self.config_backend and self.config_backend.openai_api_key)
            self.openai_base_url = openai_base_url or (self.config_backend and self.config_backend.openai_base_url)
            self.model_provider = self.config_backend and self.config_backend.model_provider
            # Store usage_markers from config
            self.usage_markers = (self.config_backend and self.config_backend.usage_markers) or []
        else:
            # Fall back to default codex config
            self.config_backend = config.get_backend_config("codex")
            self.model_name = (self.config_backend and self.config_backend.model) or "codex"
            # Use options_for_noedit if use_noedit_options is True
            if use_noedit_options and self.config_backend and self.config_backend.options_for_noedit:
                self.options = self.config_backend.options_for_noedit
            else:
                self.options = (self.config_backend and self.config_backend.options) or []
            self.options_for_noedit = (self.config_backend and self.config_backend.options_for_noedit) or []
            self.api_key = api_key
            self.base_url = base_url
            self.openai_api_key = openai_api_key
            self.openai_base_url = openai_base_url
            self.model_provider = None
            # Store usage_markers from config
            self.usage_markers = (self.config_backend and self.config_backend.usage_markers) or []

        self.default_model = self.model_name
        self.conflict_model = self.model_name
        self.timeout = None
        self.allow_isolated_noedit_sandbox_fallback = allow_isolated_noedit_sandbox_fallback
        self.capture_final_message = capture_final_message
        self._noedit_sandbox_fallback_required: Optional[bool] = None

        # Validate required options for this backend
        if self.config_backend:
            required_errors = self.config_backend.validate_required_options(is_noedit=use_noedit_options)
            if required_errors:
                for error in required_errors:
                    logger.warning(error)

        # Initialize LLM output logger
        self.output_logger = LLMOutputLogger()
        self._last_session_id: Optional[str] = None
        self._resume_session_id: Optional[str] = None

        # Check if codex CLI is available
        try:
            result = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                raise RuntimeError("codex CLI not available or not working")
        except Exception as e:
            raise RuntimeError(f"codex CLI not available: {e}")

    def switch_to_conflict_model(self) -> None:
        """No-op; codex has no model switching."""
        logger.info("CodexClient: switch_to_conflict_model noop")

    def switch_to_default_model(self) -> None:
        """No-op; codex has no model switching."""
        logger.info("CodexClient: switch_to_default_model noop")

    def _escape_prompt(self, prompt: str) -> str:
        """Escape special characters that may confuse shell/CLI."""
        return prompt.replace("@", "\\@").strip()

    def _requires_isolated_noedit_sandbox_fallback(self) -> bool:
        """Return whether Codex's Linux sandbox cannot start in this container.

        This probe does not invoke an LLM. The fallback is opt-in and is only set by
        the adversarial validator after it has entered a disposable detached worktree.
        """
        if not self.allow_isolated_noedit_sandbox_fallback:
            return False
        if self._noedit_sandbox_fallback_required is not None:
            return self._noedit_sandbox_fallback_required

        try:
            probe = subprocess.run(
                ["codex", "sandbox", "linux", "--", "true"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            probe_output = f"{probe.stdout or ''}\n{probe.stderr or ''}".lower()
            self._noedit_sandbox_fallback_required = probe.returncode != 0 and "bwrap:" in probe_output
        except (OSError, subprocess.SubprocessError):
            self._noedit_sandbox_fallback_required = False

        if self._noedit_sandbox_fallback_required:
            logger.warning("Codex read-only sandbox preflight failed; using the disposable worktree fallback")
        return self._noedit_sandbox_fallback_required

    @staticmethod
    def _has_usage_limit_diagnostic(
        stdout: str,
        stderr: str,
        usage_markers: list[object],
        returncode: int,
    ) -> bool:
        """Detect provider limit diagnostics without scanning Codex tool payloads.

        A successful ``codex exec --json`` response can contain arbitrary source
        code and command output inside item events. Those payloads are reviewer
        evidence, not Codex diagnostics, and may legitimately contain configured
        strings such as ``usage limit`` or ``rate limit``.
        """
        full_output = "\n".join(part for part in (stdout, stderr) if part)
        if returncode != 0:
            return has_usage_marker_match(full_output, usage_markers)

        events: list[dict[str, object]] = []
        is_jsonl_stream = False
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                events = []
                is_jsonl_stream = False
                break
            if not isinstance(value, dict):
                events = []
                is_jsonl_stream = False
                break
            event_type = str(value.get("type", ""))
            if event_type in {"thread.started", "turn.started", "turn.completed", "turn.failed", "error"} or event_type.startswith("item."):
                is_jsonl_stream = True
            events.append(value)

        if not is_jsonl_stream:
            return has_usage_marker_match(full_output, usage_markers)

        diagnostic_events = [event for event in events if str(event.get("type", "")) in {"error", "turn.failed"} or event.get("is_error") is True or "error" in event]
        diagnostic_output = "\n".join([stderr, *(json.dumps(event, ensure_ascii=False) for event in diagnostic_events)])
        return has_usage_marker_match(diagnostic_output, usage_markers)

    @staticmethod
    def _retryable_backend_diagnostic(stdout: str, stderr: str) -> Optional[str]:
        """Return provider evidence when Codex exhausted a transport reconnect.

        JSONL diagnostic events are preferred so source code and command output
        cannot accidentally classify a failed implementation as an outage.  A
        plain-text fallback supports CLI versions which emit diagnostics only on
        stderr.  Detection intentionally uses semantic signals rather than a
        captured request ID, endpoint, status, or fixed reconnect count.
        """
        diagnostic_messages: list[str] = []
        terminal_messages: list[str] = []
        saw_json_event = False
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type", ""))
            if event_type in {"thread.started", "turn.started", "error", "turn.failed"}:
                saw_json_event = True
            if event_type in {"error", "turn.failed"} or event.get("is_error") is True:
                message = event.get("message") or event.get("error")
                if message:
                    diagnostic_messages.append(str(message))
                    if event_type == "turn.failed":
                        terminal_messages.append(str(message))

        if stderr:
            diagnostic_messages.append(stderr)
        if not saw_json_event and stdout:
            diagnostic_messages.append(stdout)

        diagnostic = "\n".join(diagnostic_messages).strip()
        if not diagnostic:
            return None

        # A structured terminal failure is authoritative.  Earlier reconnect
        # notices describe intermediate recovery attempts and must not mask a
        # later implementation/agent failure.
        classification_text = terminal_messages[-1] if terminal_messages else diagnostic
        low = classification_text.lower()
        reconnect_counters = re.findall(r"reconnect(?:ing)?(?:\.\.\.)?\s*(\d+)\s*/\s*(\d+)", low)
        counter_exhausted = any(int(current) >= int(limit) for current, limit in reconnect_counters)
        reconnect_exhausted = counter_exhausted or bool(re.search(r"reconnect(?:ion)? (?:attempts? )?(?:exhausted|failed)", low))
        provider_transport = any(
            marker in low
            for marker in (
                "backend-api/codex",
                "wss://",
                "websocket",
                "transport",
                "connection reset",
                "connection refused",
                "service unavailable",
                "upstream unavailable",
            )
        )
        return diagnostic if reconnect_exhausted and provider_transport else None

    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        """Run codex CLI with the given prompt and show real-time output."""
        start_time = time.time()
        status = "success"
        error_message = None
        full_output = ""
        final_message_path: Optional[Path] = None

        try:
            escaped_prompt = self._escape_prompt(prompt)
            cmd = ["codex"]

            # Get processed options with placeholders replaced
            # Use options_for_noedit for no-edit operations if available
            if self.config_backend:
                processed_options = self.config_backend.replace_placeholders(model_name=self.model_name, session_id=None)
                if is_noedit and self.options_for_noedit:
                    options_to_use = processed_options["options_for_noedit"]
                else:
                    options_to_use = processed_options["options"]
            else:
                # Fallback if config_backend is not available
                options_to_use = self.options_for_noedit if is_noedit and self.options_for_noedit else self.options

            # Add configured options from config
            if options_to_use:
                cmd.extend(options_to_use)

            resume_session_id = self._resume_session_id
            self._resume_session_id = None
            if resume_session_id:
                try:
                    exec_index = cmd.index("exec")
                except ValueError:
                    cmd.extend(["exec", "resume"])
                else:
                    cmd.insert(exec_index + 1, "resume")

            # Append any one-time extra arguments (e.g., resume flags)
            extra_args = self.consume_extra_args()
            if extra_args:
                cmd.extend(extra_args)

            # When is_noedit is True, enforce Codex read-only sandboxing client-level invariant
            # against the final combined command (including configured options and extra args):
            # - Remove conflicting --sandbox / -s <value> pairs and --sandbox=... / -s=...
            # - Remove conflicting --sandbox / -s <value> pairs, --sandbox=..., -s=..., and -s<value>
            # - Remove dangerous approval, YOLO, and sandbox bypass flags:
            #   --dangerously-bypass-approvals-and-sandbox, --yolo, --full-auto, -y, --yes,
            #   --approve-for-me, --not-so-yolo, --danger-full-access
            # - Remove conflicting --ask-for-approval / -a <value> pairs, --ask-for-approval=..., -a=..., and -a<value>
            # - Remove any conflicting runtime config overrides for approvals_reviewer, approval_policy,
            #   and sandbox_mode (-c / --config, -c=..., --config=..., -c<value>)
            # - Force exactly --sandbox read-only, --ask-for-approval never, and -c approvals_reviewer="user"
            if is_noedit:
                unsafe_config_keys = ("approvals_reviewer", "approval_reviewer", "approval_policy", "sandbox_mode")

                def _is_unsafe_config_override(override: str) -> bool:
                    """Return True if a Codex ``-c``/``--config`` override targets a protected key.

                    The comparison is performed against the override key (the left-hand
                    side before ``=``) only, so safe overrides whose *value* merely
                    contains a protected name (e.g. ``model="sandbox_mode"``) are kept.
                    """
                    key = override.split("=", 1)[0].strip()
                    return key in unsafe_config_keys

                def _extract_config_override(token: str) -> str:
                    """Extract the ``KEY=VALUE`` payload from a concatenated config flag token."""
                    if token.startswith("--config="):
                        return token[len("--config=") :]
                    if token.startswith("-c="):
                        return token[len("-c=") :]
                    if token.startswith("-c") and not token.startswith("--"):
                        return token[len("-c") :]
                    return ""

                sanitized_cmd = []
                i = 0
                while i < len(cmd):
                    opt_str = str(cmd[i])
                    # Handle sandbox flags (--sandbox <val>, -s <val>, --sandbox=<val>, -s=<val>, -s<val>)
                    if opt_str in ("--sandbox", "-s"):
                        # Skip flag and its subsequent value
                        i += 2
                        continue
                    if opt_str.startswith("--sandbox=") or (opt_str.startswith("-s") and not opt_str.startswith("--")):
                        i += 1
                        continue
                    # Handle ask-for-approval flags (--ask-for-approval <val>, -a <val>, --ask-for-approval=<val>, -a=<val>, -a<val>)
                    if opt_str in ("--ask-for-approval", "-a"):
                        # Skip flag and its subsequent value
                        i += 2
                        continue
                    if opt_str.startswith("--ask-for-approval=") or (opt_str.startswith("-a") and not opt_str.startswith("--")):
                        i += 1
                        continue
                    # Handle config override flags for permission keys (-c <val>, --config <val>, -c=..., --config=..., -c<val>)
                    if opt_str in ("-c", "--config") and i + 1 < len(cmd) and _is_unsafe_config_override(str(cmd[i + 1])):
                        i += 2
                        continue
                    if (opt_str.startswith("--config=") or (opt_str.startswith("-c") and not opt_str.startswith("--") and opt_str != "-c")) and _is_unsafe_config_override(_extract_config_override(opt_str)):
                        i += 1
                        continue
                    # Handle standalone dangerous bypass and YOLO flags
                    if opt_str in (
                        "--dangerously-bypass-approvals-and-sandbox",
                        "--yolo",
                        "--approve-for-me",
                        "--not-so-yolo",
                        "--full-auto",
                        "-y",
                        "--yes",
                        "--danger-full-access",
                    ):
                        i += 1
                        continue
                    sanitized_cmd.append(cmd[i])
                    i += 1

                sandbox_mode = "danger-full-access" if self._requires_isolated_noedit_sandbox_fallback() else "read-only"
                noedit_flags = [
                    "--sandbox",
                    sandbox_mode,
                    "--ask-for-approval",
                    "never",
                    "-c",
                    'approvals_reviewer="user"',
                ]
                if sanitized_cmd:
                    sanitized_cmd = [sanitized_cmd[0], *noedit_flags, *sanitized_cmd[1:]]
                else:
                    sanitized_cmd = ["codex", *noedit_flags]
                cmd = sanitized_cmd

            # Codex's JSONL event stream is useful for diagnostics, but the
            # validator only needs the final assistant payload. Ask Codex to
            # write that payload through its dedicated output channel so an
            # incidental non-JSON stdout line cannot corrupt a valid result.
            if self.capture_final_message and is_noedit and "--json" in cmd:
                final_message_file = tempfile.NamedTemporaryFile(
                    prefix="auto-coder-codex-final-",
                    suffix=".txt",
                    delete=False,
                )
                final_message_path = Path(final_message_file.name)
                final_message_file.close()
                cmd.extend(["--output-last-message", str(final_message_path)])

            if resume_session_id:
                cmd.append(resume_session_id)
            cmd.append(escaped_prompt)
            # Use configured usage_markers if available, otherwise fall back to defaults
            if self.usage_markers and isinstance(self.usage_markers, (list, tuple)):
                usage_markers = self.usage_markers
            else:
                # Default hardcoded usage markers
                usage_markers = [
                    "rate limit",
                    "usage limit",
                    "upgrade to pro",
                    "too many requests",
                ]

            # Prepare environment variables for subprocess
            env = os.environ.copy()
            if self.api_key:
                env["CODEX_API_KEY"] = self.api_key
            if self.base_url:
                env["CODEX_BASE_URL"] = self.base_url
            if self.openai_api_key:
                env["OPENAI_API_KEY"] = self.openai_api_key
            if self.openai_base_url:
                env["OPENAI_BASE_URL"] = self.openai_base_url

            result = CommandExecutor.run_command(
                cmd,
                stream_output=True,
                env=env if len(env) > len(os.environ) else None,
                dot_format=True,
                idle_timeout=1800,
            )

            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            combined_parts = [part for part in (stdout, stderr) if part]
            full_output = "\n".join(combined_parts) if combined_parts else (result.stderr or result.stdout or "")
            full_output = full_output.strip()
            self._extract_session_id(full_output)
            response_output = stdout or stderr
            low = full_output.lower()

            # Check for timeout (returncode -1 and "timed out" in stderr)
            if result.returncode == -1 and "timed out" in low:
                raise AutoCoderTimeoutError(full_output)

            usage_limit_detected = self._has_usage_limit_diagnostic(
                stdout,
                stderr,
                list(usage_markers),
                result.returncode,
            )

            if result.returncode != 0:
                if usage_limit_detected:
                    status = "error"
                    error_message = full_output
                    raise AutoCoderUsageLimitError(full_output)
                backend_diagnostic = self._retryable_backend_diagnostic(stdout, stderr)
                if backend_diagnostic:
                    status = "error"
                    error_message = f"Retryable Codex backend/transport failure: {backend_diagnostic}"
                    raise AutoCoderRetryableBackendError(error_message)
                status = "error"
                error_message = f"codex CLI failed with return code {result.returncode}\n{full_output}"
                raise RuntimeError(error_message)

            if usage_limit_detected:
                status = "error"
                error_message = full_output
                raise AutoCoderUsageLimitError(full_output)

            if final_message_path is not None:
                final_message = final_message_path.read_text(encoding="utf-8").strip()
                if final_message:
                    return final_message

            # Keep stderr in the interaction log and error detection, but do
            # not append diagnostics to a successful response. The JSONL
            # parser remains a fail-closed fallback when Codex did not write a
            # final-message file.
            return response_output
        except AutoCoderUsageLimitError:
            # Re-raise without catching
            raise
        except AutoCoderTimeoutError:
            # Re-raise timeout errors
            raise
        except AutoCoderRetryableBackendError:
            raise
        except Exception as e:
            raise RuntimeError(f"Failed to run codex CLI: {e}")
        finally:
            if final_message_path is not None:
                try:
                    final_message_path.unlink(missing_ok=True)
                except OSError as e:
                    logger.warning(f"Failed to remove temporary Codex final-message file: {e}")

            # Always log the interaction and print summary
            duration_ms = (time.time() - start_time) * 1000

            # Log to JSON file
            self.output_logger.log_interaction(
                backend="codex",
                model=self.model_name,
                prompt=prompt,
                response=full_output,
                duration_ms=duration_ms,
                status=status,
                error=error_message,
            )

            # Print user-friendly summary to stdout
            print("\n" + "=" * 60)
            print("🤖 Codex CLI Execution Summary")
            print("=" * 60)
            print(f"Backend: codex")
            print(f"Model: {self.model_name}")
            print(f"Prompt Length: {len(prompt)} characters")
            print(f"Response Length: {len(full_output)} characters")
            print(f"Duration: {duration_ms:.0f}ms")
            print(f"Status: {status.upper()}")
            if error_message:
                print(f"Error: {error_message[:200]}..." if len(error_message) > 200 else f"Error: {error_message}")
            print("=" * 60 + "\n")

    def get_last_session_id(self) -> Optional[str]:
        return self._last_session_id

    def _extract_session_id(self, output: str) -> None:
        """Extract Codex thread/session IDs from JSONL or diagnostic output."""
        for line in output.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                value = None
            if isinstance(value, dict):
                candidate = value.get("thread_id") or value.get("session_id")
                if isinstance(candidate, str) and candidate.strip():
                    self._last_session_id = candidate.strip()
                    return
        match = re.search(r"(?:thread|session)[ _-]?id\s*[:=]\s*([A-Za-z0-9._-]+)", output, re.IGNORECASE)
        if match:
            self._last_session_id = match.group(1)

    def continue_session(self, session_id: str, prompt: str, is_noedit: bool = False) -> str:
        """Continue a specific Codex session using Codex's resume subcommand."""
        if not session_id or session_id.startswith("-"):
            raise ValueError("A valid explicit Codex session ID is required")
        self._resume_session_id = session_id
        return self._run_llm_cli(prompt, is_noedit=is_noedit)

    def check_mcp_server_configured(self, server_name: str) -> bool:
        """Check if a specific MCP server is configured for Codex CLI.

        Args:
            server_name: Name of the MCP server to check (e.g., 'test-watcher', 'mcp-pdb')

        Returns:
            True if the MCP server is configured, False otherwise
        """
        try:
            result = subprocess.run(
                ["codex", "mcp", "list"],
                capture_output=True,
                text=True,
                timeout=60,
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
            server_name: Name of the MCP server (e.g., 'test-watcher', 'mcp-pdb')
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
            fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)

            logger.info(f"Added MCP server '{server_name}' to {config_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to add Codex MCP config: {e}")
            return False
