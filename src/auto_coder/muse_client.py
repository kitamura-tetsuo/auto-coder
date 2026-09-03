import os
import shlex
import subprocess
from pathlib import Path
from typing import List, Optional

from .exceptions import AutoCoderTimeoutError, AutoCoderUsageLimitError
from .git_utils import get_current_branch
from .llm_client_base import LLMClientBase
from .logger_config import get_logger
from .usage_marker_utils import has_usage_marker_match
from .utils import CommandExecutor

logger = get_logger(__name__)


class MuseClient(LLMClientBase):
    """Muse Code CLI client for analyzing issues and generating solutions."""

    def __init__(self, backend_name: Optional[str] = None):
        """Initialize the MuseClient."""
        super().__init__()
        self.backend_name = backend_name
        self.model_name = "muse"
        self.options = []
        self.options_for_noedit = []
        self.usage_markers = []

        # Load from config if available
        from .llm_backend_config import get_llm_config

        config = get_llm_config()
        self.config_backend = None
        if config and backend_name:
            self.config_backend = config.get_backend_config(backend_name)
            if self.config_backend:
                self.model_name = self.config_backend.model or "muse"
                self.options = self.config_backend.options or []
                self.options_for_noedit = self.config_backend.options_for_noedit or []
                self.usage_markers = self.config_backend.usage_markers or []

    def _escape_prompt(self, prompt: str) -> str:
        """Escape characters that can confuse shell commands."""
        return prompt.replace("@", "\\@").strip()

    def _run_muse_cli(self, prompt: str, is_noedit: bool = False) -> str:
        """Execute Muse CLI and stream output via logger."""
        escaped_prompt = self._escape_prompt(prompt)

        override = os.environ.get("AUTOCODER_MUSE_CLI")
        cmd = shlex.split(override) if override else ["muse"]

        cmd.append("exec")

        if self.config_backend:
            processed_options = self.config_backend.replace_placeholders(model_name=self.model_name)
            if is_noedit and self.options_for_noedit:
                options_to_use = processed_options.get("options_for_noedit", [])
            else:
                options_to_use = processed_options.get("options", [])
        else:
            options_to_use = self.options_for_noedit if is_noedit and self.options_for_noedit else self.options

        if options_to_use:
            cmd.extend(options_to_use)

        extra_args = self.consume_extra_args()
        if extra_args:
            cmd.extend(extra_args)

        cmd.append(escaped_prompt)

        logger.warning("LLM invocation: muse CLI is being called. Keep LLM calls minimized.")
        logger.debug(f"Running muse CLI with prompt length: {len(prompt)} characters")
        logger.info(f"🤖 Running: muse exec [prompt]")
        logger.info("=" * 60)

        # Snapshot Git State
        pre_run_branch = get_current_branch()
        pre_run_head = CommandExecutor().run_command(["git", "rev-parse", "HEAD"]).stdout.strip()

        try:
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

            return_code = process.wait(timeout=7200)  # 2 hour timeout
            logger.info("=" * 60)
            full_output = "\n".join(output_lines).strip()

            usage_markers = self.usage_markers or ["rate limit", "quota", "429"]
            usage_limit_detected = has_usage_marker_match(full_output, usage_markers)

            # Revert Git state if it was mutated
            post_run_branch = get_current_branch()
            post_run_head = CommandExecutor().run_command(["git", "rev-parse", "HEAD"]).stdout.strip()

            git_mutated = post_run_branch != pre_run_branch or post_run_head != pre_run_head

            if is_noedit:
                CommandExecutor().run_command(["git", "reset", "--hard", pre_run_head])
                CommandExecutor().run_command(["git", "clean", "-fd"])
                if post_run_branch != pre_run_branch:
                    CommandExecutor().run_command(["git", "checkout", pre_run_branch])
            elif git_mutated:
                logger.error(f"Muse CLI execution violated Git-state invariants. Branch changed from {pre_run_branch} to {post_run_branch} or HEAD changed from {pre_run_head} to {post_run_head}")
                CommandExecutor().run_command(["git", "reset", "--hard", pre_run_head])
                if post_run_branch != pre_run_branch:
                    CommandExecutor().run_command(["git", "checkout", pre_run_branch])

                raise RuntimeError(f"Muse CLI violated Git invariants. Restored pre-run branch/HEAD state.")

            if return_code != 0:
                if usage_limit_detected:
                    raise AutoCoderUsageLimitError(full_output)
                raise RuntimeError(f"muse CLI failed with return code {return_code}\\n{full_output}")
            if usage_limit_detected:
                raise AutoCoderUsageLimitError(full_output)

            return full_output
        except subprocess.TimeoutExpired:
            if process:
                process.kill()
            raise AutoCoderTimeoutError("muse CLI timed out after 7200 seconds")

    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        """Execute LLM with the given prompt."""
        return self._run_muse_cli(prompt, is_noedit)

    def check_mcp_server_configured(self, server_name: str) -> bool:
        """Check if a specific MCP server is configured for Muse CLI."""
        return False

    def add_mcp_server_config(self, server_name: str, command: str, args: list[str]) -> bool:
        """Add MCP server configuration to Muse CLI config."""
        return False
