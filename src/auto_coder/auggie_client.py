"""
Auggie CLI client for Auto-Coder.

Design: mimic GeminiClient/CodexClient interface so the automation engine can
swap backends transparently. The Auggie CLI is assumed to be installed via
`npm install -g @augmentcode/auggie` and supports non-interactive invocation
using `--print` together with `--model` and the prompt as a positional
argument.
"""
from __future__ import annotations

import subprocess
from typing import List

from .logger_config import get_logger
from .exceptions import AutoCoderUsageLimitError

logger = get_logger(__name__)


class AuggieClient:
    """Auggie CLI client wrapper."""

    def __init__(self, model_name: str = "GPT-5") -> None:
        self.model_name = model_name or "GPT-5"
        self.default_model = self.model_name
        self.conflict_model = self.model_name
        self.timeout = None

        # Verify Auggie CLI availability early for deterministic failures.
        try:
            result = subprocess.run(
                ["auggie", "--version"], capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                raise RuntimeError("auggie CLI not available or not working")
        except Exception as exc:  # pragma: no cover - defensive; raised in init
            raise RuntimeError(f"auggie CLI not available: {exc}")

    def switch_to_conflict_model(self) -> None:
        """No-op placeholder for compatibility with BackendManager."""
        logger.debug("AuggieClient.switch_to_conflict_model: no-op (single model)")

    def switch_to_default_model(self) -> None:
        """No-op placeholder for compatibility with BackendManager."""
        logger.debug("AuggieClient.switch_to_default_model: no-op (single model)")

    def _escape_prompt(self, prompt: str) -> str:
        """Escape characters that can confuse shell commands."""
        return prompt.replace('@', '\\@').strip()

    def _run_auggie_cli(self, prompt: str) -> str:
        """Execute Auggie CLI and stream output via logger."""
        escaped_prompt = self._escape_prompt(prompt)
        cmd = [
            "auggie",
            "--print",
            "--model",
            self.model_name,
            escaped_prompt,
        ]

        logger.warning("LLM invocation: auggie CLI is being called. Keep LLM calls minimized.")
        logger.debug(f"Running auggie CLI with prompt length: {len(prompt)} characters")
        logger.info("🤖 Running: auggie --print --model %s [prompt]" % self.model_name)
        logger.info("=" * 60)

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
        )

        output_lines: List[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            logger.info(line)
            output_lines.append(line)

        return_code = process.wait()
        logger.info("=" * 60)
        full_output = "\n".join(output_lines).strip()
        low = full_output.lower()
        if return_code != 0:
            if ("rate limit" in low) or ("quota" in low) or ("429" in low):
                raise AutoCoderUsageLimitError(full_output)
            raise RuntimeError(f"auggie CLI failed with return code {return_code}\n{full_output}")
        if ("rate limit" in low) or ("quota" in low):
            raise AutoCoderUsageLimitError(full_output)
        return full_output

    def _run_gemini_cli(self, prompt: str) -> str:
        """Compatibility shim for legacy call sites."""
        return self._run_auggie_cli(prompt)

    def _run_llm_cli(self, prompt: str) -> str:
        """BackendManager entry-point."""
        return self._run_auggie_cli(prompt)

