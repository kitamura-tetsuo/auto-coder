"""
Gemini CLI client for Auto-Coder.
"""

import json
import subprocess
from typing import Any, Dict, List, Optional

# genai はテストでモックされる。実環境で未インストールでも属性が存在するようにする
try:
    import google.generativeai as genai  # type: ignore
except Exception:  # ランタイム依存を避けるため
    genai = None  # テストでは patch により置き換えられる

from .exceptions import AutoCoderUsageLimitError
from .llm_client_base import LLMClientBase
from .logger_config import get_logger
from .prompt_loader import render_prompt
from .utils import CommandExecutor

logger = get_logger(__name__)


class GeminiClient(LLMClientBase):
    """Gemini client that uses google.generativeai SDK primarily in tests and a CLI fallback."""

    def __init__(
        self, api_key: Optional[str] = None, model_name: str = "gemini-2.5-pro"
    ):
        """Initialize Gemini client.

        In tests, genai is patched and used. In production, we still verify gemini CLI presence
        for the CLI-based paths used elsewhere in the tool.
        """
        # Allow single positional argument to be treated as model_name when it looks like a model id
        if (
            api_key
            and isinstance(api_key, str)
            and api_key.lower().startswith("gemini-")
            and model_name == "gemini-2.5-pro"
        ):
            model_name, api_key = api_key, None

        self.api_key = api_key
        self.model_name = model_name
        self.default_model = model_name
        self.conflict_model = "gemini-2.5-flash"  # Faster model for conflict resolution
        self.timeout = None  # No timeout - let gemini CLI run as long as needed

        # Configure genai if available (tests patch this symbol)
        if genai is not None and api_key:
            try:
                genai.configure(api_key=api_key)
                self.model = genai.GenerativeModel(model_name)
            except Exception:
                # Fall back silently for tests that don't rely on real SDK
                self.model = None
        else:
            self.model = None

        # Check if gemini CLI is available for CLI-based flows
        try:
            result = subprocess.run(
                ["gemini", "--version"], capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                raise RuntimeError("Gemini CLI not available or not working")
        except (
            subprocess.TimeoutExpired,
            subprocess.CalledProcessError,
            FileNotFoundError,
        ) as e:
            raise RuntimeError(f"Gemini CLI not available: {e}")

    def switch_to_conflict_model(self) -> None:
        """Switch to faster model for conflict resolution."""
        if self.model_name != self.conflict_model:
            logger.info(
                f"Switching from {self.model_name} to {self.conflict_model} for conflict resolution"
            )
            self.model_name = self.conflict_model

    def switch_to_default_model(self) -> None:
        """Switch back to default model."""
        if self.model_name != self.default_model:
            logger.info(
                f"Switching back from {self.model_name} to {self.default_model}"
            )
            self.model_name = self.default_model

    def _escape_prompt(self, prompt: str) -> str:
        """Escape @ characters in prompt for Gemini."""
        return prompt.replace("@", "\\@").strip()

    def _run_gemini_cli(self, prompt: str) -> str:
        """Run gemini CLI with the given prompt and show real-time output."""
        try:
            # Escape @ characters in prompt for Gemini
            escaped_prompt = self._escape_prompt(prompt)

            # Run gemini CLI with prompt via stdin and additional prompt parameter
            cmd = [
                "gemini",
                "--yolo",
                "--model",
                self.model_name,
                "--force-model",
                "--prompt",
                escaped_prompt,
            ]

            # Warn that we are invoking an LLM (keep calls minimized)
            logger.warning(
                "LLM invocation: gemini CLI is being called. Keep LLM calls minimized."
            )
            logger.debug(
                f"Running gemini CLI with prompt length: {len(prompt)} characters"
            )
            logger.info(
                f"🤖 Running: gemini --model {self.model_name} --force-model --prompt [prompt]"
            )
            logger.info("=" * 60)

            # Streaming-time usage limit detection via callback
            usage_markers = (
                "rate limit",
                "quota",
                "429",
                "resource_exhausted",
                "too many requests",
            )

            def _on_stream(stream_name: str, chunk: str) -> None:
                low_chunk = chunk.lower()
                if any(m in low_chunk for m in usage_markers):
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
            full_output = (
                "\n".join(combined_parts)
                if combined_parts
                else (result.stderr or result.stdout or "")
            )
            full_output = full_output.strip()
            low = full_output.lower()

            # Detect usage/rate limit conditions
            usage_markers = (
                "rate limit",
                "quota",
                "429",
                "resource_exhausted",
                "too many requests",
            )

            if result.returncode != 0:
                if any(m in low for m in usage_markers):
                    raise AutoCoderUsageLimitError(full_output)
                raise RuntimeError(
                    f"Gemini CLI failed with return code {result.returncode}\n{full_output}"
                )

            # Even with 0, detect soft limit messages (some CLIs log 429 but exit 0)
            if any(m in low for m in usage_markers):
                raise AutoCoderUsageLimitError(full_output)
            return full_output

        except AutoCoderUsageLimitError:
            raise
        except Exception as e:
            if "timed out" not in str(e):  # Don't mention timeout since we removed it
                raise RuntimeError(f"Failed to run Gemini CLI: {e}")
            raise

    def suggest_features(self, repo_context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Suggest new features based on repository analysis."""
        prompt = self._create_feature_suggestion_prompt(repo_context)

        try:
            # Prefer SDK path when model is available (tests patch genai)
            if getattr(self, "model", None) is not None:
                resp = self.model.generate_content(prompt)
                text = getattr(resp, "text", "")
                suggestions = self._parse_feature_suggestions(text)
            else:
                response_text = self._run_gemini_cli(prompt)
                suggestions = self._parse_feature_suggestions(response_text)
            logger.info(f"Generated {len(suggestions)} feature suggestions")
            return suggestions

        except Exception as e:
            logger.error(f"Failed to generate feature suggestions: {e}")
            return []

    def _create_pr_analysis_prompt(self, pr_data: Dict[str, Any]) -> str:
        """Create prompt for pull request analysis."""
        return render_prompt(
            "gemini.pr_analysis",
            title=pr_data.get("title", ""),
            body=pr_data.get("body", ""),
            labels=", ".join(pr_data.get("labels", [])),
            head_branch=pr_data.get("head_branch", ""),
            base_branch=pr_data.get("base_branch", ""),
            additions=pr_data.get("additions", 0),
            deletions=pr_data.get("deletions", 0),
            changed_files=pr_data.get("changed_files", 0),
            draft=pr_data.get("draft", False),
            mergeable=pr_data.get("mergeable", False),
        )

    def _create_feature_suggestion_prompt(self, repo_context: Dict[str, Any]) -> str:
        """Create prompt for feature suggestions."""
        return render_prompt(
            "feature.suggestion",
            repo_name=repo_context.get("name", "Unknown"),
            description=repo_context.get("description", "No description"),
            language=repo_context.get("language", "Unknown"),
            recent_issues=repo_context.get("recent_issues", []),
            recent_prs=repo_context.get("recent_prs", []),
        )

    # ===== SDK-based analysis helpers (disabled per LLM execution policy) =====
    # analyze_issue / analyze_pull_request / generate_solution are intentionally removed.
    # The system must not perform analysis-only LLM calls. Single-run direct actions are used instead.

    # ===== Parsing helpers expected by tests =====
    def _parse_analysis_response(self, response_text: str) -> Dict[str, Any]:
        try:
            # Extract first JSON object in text
            start = response_text.find("{")
            end = response_text.rfind("}") + 1
            if start != -1 and end != -1:
                return json.loads(response_text[start:end])
        except Exception:
            pass
        # Fallback default per tests expectations
        return {
            "category": "unknown",
            "priority": "medium",
            "summary": response_text[:200],
        }

    def _parse_solution_response(self, response_text: str) -> Dict[str, Any]:
        try:
            return json.loads(response_text)
        except Exception:
            return {
                "solution_type": "investigation",
                "summary": f"Invalid JSON: {response_text[:200]}",
                "steps": [],
                "code_changes": [],
            }

    def _parse_feature_suggestions(self, response_text: str) -> List[Dict[str, Any]]:
        """Parse feature suggestions from Gemini."""
        try:
            # Try to extract JSON array from the response
            start_idx = response_text.find("[")
            end_idx = response_text.rfind("]") + 1

            if start_idx != -1 and end_idx != -1:
                json_str = response_text[start_idx:end_idx]
                return json.loads(json_str)
            else:
                return []
        except json.JSONDecodeError:
            return []

    def _run_llm_cli(self, prompt: str) -> str:
        """Neutral alias: delegate to _run_gemini_cli (migration helper)."""
        return self._run_gemini_cli(prompt)
