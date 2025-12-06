"""
Qwen Code CLI client for Auto-Coder.

Design: mirror GeminiClient/CodexClient public surface so AutomationEngine can use it transparently.
- _run_gemini_cli(prompt: str) -> str
- switch_to_conflict_model() / switch_to_default_model()
- suggest_features(repo_context)
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any, Dict, List, Optional

from .exceptions import AutoCoderTimeoutError, AutoCoderUsageLimitError
from .llm_backend_config import get_llm_config
from .llm_client_base import LLMClientBase
from .llm_output_logger import LLMOutputLogger
from .logger_config import get_logger
from .prompt_loader import render_prompt
from .usage_marker_utils import has_usage_marker_match
from .utils import CommandExecutor

logger = get_logger(__name__)


class QwenClient(LLMClientBase):
    """Qwen Code CLI client.

    Note: Qwen Code is adapted from Gemini CLI. We assume a similar non-interactive CLI interface.
    Tests mock subprocess, so no external dependency is required to run tests.
    """

    def __init__(
        self,
        backend_name: Optional[str] = None,
        use_env_vars: bool = True,
        preserve_existing_env: bool = False,
    ) -> None:
        """Initialize QwenClient.

        Args:
            backend_name: Backend name to use for configuration lookup (optional).
            use_env_vars: If True, pass credentials via environment variables.
                         If False, use command-line options (default: True)
            preserve_existing_env: If True, preserve existing OPENAI_* env vars.
                                  If False, clear them before setting new values (default: False)
        """
        super().__init__()
        config = get_llm_config()
        self.config_backend = config.get_backend_config(backend_name or "qwen")
        self.model_name = (self.config_backend and self.config_backend.model) or "qwen3-coder-plus"

        def _as_list(value: Any) -> List[str]:
            if isinstance(value, list):
                return value
            if isinstance(value, tuple):
                return list(value)
            return []

        options_for_noedit = _as_list(getattr(self.config_backend, "options_for_noedit", None) if self.config_backend else None)
        general_options = _as_list(self.config_backend.options if self.config_backend else None)
        # Backwards compatibility: use options_for_noedit if present, otherwise use general options
        # This maintains the old behavior for existing configs
        self.options = options_for_noedit or general_options
        self.options_for_noedit = options_for_noedit
        self.api_key = self.config_backend and self.config_backend.api_key
        self.base_url = self.config_backend and self.config_backend.base_url
        self.openai_api_key = self.config_backend and self.config_backend.openai_api_key
        self.openai_base_url = self.config_backend and self.config_backend.openai_base_url
        # Store usage_markers from config
        self.usage_markers = _as_list(getattr(self.config_backend, "usage_markers", None) if self.config_backend else None)

        self.default_model = self.model_name
        # Use a faster/cheaper coder variant for conflict resolution when switching
        self.conflict_model = self.model_name
        self.timeout: Optional[int] = None
        self.use_env_vars = use_env_vars
        self.preserve_existing_env = preserve_existing_env

        # Validate required options for this backend
        if self.config_backend:
            required_errors = self.config_backend.validate_required_options()
            if required_errors:
                for error in required_errors:
                    logger.warning(error)

        # Initialize LLM output logger
        self.output_logger = LLMOutputLogger()

        # Verify qwen CLI is available
        try:
            result = subprocess.run(["qwen", "--version"], capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                raise RuntimeError("qwen CLI not available or not working")
        except Exception as e:
            raise RuntimeError(f"qwen CLI not available: {e}")

    # ----- Model switching (keep simple; Qwen may not need to switch models) -----
    def switch_to_conflict_model(self) -> None:
        # Keep same model by default. In future, allow switching to a lighter model.
        self.model_name = self.conflict_model
        logger.debug("QwenClient.switch_to_conflict_model: active model -> %s", self.model_name)

    def switch_to_default_model(self) -> None:
        # Restore to the initially configured default model.
        self.model_name = self.default_model
        logger.debug("QwenClient.switch_to_default_model: active model -> %s", self.model_name)

    # ----- Helpers -----
    def _escape_prompt(self, prompt: str) -> str:
        # Qwen Code does not require special escaping like Gemini's @; keep minimal sanitation
        return prompt.strip()

    # ----- Core execution -----
    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        """Execute LLM with the given prompt.

        This method is called by BackendManager which handles provider rotation.
        Environment variables for the current provider are set by BackendManager.

        Args:
            prompt: The prompt to send to the LLM
            is_noedit: Whether this is a no-edit operation (uses options_for_noedit)

        Returns:
            The LLM's response as a string
        """
        escaped_prompt = self._escape_prompt(prompt)

        # Get model from environment or use current model
        provider_model = os.environ.get("QWEN_MODEL")

        # Always use OAuth path (native Qwen CLI)
        # Note: options_for_noedit is already used in self.options during initialization
        return self._run_qwen_cli(escaped_prompt, provider_model, is_noedit)

    def _run_qwen_cli(self, escaped_prompt: str, model: Optional[str], is_noedit: bool = False) -> str:
        """Run qwen CLI for OAuth (no provider credentials)."""
        env = os.environ.copy()

        if self.api_key:
            env["QWEN_API_KEY"] = self.api_key
        if self.base_url:
            env["QWEN_BASE_URL"] = self.base_url
        if self.openai_api_key:
            env["OPENAI_API_KEY"] = self.openai_api_key
        if self.openai_base_url:
            env["OPENAI_BASE_URL"] = self.openai_base_url

        # Get model: use provider_model from env, or self.model_name if not set
        # If provider_model is provided (from env), use it
        # Otherwise use current self.model_name (may have been switched)
        # If that's also not set, fall back to self.default_model
        if model:
            model_to_use = model
        elif self.model_name:
            model_to_use = self.model_name
        else:
            model_to_use = self.default_model

        # Pass credentials via environment variables if use_env_vars is True
        if self.use_env_vars:
            if not self.preserve_existing_env:
                # Reset QWEN_MODEL value before applying overrides
                env.pop("QWEN_MODEL", None)
            env["QWEN_MODEL"] = model_to_use

        cmd = ["qwen"]

        # Get processed options with placeholders replaced
        if self.config_backend:
            processed_options = self.config_backend.replace_placeholders(model_name=model_to_use)
            # Use options_for_noedit if is_noedit is True, otherwise use general options
            if is_noedit and processed_options["options_for_noedit"]:
                options_to_use = processed_options["options_for_noedit"]
            else:
                options_to_use = processed_options["options"]
        else:
            # Fallback if config_backend is not available
            options_to_use = self.options

        # Add configured options from config
        if options_to_use:
            cmd.extend(options_to_use)

        # Add model flag if model is specified
        if model_to_use:
            cmd.extend(["-m", model_to_use])

        # Apply any extra arguments (e.g., session resume flags) before the prompt
        extra_args = self.consume_extra_args()
        if extra_args:
            cmd.extend(extra_args)

        # Add prompt flag and prompt
        cmd.extend(["-p", escaped_prompt])

        return self._execute_cli(cmd, "qwen", env, model_to_use)

    def _execute_cli(
        self,
        cmd: List[str],
        cli_name: str,
        env: Dict[str, str],
        model: Optional[str],
    ) -> str:
        """Execute a CLI command and handle the response.

        Args:
            cmd: Command to execute
            cli_name: Name of the CLI for logging
            env: Environment variables
            model: Model name for logging

        Returns:
            The CLI's response as a string
        """
        start_time = time.time()
        status = "success"
        error_message = None
        prompt = ""  # We'll extract this from cmd if available
        full_output = ""

        # Try to extract prompt from command (last argument typically)
        if len(cmd) > 0:
            # The prompt is typically the last argument
            prompt = cmd[-1]

        try:
            display_model = model or self.default_model
            display_cmd = " ".join(cmd[:3]) + "..." if len(cmd) > 3 else " ".join(cmd)

            logger.warning(
                "LLM invocation: %s CLI is being called. Keep LLM calls minimized.",
                cli_name,
            )
            logger.info(
                "🤖 Running (qwen): %s",
                display_cmd,
            )
            logger.info("=" * 60)

            result = CommandExecutor.run_command(
                cmd,
                stream_output=True,
                env=env,
            )
            logger.info("=" * 60)

            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            combined_parts = [part for part in (stdout, stderr) if part]
            full_output = "\n".join(combined_parts) if combined_parts else (result.stderr or result.stdout or "")
            full_output = full_output.strip()

            # Check for timeout (returncode -1 and "timed out" in stderr)
            if result.returncode == -1 and "timed out" in full_output.lower():
                status = "error"
                error_message = full_output
                raise AutoCoderTimeoutError(full_output)

            if self._is_usage_limit(full_output, result.returncode):
                status = "error"
                error_message = full_output
                raise AutoCoderUsageLimitError(full_output)

            if result.returncode != 0:
                status = "error"
                error_message = f"{cli_name} CLI failed with return code {result.returncode}\n{full_output}"
                raise RuntimeError(error_message)

            return full_output

        except AutoCoderUsageLimitError:
            # Re-raise without catching
            raise
        except AutoCoderTimeoutError:
            # Re-raise timeout errors
            raise
        except Exception as e:
            raise
        finally:
            # Always log the interaction and print summary
            duration_ms = (time.time() - start_time) * 1000

            # Determine backend name based on cli_name
            backend_name = "qwen"

            # Log to JSON file
            self.output_logger.log_interaction(
                backend=backend_name,
                model=model or self.default_model,
                prompt=prompt,
                response=full_output,
                duration_ms=duration_ms,
                status=status,
                error=error_message,
            )

            # Print user-friendly summary to stdout
            print("\n" + "=" * 60)
            print(f"🤖 {cli_name.upper()} CLI Execution Summary")
            print("=" * 60)
            print(f"Backend: {backend_name}")
            print(f"Model: {model or self.default_model}")
            print(f"Prompt Length: {len(prompt)} characters")
            print(f"Response Length: {len(full_output)} characters")
            print(f"Duration: {duration_ms:.0f}ms")
            print(f"Status: {status.upper()}")
            if error_message:
                print(f"Error: {error_message[:200]}..." if len(error_message) > 200 else f"Error: {error_message}")
            print("=" * 60 + "\n")

    def _is_usage_limit(self, message: str, returncode: int) -> bool:
        """Check if the error message indicates a usage limit."""
        # Use configured usage_markers if available, otherwise fall back to defaults
        if self.usage_markers and isinstance(self.usage_markers, (list, tuple)):
            usage_markers = self.usage_markers
        else:
            # Default hardcoded usage markers
            usage_markers = (
                "rate limit",
                "quota",
                "429",
                "too many requests",
                "error: 400 model access denied.",
                "openai api streaming error: 429 free allocated quota exceeded.",
                "openai api streaming error: 429 provider returned error",
            )

        return has_usage_marker_match(message, usage_markers)

    # ----- Feature suggestion helpers (copy of GeminiClient behavior) -----
    def suggest_features(self, repo_context: Dict[str, Any]) -> List[Dict[str, Any]]:
        prompt = self._create_feature_suggestion_prompt(repo_context)
        try:
            response_text = self._run_llm_cli(prompt)
            suggestions = self._parse_feature_suggestions(response_text)
            logger.info(f"Generated {len(suggestions)} feature suggestions (Qwen)")
            return suggestions
        except Exception as e:
            logger.error(f"Failed to generate feature suggestions (Qwen): {e}")
            return []

    def _create_feature_suggestion_prompt(self, repo_context: Dict[str, Any]) -> str:
        result: str = render_prompt(
            "feature.suggestion",
            repo_name=repo_context.get("name", "Unknown"),
            description=repo_context.get("description", "No description"),
            language=repo_context.get("language", "Unknown"),
            recent_issues=repo_context.get("recent_issues", []),
            recent_prs=repo_context.get("recent_prs", []),
        )
        return result

    def _parse_feature_suggestions(self, response_text: str) -> List[Dict[str, Any]]:
        try:
            start_idx = response_text.find("[")
            end_idx = response_text.rfind("]") + 1
            if start_idx != -1 and end_idx != -1:
                json_str = response_text[start_idx:end_idx]
                result: List[Dict[str, Any]] = json.loads(json_str)
                return result
            return []
        except json.JSONDecodeError:
            return []

    def check_mcp_server_configured(self, server_name: str) -> bool:
        """Check if a specific MCP server is configured for Qwen Code CLI.

        Args:
            server_name: Name of the MCP server to check (e.g., 'graphrag', 'mcp-pdb')

        Returns:
            True if the MCP server is configured, False otherwise
        """
        try:
            result = subprocess.run(
                ["qwen", "mcp", "list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                output = result.stdout.lower()
                if server_name.lower() in output:
                    logger.info(f"Found MCP server '{server_name}' via 'qwen mcp list'")
                    return True
                logger.debug(f"MCP server '{server_name}' not found via 'qwen mcp list'")
                return False
            else:
                logger.debug(f"'qwen mcp list' command failed with return code {result.returncode}")
                return False
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.debug(f"Failed to check Qwen MCP config: {e}")
            return False

    def add_mcp_server_config(self, server_name: str, command: str, args: list[str]) -> bool:
        """Add MCP server configuration to Qwen Code CLI config.

        Args:
            server_name: Name of the MCP server (e.g., 'graphrag', 'mcp-pdb')
            command: Command to run the MCP server (e.g., 'uv', '/path/to/script.sh')
            args: Arguments for the command (e.g., ['run', 'main.py'] or [])

        Returns:
            True if configuration was added successfully, False otherwise
        """
        try:
            # Use qwen mcp add command to add the server
            # Format: qwen mcp add --scope user <name> <command> [args...]
            cmd = ["qwen", "mcp", "add", "--scope", "user", server_name, command] + args

            logger.debug(f"Running command: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0:
                logger.info(f"Added MCP server '{server_name}' via 'qwen mcp add'")
                return True
            else:
                # Check if it's already configured (qwen mcp add may fail if already exists)
                if "already" in result.stderr.lower() or "exists" in result.stderr.lower():
                    logger.info(f"MCP server '{server_name}' already configured in Qwen")
                    return True
                logger.error(f"Failed to add MCP server '{server_name}': " f"returncode={result.returncode}, stderr={result.stderr}")
                return False
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.error(f"Failed to add Qwen MCP config: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error adding Qwen MCP config: {e}")
            return False
