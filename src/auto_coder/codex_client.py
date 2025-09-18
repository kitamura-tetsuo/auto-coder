"""
Codex CLI client for Auto-Coder.
"""

import subprocess
from typing import Dict, Any, List

from .logger_config import get_logger

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

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            output_lines: List[str] = []
            assert process.stdout is not None
            for line in process.stdout:
                line = line.rstrip('\n')
                logger.info(line)
                output_lines.append(line)

            return_code = process.wait()
            logger.info("=" * 60)
            if return_code != 0:
                raise RuntimeError(f"codex CLI failed with return code {return_code}")

            return "\n".join(output_lines).strip()
        except Exception as e:
            raise RuntimeError(f"Failed to run codex CLI: {e}")


    def _run_llm_cli(self, prompt: str) -> str:
        """Neutral alias: delegate to _run_gemini_cli (migration helper)."""
        return self._run_gemini_cli(prompt)
