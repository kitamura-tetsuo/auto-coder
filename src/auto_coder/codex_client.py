"""
Codex CLI client for Auto-Coder.
"""

import subprocess

from .logger_config import get_logger
from .exceptions import AutoCoderUsageLimitError
from .utils import CommandExecutor

logger = get_logger(__name__)


class CodexClient:
    """Codex CLI client for analyzing issues and generating solutions.

    Note: Provides a GeminiClient-compatible surface for integration.
    """

    def __init__(self, model_name: str = "codex"):
        """Initialize Codex CLI client.
        model_name is accepted for compatibility; not used by codex CLI.
        """
        self.model_name = model_name or "codex"
        self.default_model = self.model_name
        self.conflict_model = self.model_name  # codex doesn't switch models; keep same
        self.timeout = None

        # Check if codex CLI is available
        try:
            result = subprocess.run(
                ["codex", "--version"], capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                raise RuntimeError("codex CLI not available or not working")
        except Exception as e:
            raise RuntimeError(f"codex CLI not available: {e}")

    def switch_to_conflict_model(self) -> None:
        """No-op for compatibility; codex has no model switching."""
        logger.info("CodexClient: switch_to_conflict_model noop")

    def switch_to_default_model(self) -> None:
        """No-op for compatibility; codex has no model switching."""
        logger.info("CodexClient: switch_to_default_model noop")

    def _escape_prompt(self, prompt: str) -> str:
        """Escape special characters that may confuse shell/CLI.
        Keep behavior aligned with GeminiClient for consistency.
        """
        return prompt.replace('@', '\\@').strip()

    def _run_gemini_cli(self, prompt: str) -> str:
        """Run codex CLI with the given prompt and show real-time output.
        The method name matches GeminiClient for compatibility with AutomationEngine.
        """
        try:
            escaped_prompt = self._escape_prompt(prompt)
            cmd = [
                "codex", "exec",
                "-s", "workspace-write",
                "--dangerously-bypass-approvals-and-sandbox",
                escaped_prompt,
            ]

            # Warn that we are invoking an LLM (minimize calls)
            logger.warning("LLM invocation: codex CLI is being called. Keep LLM calls minimized.")
            logger.debug(f"Running codex CLI with prompt length: {len(prompt)} characters")
            logger.info("🤖 Running: codex exec -s workspace-write --dangerously-bypass-approvals-and-sandbox [prompt]")
            logger.info("=" * 60)

            usage_markers = (
                "rate limit",
                "quota",
                "429",
                "usage limit",
                "upgrade to pro",
            )

            def _on_stream(stream_name: str, chunk: str) -> None:
                low_chunk = chunk.lower()
                if any(marker in low_chunk for marker in usage_markers):
                    raise AutoCoderUsageLimitError(chunk.strip())

            result = CommandExecutor.run_command(
                cmd,
                stream_output=True,
                check_success=False,
                on_stream=_on_stream,
            )
            logger.info("=" * 60)
            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            combined_parts = [part for part in (stdout, stderr) if part]
            full_output = "\n".join(combined_parts) if combined_parts else (result.stderr or result.stdout or "")
            full_output = full_output.strip()
            low = full_output.lower()
            if result.returncode != 0:
                # Detect usage/rate limit patterns
                if any(marker in low for marker in usage_markers):
                    raise AutoCoderUsageLimitError(full_output)
                raise RuntimeError(
                    f"codex CLI failed with return code {result.returncode}\n{full_output}"
                )

            # Even with 0, some CLIs may print limit messages
            if any(marker in low for marker in usage_markers):
                raise AutoCoderUsageLimitError(full_output)
            return full_output
        except AutoCoderUsageLimitError:
            raise
        except Exception as e:
            raise RuntimeError(f"Failed to run codex CLI: {e}")


    def _run_llm_cli(self, prompt: str) -> str:
        """Neutral alias: delegate to _run_gemini_cli (migration helper)."""
        return self._run_gemini_cli(prompt)
