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
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .exceptions import AutoCoderUsageLimitError
from .llm_client_base import LLMClientBase
from .logger_config import get_logger
from .prompt_loader import render_prompt
from .qwen_provider_config import load_qwen_provider_configs
from .utils import CommandExecutor

logger = get_logger(__name__)


class QwenClient(LLMClientBase):
    """Qwen Code CLI client.

    Note: Qwen Code is adapted from Gemini CLI. We assume a similar non-interactive CLI interface.
    Tests mock subprocess, so no external dependency is required to run tests.
    """

    def __init__(
        self,
        model_name: str = "qwen3-coder-plus",
        openai_api_key: Optional[str] = None,
        openai_base_url: Optional[str] = None,
    ):
        self.model_name = model_name or "qwen3-coder-plus"
        self.default_model = self.model_name
        # Use a faster/cheaper coder variant for conflict resolution when switching
        self.conflict_model = self.model_name
        self.timeout: Optional[int] = None
        # OpenAI-compatible env overrides (Qwen backend only)
        self.openai_api_key = openai_api_key
        self.openai_base_url = openai_base_url

        self._provider_chain: List[_QwenProviderOption] = self._build_provider_chain()
        self._active_provider_index: int = 0
        self._last_used_model: Optional[str] = self.model_name

        # Verify qwen CLI is available
        try:
            result = subprocess.run(
                ["qwen", "--version"], capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                raise RuntimeError("qwen CLI not available or not working")
        except Exception as e:
            raise RuntimeError(f"qwen CLI not available: {e}")

    # ----- Model switching (keep simple; Qwen may not need to switch models) -----
    def switch_to_conflict_model(self) -> None:
        # Keep same model by default. In future, allow switching to a lighter model.
        self.model_name = self.conflict_model
        logger.debug(
            "QwenClient.switch_to_conflict_model: active model -> %s", self.model_name
        )

    def switch_to_default_model(self) -> None:
        # Restore to the initially configured default model.
        self.model_name = self.default_model
        logger.debug(
            "QwenClient.switch_to_default_model: active model -> %s", self.model_name
        )

    # ----- Helpers -----
    def _escape_prompt(self, prompt: str) -> str:
        # Qwen Code does not require special escaping like Gemini's @; keep minimal sanitation
        return prompt.strip()

    # ----- Core execution -----
    def _run_qwen_cli(self, prompt: str) -> str:
        """Run qwen CLI with the given prompt and stream output line by line.

        We set OPENAI_* env vars when provided and invoke non-interactively with -p/--prompt.
        """
        escaped_prompt = self._escape_prompt(prompt)

        if not self._provider_chain:
            raise RuntimeError("No Qwen providers are configured")

        usage_errors: List[str] = []
        start_index = self._active_provider_index

        for offset in range(len(self._provider_chain)):
            provider_index = (start_index + offset) % len(self._provider_chain)
            provider = self._provider_chain[provider_index]

            try:
                output = self._execute_with_provider(provider, escaped_prompt, prompt)
                self._active_provider_index = provider_index
                self._last_used_model = provider.model or self.model_name
                self.model_name = self._last_used_model or self.model_name
                return output
            except AutoCoderUsageLimitError as exc:
                usage_errors.append(f"{provider.display_name}: {str(exc).strip()}")
                logger.warning(
                    "Qwen provider '%s' hit usage limit. Trying next provider.",
                    provider.display_name,
                )
                continue

        if usage_errors:
            aggregated = " | ".join(usage_errors)
            raise AutoCoderUsageLimitError(
                f"All Qwen providers reached usage limits: {aggregated}"
            )

        raise RuntimeError("Qwen providers exhausted without usable response")

    def _execute_with_provider(
        self,
        provider: "_QwenProviderOption",
        escaped_prompt: str,
        original_prompt: str,
    ) -> str:
        env = os.environ.copy()

        model_to_use = provider.model or self.default_model

        # Reset OPENAI_* values before applying overrides.
        env.pop("OPENAI_API_KEY", None)
        env.pop("OPENAI_BASE_URL", None)
        env.pop("OPENAI_MODEL", None)

        if provider.api_key:
            env["OPENAI_API_KEY"] = provider.api_key
        if provider.base_url:
            env["OPENAI_BASE_URL"] = provider.base_url
        if model_to_use:
            env["OPENAI_MODEL"] = model_to_use

        cmd = ["qwen", "-y"]
        if model_to_use:
            cmd.extend(["-m", model_to_use])
        cmd.extend(["-p", escaped_prompt])

        logger.warning(
            "LLM invocation: qwen CLI is being called. Keep LLM calls minimized."
        )
        logger.debug(
            "Running qwen CLI with prompt length: %d characters", len(original_prompt)
        )
        logger.info(
            "🤖 Running (%s): qwen %s",
            provider.display_name,
            "-m %s -p [prompt]" % model_to_use if model_to_use else "-p [prompt]",
        )
        logger.info("=" * 60)

        result = CommandExecutor.run_command(
            cmd,
            stream_output=True,
            check_success=False,
            env=env,
        )
        logger.info("=" * 60)

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        combined_parts = [part for part in (stdout, stderr) if part]
        full_output = (
            "\n".join(combined_parts)
            if combined_parts
            else (result.stderr or result.stdout or "")
        )
        full_output = full_output.strip()

        if self._is_usage_limit(full_output, result.returncode):
            raise AutoCoderUsageLimitError(full_output)

        if result.returncode != 0:
            raise RuntimeError(
                f"qwen CLI failed with return code {result.returncode}\n{full_output}"
            )

        return full_output

    @staticmethod
    def _is_usage_limit(message: str, returncode: int) -> bool:
        low = message.lower()
        if "rate limit" in low or "quota" in low:
            return True
        if returncode != 0 and ("429" in low or "too many requests" in low):
            return True
        return False

    def _build_provider_chain(self) -> List["_QwenProviderOption"]:
        providers: List[_QwenProviderOption] = []

        if self.openai_api_key or self.openai_base_url:
            providers.append(
                _QwenProviderOption(
                    name="custom-openai",
                    display_name="Custom OpenAI-compatible",
                    api_key=self.openai_api_key,
                    base_url=self.openai_base_url,
                    model=self.model_name,
                )
            )

        configured = load_qwen_provider_configs()
        for cfg in configured:
            providers.append(
                _QwenProviderOption(
                    name=cfg.name,
                    display_name=cfg.description or cfg.name,
                    api_key=cfg.api_key,
                    base_url=cfg.base_url,
                    model=cfg.model or self.default_model,
                )
            )

        # Always allow falling back to OAuth as the last resort so that API keys are
        # consumed before the default shared pool is hit.
        providers.append(
            _QwenProviderOption(
                name="qwen-oauth",
                display_name="Qwen OAuth",
                api_key=None,
                base_url=None,
                model=self.model_name,
            )
        )

        return providers

    def _run_gemini_cli(self, prompt: str) -> str:
        """Temporary alias for backward compatibility.
        Prefer calling _run_qwen_cli going forward; this delegates to _run_qwen_cli.
        """
        return self._run_qwen_cli(prompt)

    def _run_llm_cli(self, prompt: str) -> str:
        """Neutral alias: delegate to _run_qwen_cli (migration helper)."""
        return self._run_qwen_cli(prompt)

    # ----- Feature suggestion helpers (copy of GeminiClient behavior) -----
    def suggest_features(self, repo_context: Dict[str, Any]) -> List[Dict[str, Any]]:
        prompt = self._create_feature_suggestion_prompt(repo_context)
        try:
            response_text = self._run_qwen_cli(prompt)
            suggestions = self._parse_feature_suggestions(response_text)
            logger.info(f"Generated {len(suggestions)} feature suggestions (Qwen)")
            return suggestions
        except Exception as e:
            logger.error(f"Failed to generate feature suggestions (Qwen): {e}")
            return []

    def _create_feature_suggestion_prompt(self, repo_context: Dict[str, Any]) -> str:
        return render_prompt(
            "feature.suggestion",
            repo_name=repo_context.get("name", "Unknown"),
            description=repo_context.get("description", "No description"),
            language=repo_context.get("language", "Unknown"),
            recent_issues=repo_context.get("recent_issues", []),
            recent_prs=repo_context.get("recent_prs", []),
        )

    def _parse_feature_suggestions(self, response_text: str) -> List[Dict[str, Any]]:
        try:
            start_idx = response_text.find("[")
            end_idx = response_text.rfind("]") + 1
            if start_idx != -1 and end_idx != -1:
                json_str = response_text[start_idx:end_idx]
                return json.loads(json_str)
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
            command: Command to run the MCP server (e.g., 'npx', 'uv')
            args: Arguments for the command (e.g., ['-y', '@modelcontextprotocol/server-graphrag'])

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
                logger.error(
                    f"Failed to add MCP server '{server_name}': "
                    f"returncode={result.returncode}, stderr={result.stderr}"
                )
                return False
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.error(f"Failed to add Qwen MCP config: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error adding Qwen MCP config: {e}")
            return False


@dataclass
class _QwenProviderOption:
    name: str
    display_name: str
    api_key: Optional[str]
    base_url: Optional[str]
    model: Optional[str]
