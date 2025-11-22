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
from typing import Any, Dict, List, Optional

from .exceptions import AutoCoderUsageLimitError
from .llm_backend_config import get_llm_config
from .llm_client_base import LLMClientBase
from .logger_config import get_logger
from .prompt_loader import render_prompt
from .utils import CommandExecutor

logger = get_logger(__name__)


class QwenClient(LLMClientBase):
    """Qwen Code CLI client.

    Note: Qwen Code is adapted from Gemini CLI. We assume a similar non-interactive CLI interface.
    Tests mock subprocess, so no external dependency is required to run tests.
    """

    def __init__(
        self,
        model_name: Optional[str] = None,
        openai_api_key: Optional[str] = None,
        openai_base_url: Optional[str] = None,
        use_env_vars: bool = True,
        preserve_existing_env: bool = False,
    ) -> None:
        """Initialize QwenClient.

        Args:
            model_name: Model name to use (will use config default if not provided)
            openai_api_key: OpenAI API key (will use config value if not provided)
            openai_base_url: OpenAI base URL (will use config value if not provided)
            use_env_vars: If True, pass credentials via environment variables.
                         If False, use command-line options (default: True)
            preserve_existing_env: If True, preserve existing OPENAI_* env vars.
                                  If False, clear them before setting new values (default: False)
        """
        config = get_llm_config()
        config_backend = config.get_backend_config("qwen")

        # Use provided values, fall back to config, then to default
        self.model_name = model_name or (config_backend and config_backend.model) or "qwen3-coder-plus"
        self.default_model = self.model_name
        # Use a faster/cheaper coder variant for conflict resolution when switching
        self.conflict_model = self.model_name
        self.timeout: Optional[int] = None
        # Use provided values or config values, with environment variables as another fallback
        self.openai_api_key = openai_api_key or (config_backend and config_backend.openai_api_key) or os.environ.get("OPENAI_API_KEY")
        self.openai_base_url = openai_base_url or (config_backend and config_backend.openai_base_url) or os.environ.get("OPENAI_BASE_URL")
        self.use_env_vars = use_env_vars
        self.preserve_existing_env = preserve_existing_env

        # Provider management is now handled by BackendProviderManager via BackendManager
        # We only need to verify that at least one CLI is available
        self._last_used_model: Optional[str] = self.model_name

        # Verify required CLIs are available
        # Check if codex is needed (for providers with api_key or base_url)
        # Check if qwen is needed (for OAuth fallback)
        # We check both to provide clear error messages
        try:
            result = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                logger.warning("codex CLI not working, but will continue (may be unused)")
        except Exception:
            logger.debug("codex CLI not available (providers may not use it)")

        try:
            result = subprocess.run(["qwen", "--version"], capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                raise RuntimeError("qwen CLI not available or not working (required for OAuth fallback)")
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
    def _run_qwen_cli(self, prompt: str) -> str:
        """Run qwen CLI with the given prompt and stream output line by line.

        Provider management is now handled by BackendManager. This method receives
        the provider information via environment variables set by BackendManager.

        Args:
            prompt: The prompt to send to the LLM

        Returns:
            The LLM's response as a string
        """
        escaped_prompt = self._escape_prompt(prompt)

        # Get provider settings from environment variables (set by BackendProviderManager)
        # These are exported as uppercase settings from provider_metadata.toml
        provider_api_key = os.environ.get("QWEN_API_KEY") or os.environ.get("OPENAI_API_KEY")
        provider_base_url = os.environ.get("QWEN_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
        provider_model = os.environ.get("QWEN_MODEL")

        # Use qwen CLI for OAuth (no provider) - this is the default fallback
        # Use codex CLI when provider has api_key or base_url (OpenAI-compatible providers)
        use_codex = bool(provider_api_key or provider_base_url)

        if use_codex:
            return self._run_codex_cli(escaped_prompt, provider_model, provider_api_key, provider_base_url)
        else:
            return self._run_qwen_oauth_cli(escaped_prompt, provider_model)

    def _run_codex_cli(
        self,
        escaped_prompt: str,
        model: Optional[str],
        api_key: Optional[str],
        base_url: Optional[str],
    ) -> str:
        """Run codex CLI with OpenAI-compatible provider settings."""
        env = os.environ.copy()

        model_to_use = model or self.default_model

        if not self.preserve_existing_env:
            # Reset OPENAI_* values before applying overrides
            env.pop("OPENAI_API_KEY", None)
            env.pop("OPENAI_BASE_URL", None)

        # Use codex exec with -c options for model_provider and model
        cmd = [
            "codex",
            "exec",
            "-s",
            "workspace-write",
            "--dangerously-bypass-approvals-and-sandbox",
        ]

        # Set model
        if model_to_use:
            cmd.extend(["-c", f'model="{model_to_use}"'])

        # Set API key and base URL via environment variables
        if api_key:
            env["OPENAI_API_KEY"] = api_key
        if base_url:
            env["OPENAI_BASE_URL"] = base_url

        # Add prompt
        cmd.append(escaped_prompt)

        return self._execute_cli(cmd, "codex", env, model_to_use)

    def _run_qwen_oauth_cli(self, escaped_prompt: str, model: Optional[str]) -> str:
        """Run qwen CLI for OAuth (no provider credentials)."""
        env = os.environ.copy()

        model_to_use = model or self.default_model

        if not self.preserve_existing_env:
            # Reset OPENAI_* values before applying overrides
            env.pop("OPENAI_API_KEY", None)
            env.pop("OPENAI_BASE_URL", None)
            env.pop("OPENAI_MODEL", None)

        cmd = ["qwen", "-y"]

        if self.use_env_vars:
            # Pass credentials via environment variables
            if model_to_use:
                env["OPENAI_MODEL"] = model_to_use
            # Model flag for qwen CLI
            if model_to_use:
                cmd.extend(["-m", model_to_use])
        else:
            # Pass credentials via command-line options
            if model_to_use:
                cmd.extend(["-m", model_to_use])

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

        if self._is_usage_limit(full_output, result.returncode):
            raise AutoCoderUsageLimitError(full_output)

        if result.returncode != 0:
            raise RuntimeError(f"{cli_name} CLI failed with return code {result.returncode}\n{full_output}")

        return full_output

    @staticmethod
    def _is_usage_limit(message: str, returncode: int) -> bool:
        """Check if the error message indicates a usage limit."""
        low = message.lower()
        # rate limit with Qwen OAuth
        if "rate limit" in low or "quota" in low:
            return True
        if returncode != 0 and ("429" in low or "too many requests" in low):
            return True
        # rate limit with 'Alibaba Cloud ModelStudio compatible endpoint'
        if "error: 400 model access denied." in low:
            return True
        if "openai api streaming error: 429 free allocated quota exceeded." in low:
            return True
        # rate limit with 'OpenRouter free tier compatible endpoint'
        if "openai api streaming error: 429 provider returned error" in low:
            return True
        return False

    def _run_llm_cli(self, prompt: str) -> str:
        """Execute LLM with the given prompt.

        This method is called by BackendManager which handles provider rotation.
        Environment variables for the current provider are set by BackendManager.

        Args:
            prompt: The prompt to send to the LLM

        Returns:
            The LLM's response as a string
        """
        return self._run_qwen_cli(prompt)

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
