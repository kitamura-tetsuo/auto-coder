"""
Gemini CLI client for Auto-Coder.
"""

import json
import os
import subprocess
from typing import Any, Dict, List, Optional

# genai is mocked in tests. Ensure the attribute is available even if the package is not installed at runtime
try:
    import google.generativeai as genai  # type: ignore
except Exception:  # Avoid runtime dependency
    genai = None  # Replaced via patch in tests

from .exceptions import AutoCoderUsageLimitError
from .llm_backend_config import get_llm_config
from .llm_client_base import LLMClientBase
from .logger_config import get_logger
from .prompt_loader import render_prompt
from .utils import CommandExecutor

logger = get_logger(__name__)


class GeminiClient(LLMClientBase):
    """Gemini client that uses google.generativeai SDK primarily in tests and a CLI fallback."""

    def __init__(self, api_key: Optional[str] = None, model_name: Optional[str] = None) -> None:
        """Initialize Gemini client.

        In tests, genai is patched and used. In production, we still verify gemini CLI presence
        for the CLI-based paths used elsewhere in the tool.

        Args:
            api_key: API key (optional, will be loaded from config if not provided)
            model_name: Model name (optional, will be loaded from config if not provided)
        """
        config = get_llm_config()
        config_backend = config.get_backend_config("gemini")

        # Use provided values, fall back to config, then to default
        self.api_key = api_key or (config_backend and config_backend.api_key) or os.environ.get("GEMINI_API_KEY")
        self.model_name = model_name or (config_backend and config_backend.model) or "gemini-2.5-pro"
        self.default_model = self.model_name
        self.conflict_model = "gemini-2.5-flash"  # Faster model for conflict resolution
        self.timeout = None  # No timeout - let gemini CLI run as long as needed

        # Configure genai if available (tests patch this symbol)
        if genai is not None and self.api_key:
            try:
                genai.configure(api_key=self.api_key)
                self.model = genai.GenerativeModel(self.model_name)
            except Exception:
                # Fall back silently for tests that don't rely on real SDK
                self.model = None
        else:
            self.model = None

        # Check if gemini CLI is available for CLI-based flows
        try:
            result = subprocess.run(["gemini", "--version"], capture_output=True, text=True, timeout=10)
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
            logger.info(f"Switching from {self.model_name} to {self.conflict_model} for conflict resolution")
            self.model_name = self.conflict_model

    def switch_to_default_model(self) -> None:
        """Switch back to default model."""
        if self.model_name != self.default_model:
            logger.info(f"Switching back from {self.model_name} to {self.default_model}")
            self.model_name = self.default_model

    def _escape_prompt(self, prompt: str) -> str:
        """Escape @ characters in prompt for Gemini."""
        return prompt.replace("@", "\\@").strip()

    def _run_llm_cli(self, prompt: str) -> str:
        """Run gemini CLI with the given prompt and show real-time output."""
        try:
            escaped_prompt = self._escape_prompt(prompt)

            cmd = [
                "gemini",
                "--yolo",
                "--model",
                self.model_name,
                "--force-model",
                "--prompt",
                escaped_prompt,
            ]

            logger.warning("LLM invocation: gemini CLI is being called. Keep LLM calls minimized.")
            logger.debug(f"Running gemini CLI with prompt length: {len(prompt)} characters")
            logger.info(f"🤖 Running: gemini --model {self.model_name} --force-model --prompt [prompt]")
            logger.info("=" * 60)

            result = CommandExecutor.run_command(
                cmd,
                stream_output=True,
            )

            logger.info("=" * 60)

            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            combined_parts = [part for part in (stdout, stderr) if part]
            full_output = "\n".join(combined_parts) if combined_parts else (result.stderr or result.stdout or "")
            full_output = full_output.strip()
            low = full_output.lower()

            usage_markers = ("[API Error: You have exhausted your capacity on this model. Your quota will reset after ".lower(),)

            if result.returncode != 0:
                if any(m in low for m in usage_markers):
                    raise AutoCoderUsageLimitError(full_output)
                raise RuntimeError(f"Gemini CLI failed with return code {result.returncode}\n{full_output}")

            return full_output

        except AutoCoderUsageLimitError:
            raise
        except Exception as e:
            if "timed out" not in str(e):
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
                response_text = self._run_llm_cli(prompt)
                suggestions = self._parse_feature_suggestions(response_text)
            logger.info(f"Generated {len(suggestions)} feature suggestions")
            return suggestions

        except Exception as e:
            logger.error(f"Failed to generate feature suggestions: {e}")
            return []

    def _create_pr_analysis_prompt(self, pr_data: Dict[str, Any]) -> str:
        """Create prompt for pull request analysis."""
        result: str = render_prompt(
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
        return result

    def _create_feature_suggestion_prompt(self, repo_context: Dict[str, Any]) -> str:
        """Create prompt for feature suggestions."""
        result: str = render_prompt(
            "feature.suggestion",
            repo_name=repo_context.get("name", "Unknown"),
            description=repo_context.get("description", "No description"),
            language=repo_context.get("language", "Unknown"),
            recent_issues=repo_context.get("recent_issues", []),
            recent_prs=repo_context.get("recent_prs", []),
        )
        return result

    # SDK-based analysis helpers removed per LLM execution policy.
    # analyze_issue / analyze_pull_request / generate_solution are intentionally removed.
    # The system must not perform analysis-only LLM calls. Single-run direct actions are used instead.

    # ===== Parsing helpers expected by tests =====
    def _parse_analysis_response(self, response_text: str) -> Dict[str, Any]:
        try:
            # Extract first JSON object in text
            start = response_text.find("{")
            end = response_text.rfind("}") + 1
            if start != -1 and end != -1:
                parsed = json.loads(response_text[start:end])
                result: Dict[str, Any] = parsed if isinstance(parsed, dict) else {}
                return result
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
            parsed = json.loads(response_text)
            result: Dict[str, Any] = (
                parsed
                if isinstance(parsed, dict)
                else {
                    "solution_type": "investigation",
                    "summary": f"Invalid JSON: {response_text[:200]}",
                    "steps": [],
                    "code_changes": [],
                }
            )
            return result
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
                parsed = json.loads(json_str)
                result: List[Dict[str, Any]] = parsed if isinstance(parsed, list) else []
                return result
            else:
                return []
        except json.JSONDecodeError:
            return []

    def check_mcp_server_configured(self, server_name: str) -> bool:
        """Check if a specific MCP server is configured for Gemini CLI.

        Args:
            server_name: Name of the MCP server to check (e.g., 'graphrag', 'mcp-pdb')

        Returns:
            True if the MCP server is configured, False otherwise
        """
        try:
            result = subprocess.run(
                ["gemini", "mcp", "list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                output = result.stdout.lower()
                if server_name.lower() in output:
                    logger.info(f"Found MCP server '{server_name}' via 'gemini mcp list'")
                    return True
                logger.debug(f"MCP server '{server_name}' not found via 'gemini mcp list'")
                return False
            else:
                logger.debug(f"'gemini mcp list' command failed with return code {result.returncode}")
                return False
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.debug(f"Failed to check Gemini MCP config: {e}")
            return False

    def add_mcp_server_config(self, server_name: str, command: str, args: list[str]) -> bool:
        """Add MCP server configuration to Gemini CLI config.

        Args:
            server_name: Name of the MCP server (e.g., 'graphrag', 'mcp-pdb')
            command: Command to run the MCP server (e.g., 'uv', '/path/to/script.sh')
            args: Arguments for the command (e.g., ['run', 'main.py'] or [])

        Returns:
            True if configuration was added successfully, False otherwise
        """
        try:
            # Use gemini mcp add command
            # Format: gemini mcp add <name> <command> [args...]
            cmd = ["gemini", "mcp", "add", server_name, command] + args

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0:
                logger.info(f"Added MCP server '{server_name}' to Gemini config")
                return True
            else:
                logger.error(f"Failed to add Gemini MCP config: {result.stderr}")
                return False
        except Exception as e:
            logger.error(f"Failed to add Gemini MCP config: {e}")
            return False
