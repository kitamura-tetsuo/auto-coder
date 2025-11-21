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

from .backend_provider_manager import BackendProviderManager, ProviderMetadata, ProviderOutcome
from .exceptions import AutoCoderUsageLimitError
from .llm_backend_config import get_llm_config
from .llm_client_base import LLMClientBase
from .logger_config import get_logger
from .prompt_loader import render_prompt
from .utils import CommandExecutor

logger = get_logger(__name__)

_DEFAULT_CODEX_ARGS = ("exec", "-s", "workspace-write", "--dangerously-bypass-approvals-and-sandbox")
_DEFAULT_QWEN_ARGS = ("-y",)


class QwenClient(LLMClientBase):
    """Qwen Code CLI client."""

    def __init__(
        self,
        model_name: Optional[str] = None,
        openai_api_key: Optional[str] = None,
        openai_base_url: Optional[str] = None,
        use_env_vars: bool = True,
        preserve_existing_env: bool = False,
        provider_manager: Optional[BackendProviderManager] = None,
    ) -> None:
        """Initialize QwenClient."""
        config = get_llm_config()
        config_backend = config.get_backend_config("qwen")

        self.model_name = model_name or (config_backend and config_backend.model) or "qwen3-coder-plus"
        self.default_model = self.model_name
        self.conflict_model = self.model_name
        self.timeout: Optional[int] = None
        self.openai_api_key = openai_api_key or (config_backend and config_backend.openai_api_key) or os.environ.get("OPENAI_API_KEY")
        self.openai_base_url = openai_base_url or (config_backend and config_backend.openai_base_url) or os.environ.get("OPENAI_BASE_URL")
        self.use_env_vars = use_env_vars
        self.preserve_existing_env = preserve_existing_env
        self.backend_name = "qwen"
        self._provider_manager = provider_manager or BackendProviderManager.get_default_manager()
        self._fallback_providers = self._build_fallback_providers()
        self._last_used_model: Optional[str] = self.model_name

        available_providers = self._provider_manager.get_provider_chain(self.backend_name, self._fallback_providers)
        if not available_providers:
            raise RuntimeError("No Qwen providers are configured")

        needs_codex = any(self._resolve_invocation(p) == "codex" for p in available_providers)
        needs_qwen = any(self._resolve_invocation(p) == "qwen" for p in available_providers)

        if needs_codex:
            self._ensure_cli_available("codex")
        if needs_qwen:
            self._ensure_cli_available("qwen")

    # ----- Model switching (keep simple; Qwen may not need to switch models) -----
    def switch_to_conflict_model(self) -> None:
        self.model_name = self.conflict_model
        logger.debug("QwenClient.switch_to_conflict_model: active model -> %s", self.model_name)

    def switch_to_default_model(self) -> None:
        self.model_name = self.default_model
        logger.debug("QwenClient.switch_to_default_model: active model -> %s", self.model_name)

    # ----- Helpers -----
    def _escape_prompt(self, prompt: str) -> str:
        return prompt.strip()

    def _ensure_cli_available(self, cli_name: str) -> None:
        try:
            result = subprocess.run([cli_name, "--version"], capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                raise RuntimeError(f"{cli_name} CLI not available or not working")
        except Exception as exc:  # pragma: no cover - defensive
            raise RuntimeError(f"{cli_name} CLI not available: {exc}")

    # ----- Core execution -----
    def _run_qwen_cli(self, prompt: str) -> str:
        escaped_prompt = self._escape_prompt(prompt)
        choices = self._provider_manager.iterate_provider_choices(self.backend_name, self._fallback_providers)
        if not choices:
            raise RuntimeError("No Qwen providers are configured")

        usage_errors: List[str] = []

        for choice in choices:
            provider = choice.provider
            display_name = self._provider_display_name(provider)
            try:
                output = self._execute_with_provider(provider, escaped_prompt, prompt)
                self._provider_manager.report_provider_result(self.backend_name, choice, ProviderOutcome.SUCCESS)
                return output
            except AutoCoderUsageLimitError as exc:
                usage_errors.append(f"{display_name}: {str(exc).strip()}")
                logger.warning("Qwen provider '%s' hit usage limit. Trying next provider.", display_name)
                self._provider_manager.report_provider_result(self.backend_name, choice, ProviderOutcome.USAGE_LIMIT)
            except Exception:
                self._provider_manager.report_provider_result(self.backend_name, choice, ProviderOutcome.FAILURE)
                raise

        if usage_errors:
            aggregated = " | ".join(usage_errors)
            raise AutoCoderUsageLimitError(f"All Qwen providers reached usage limits: {aggregated}")

        raise RuntimeError("Qwen providers exhausted without usable response")

    def _run_llm_cli(self, prompt: str) -> str:
        """Compatibility wrapper for base class."""
        return self._run_qwen_cli(prompt)

    def _execute_with_provider(
        self,
        provider: ProviderMetadata,
        escaped_prompt: str,
        original_prompt: str,
    ) -> str:
        env = self._build_env(provider)
        model_to_use = self._resolve_provider_model(provider) or self.default_model
        invocation = self._resolve_invocation(provider)

        if invocation == "codex":
            cmd, display_cmd = self._build_codex_command(provider, model_to_use, escaped_prompt, env)
        elif invocation == "qwen":
            cmd, display_cmd = self._build_qwen_command(provider, model_to_use, escaped_prompt, env)
        else:
            raise RuntimeError(f"Unsupported Qwen provider command '{invocation}'")

        cli_name = cmd[0]

        logger.warning("LLM invocation: %s CLI is being called. Keep LLM calls minimized.", cli_name)
        logger.debug("Running %s CLI with prompt length: %d characters", cli_name, len(original_prompt))
        logger.info("🤖 Running (%s): %s", self._provider_display_name(provider), display_cmd)
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

        if model_to_use:
            self.model_name = model_to_use
            self._last_used_model = model_to_use

        return full_output

    @staticmethod
    def _is_usage_limit(message: str, returncode: int) -> bool:
        low = message.lower()
        if "rate limit" in low or "quota" in low:
            return True
        if returncode != 0 and ("429" in low or "too many requests" in low):
            return True
        if "error: 400 model access denied." in low:
            return True
        if "openai api streaming error: 429 free allocated quota exceeded." in low:
            return True
        if "openai api streaming error: 429 provider returned error" in low:
            return True
        return False

    def _build_fallback_providers(self) -> List[ProviderMetadata]:
        providers: List[ProviderMetadata] = []
        if self.openai_api_key and self.openai_base_url:
            providers.append(
                ProviderMetadata(
                    name="custom-openai",
                    command="codex",
                    args=list(_DEFAULT_CODEX_ARGS),
                    description="Custom OpenAI-compatible",
                    uppercase_settings={
                        "OPENAI_API_KEY": self.openai_api_key,
                        "OPENAI_BASE_URL": self.openai_base_url,
                        "OPENAI_MODEL": self.model_name,
                    },
                )
            )

        providers.append(
            ProviderMetadata(
                name="qwen-oauth",
                command="qwen",
                args=list(_DEFAULT_QWEN_ARGS),
                description="Qwen OAuth",
                uppercase_settings={},
            )
        )
        return providers

    def _build_env(self, provider: ProviderMetadata) -> Dict[str, str]:
        env = os.environ.copy()
        if not self.preserve_existing_env:
            for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
                env.pop(key, None)
        for key, value in provider.uppercase_settings.items():
            env[key] = value
        return env

    def _provider_display_name(self, provider: ProviderMetadata) -> str:
        return provider.description or provider.name

    def _resolve_invocation(self, provider: ProviderMetadata) -> str:
        command = (provider.command or "").strip().lower()
        if command in {"codex", "qwen"}:
            return command
        settings = provider.uppercase_settings
        if settings.get("OPENAI_API_KEY") or settings.get("OPENAI_BASE_URL"):
            return "codex"
        return "qwen"

    def _resolve_command_name(self, provider: ProviderMetadata, default: str) -> str:
        command = (provider.command or "").strip()
        return command or default

    def _resolve_provider_model(self, provider: ProviderMetadata) -> Optional[str]:
        settings = provider.uppercase_settings
        return settings.get("OPENAI_MODEL") or settings.get("MODEL") or settings.get("QWEN_MODEL")

    def _resolve_model_provider(self, provider: ProviderMetadata) -> str:
        return provider.uppercase_settings.get("MODEL_PROVIDER") or provider.name.lower()

    def _collect_credentials(self, provider: ProviderMetadata) -> Dict[str, Optional[str]]:
        settings = provider.uppercase_settings
        return {
            "api_key": settings.get("OPENAI_API_KEY"),
            "base_url": settings.get("OPENAI_BASE_URL"),
        }

    def _build_codex_command(
        self,
        provider: ProviderMetadata,
        model_to_use: Optional[str],
        escaped_prompt: str,
        env: Dict[str, str],
    ) -> tuple[List[str], str]:
        base_args = list(provider.args) if provider.args else list(_DEFAULT_CODEX_ARGS)
        cmd = [self._resolve_command_name(provider, "codex"), *base_args]
        model_provider = self._resolve_model_provider(provider)

        if model_provider and model_provider != "qwen-oauth":
            cmd.extend(["-c", f'model_provider="{model_provider}"'])
        if model_to_use:
            cmd.extend(["-c", f'model="{model_to_use}"'])

        credentials = self._collect_credentials(provider)
        if credentials["api_key"]:
            env["OPENAI_API_KEY"] = credentials["api_key"]
        if credentials["base_url"]:
            env["OPENAI_BASE_URL"] = credentials["base_url"]

        cmd.append(escaped_prompt)
        display_cmd = f'{cmd[0]} {" ".join(cmd[1:-1])} [prompt]' if len(cmd) > 1 else f"{cmd[0]} [prompt]"
        return cmd, display_cmd

    def _build_qwen_command(
        self,
        provider: ProviderMetadata,
        model_to_use: Optional[str],
        escaped_prompt: str,
        env: Dict[str, str],
    ) -> tuple[List[str], str]:
        base_args = list(provider.args) if provider.args else list(_DEFAULT_QWEN_ARGS)
        cmd = [self._resolve_command_name(provider, "qwen"), *base_args]
        credentials = self._collect_credentials(provider)

        if self.use_env_vars:
            if credentials["api_key"]:
                env["OPENAI_API_KEY"] = credentials["api_key"]
            if credentials["base_url"]:
                env["OPENAI_BASE_URL"] = credentials["base_url"]
            if model_to_use:
                env["OPENAI_MODEL"] = model_to_use
        else:
            if credentials["api_key"]:
                cmd.extend(["--openai-api-key", credentials["api_key"]])
                env.pop("OPENAI_API_KEY", None)
            if credentials["base_url"]:
                cmd.extend(["--openai-base-url", credentials["base_url"]])
                env.pop("OPENAI_BASE_URL", None)
            env.pop("OPENAI_MODEL", None)

        if model_to_use and "-m" not in cmd:
            cmd.extend(["-m", model_to_use])

        cmd.extend(["-p", escaped_prompt])
        display_cmd = f"{cmd[0]} {' '.join(arg for arg in cmd[1:-1])} [prompt]" if len(cmd) > 1 else f"{cmd[0]} [prompt]"
        return cmd, display_cmd

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
                return list(json.loads(json_str))
            return []
        except json.JSONDecodeError:
            return []

    # ----- MCP helpers -----
    def check_mcp_server_configured(self, server_name: str) -> bool:
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
        try:
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

            if "already" in result.stderr.lower() or "exists" in result.stderr.lower():
                logger.info(f"MCP server '{server_name}' already configured in Qwen")
                return True
            logger.error(f"Failed to add MCP server '{server_name}': returncode={result.returncode}, stderr={result.stderr}")
            return False
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.error(f"Failed to add Qwen MCP config: {e}")
            return False
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error(f"Unexpected error adding Qwen MCP config: {e}")
            return False
