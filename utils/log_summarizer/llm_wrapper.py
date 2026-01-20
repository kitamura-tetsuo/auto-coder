import os
import sys
from typing import Optional

# Ensure src is in python path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../src")))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from auto_coder.cli_helpers import build_backend_manager_from_config
from auto_coder.llm_backend_config import get_llm_config


class LLMWrapper:
    def __init__(self, backend_name: Optional[str] = None):
        """
        Initialize the LLMWrapper.

        Args:
            backend_name: Optional name of the backend to use.
                          If None, defaults to the configured default.
        """
        cli_backends = [backend_name] if backend_name else None

        # Build backend manager using the helper that mirrors the main CLI logic
        self.backend_manager = build_backend_manager_from_config(cli_backends=cli_backends, enable_graphrag=False)  # Not needed for simple summarization
        # Prevent session resumption which causes context window issues by including previous chat history
        self.backend_manager._last_session_id = None
        # Ensure we don't start with a 'last backend' that triggers resume logic
        self.backend_manager._last_backend = None

        # Force no-restore-chat-history for AiderClient to avoid context window issues
        # This handles the case where Aider defaults to restoration even without session resumption args
        # We use ENV VAR as it is more reliable than CLI args sometimes
        # Also disable other "smart" features that might auto-add files from the log content or consume tokens
        os.environ["AIDER_RESTORE_CHAT_HISTORY"] = "false"
        os.environ["AIDER_DETECT_URLS"] = "false"
        os.environ["AIDER_SUGGEST_SHELL_COMMANDS"] = "false"
        os.environ["AIDER_SHOW_REPO_MAP"] = "false"

        try:
            target_backend = backend_name or self.backend_manager._default_backend
            cli = self.backend_manager._get_or_create_client(target_backend)
            if hasattr(cli, "set_extra_args"):
                cli.set_extra_args(["--no-restore-chat-history", "--no-detect-urls", "--no-suggest-shell-commands"])
        except Exception as e:
            print(f"Warning: Failed to set no-restore-chat-history: {e}")

    MAX_CHUNK_SIZE = 200000  # Increased to ~50k tokens to minimize calls

    def _split_text(self, text: str) -> list[str]:
        """Split text into chunks respecting line boundaries."""
        chunks = []
        current_chunk = []
        current_length = 0
        for line in text.splitlines(keepends=True):
            if current_length + len(line) > self.MAX_CHUNK_SIZE:
                if current_chunk:
                    chunks.append("".join(current_chunk))
                current_chunk = [line]
                current_length = len(line)
            else:
                current_chunk.append(line)
                current_length += len(line)
        if current_chunk:
            chunks.append("".join(current_chunk))
        return chunks

    def summarize_log(self, log_content: str) -> str:
        """
        Summarize the provided log content using the LLM.
        Handles large logs by recursively chunking and summarizing.
        """
        if len(log_content) <= self.MAX_CHUNK_SIZE:
            return self._run_summarization(log_content)

        print(f"Log content length ({len(log_content)}) exceeds limit. Splitting into chunks...")
        chunks = self._split_text(log_content)
        chunk_summaries = []

        for i, chunk in enumerate(chunks):
            print(f"Summarizing chunk {i+1}/{len(chunks)}...")
            summary = self._run_summarization(chunk)
            chunk_summaries.append(summary)

        combined_text = "Here are the summaries of parts of the error log. Please provide a consolidated summary:\n\n" + "\n---\n".join(chunk_summaries)

        # Recurse in case the combined summary is still too large (unlikely but safe)
        return self.summarize_log(combined_text)

    def _run_summarization(self, content: str) -> str:
        """Internal method to run actual LLM call."""
        prompt = f"""
You are an expert software engineer.
Please analyze the following error log.
Extract 3 to 50 lines from the log that accurately and concisely represent the current problem.
Do not summarize, but extract the lines directly as they appear in the log.
If there are test failures, include the filename of the failed test.
Include lines that clearly show the content of the failure or error.

Error Log:
{content}
"""
        # Change CWD to /tmp to prevent aider from finding relevant files in the repo
        # and automatically adding them to the chat context, which blows up the token limit.
        # This is safe because we are just summarizing text content, not editing files.
        original_cwd = os.getcwd()
        try:
            os.chdir("/tmp")
            return self.backend_manager.run_prompt(prompt)
        except Exception as e:
            print(f"Error during summarization: {e}")
            return f"Error during summarization: {e}"
        finally:
            os.chdir(original_cwd)
