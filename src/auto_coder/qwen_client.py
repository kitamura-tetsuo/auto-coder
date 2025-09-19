"""
Qwen Code CLI client for Auto-Coder.

Design: mirror GeminiClient/CodexClient public surface so AutomationEngine can use it transparently.
- _run_gemini_cli(prompt: str) -> str
- switch_to_conflict_model() / switch_to_default_model()
- suggest_features(repo_context)
"""
from __future__ import annotations

import os
import json
import subprocess
from typing import Any, Dict, List, Optional

from .logger_config import get_logger

logger = get_logger(__name__)


class QwenClient:
    """Qwen Code CLI client.

    Note: Qwen Code is adapted from Gemini CLI. We assume a similar non-interactive CLI interface.
    Tests mock subprocess, so no external dependency is required to run tests.
    """

    def __init__(self, model_name: str = "qwen3-coder-plus", openai_api_key: Optional[str] = None, openai_base_url: Optional[str] = None):
        self.model_name = model_name or "qwen3-coder-plus"
        self.default_model = self.model_name
        # Use a faster/cheaper coder variant for conflict resolution when switching
        self.conflict_model = self.model_name
        self.timeout: Optional[int] = None
        # OpenAI-compatible env overrides (Qwen backend only)
        self.openai_api_key = openai_api_key
        self.openai_base_url = openai_base_url

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
        logger.debug("QwenClient.switch_to_conflict_model: no-op (using same model)")

    def switch_to_default_model(self) -> None:
        # No-op; self.model_name already default unless we implement dynamic switching
        logger.debug("QwenClient.switch_to_default_model: no-op (using same model)")

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

        # Prefer explicit -m/--model if available; also set OPENAI_MODEL for OpenAI-compatible providers
        env = os.environ.copy()
        if self.model_name:
            env.setdefault("OPENAI_MODEL", self.model_name)
        if self.openai_api_key:
            env["OPENAI_API_KEY"] = self.openai_api_key
        if self.openai_base_url:
            env["OPENAI_BASE_URL"] = self.openai_base_url

        # Non-interactive execution: use -p/--prompt
        # If a model is provided, pass it explicitly via -m for Qwen OAuth mode; env OPENAI_MODEL works for OpenAI-compatible mode
        if self.model_name:
            cmd = [
                "qwen",
                "-y",
                "-m",
                self.model_name,
                "-p",
                escaped_prompt,
            ]
        else:
            cmd = [
                "qwen",
                "-y",
                "-p",
                escaped_prompt,
            ]

        logger.warning("LLM invocation: qwen CLI is being called. Keep LLM calls minimized.")
        logger.debug(f"Running qwen CLI with prompt length: {len(prompt)} characters")
        if self.model_name:
            logger.info("🤖 Running: qwen -m %s -p [prompt]" % self.model_name)
        else:
            logger.info("🤖 Running: qwen -p [prompt]")
        logger.info("=" * 60)

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
            env=env,
        )

        output_lines: List[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip("\n")
            if len(line) == 0:
                continue
            logger.info(line)
            output_lines.append(line)

        return_code = process.wait()
        logger.info("=" * 60)
        if return_code != 0:
            raise RuntimeError(f"qwen CLI failed with return code {return_code}")

        return "\n".join(output_lines)


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
        return f"""
Based on the following repository context, suggest new features that would be valuable:

Repository: {repo_context.get('name', 'Unknown')}
Description: {repo_context.get('description', 'No description')}
Language: {repo_context.get('language', 'Unknown')}
Recent Issues: {repo_context.get('recent_issues', [])}
Recent PRs: {repo_context.get('recent_prs', [])}

Please provide feature suggestions in the following JSON format:
[
    {{
        "title": "Feature title",
        "description": "Detailed description of the feature",
        "rationale": "Why this feature would be valuable",
        "priority": "low|medium|high",
        "complexity": "simple|moderate|complex",
        "estimated_effort": "hours|days|weeks",
        "labels": ["enhancement", "feature"],
        "acceptance_criteria": [
            "criteria 1",
            "criteria 2"
        ]
    }}
]
"""

    def _parse_feature_suggestions(self, response_text: str) -> List[Dict[str, Any]]:
        try:
            start_idx = response_text.find('[')
            end_idx = response_text.rfind(']') + 1
            if start_idx != -1 and end_idx != -1:
                json_str = response_text[start_idx:end_idx]
                return json.loads(json_str)
            return []
        except json.JSONDecodeError:
            return []

