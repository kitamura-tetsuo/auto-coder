"""
LLM Output Logger for Auto-Coder.

This module provides the LLMOutputLogger class for logging LLM command output
to JSON Lines files with metadata including timestamp, caller location, model,
prompt information, and output content.

Features:
- JSON Lines format for easy parsing
- Optional real-time feedback via callback
- Metadata tracking (timestamp, model, prompt, etc.)
- Automatic log file creation with rotation
"""

import json
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union


class LLMOutputLogger:
    """
    Logger for LLM output.

    Logs LLM command executions to JSON Lines files with metadata including
    timestamp, caller file and line number, model, prompt length, output content, etc.

    Features:
    - JSON Lines format for easy analysis
    - Optional real-time feedback via callback
    - Metadata tracking (timestamp, model, prompt length, etc.)
    - Context manager support for automatic logging
    """

    def __init__(
        self,
        log_dir: Optional[Path] = None,
        logger: Optional[Any] = None,
        realtime_callback: Optional[Callable[[str], None]] = None,
    ):
        """
        Initialize LLMOutputLogger.

        Args:
            log_dir: Directory to store log files. Defaults to ~/.auto-coder/log
            logger: Optional loguru logger instance to use
            realtime_callback: Optional callback for real-time output feedback
        """
        from .logger_config import get_logger

        self.log_dir = log_dir or (Path.home() / ".auto-coder" / "log")
        self.logger = logger or get_logger(__name__)
        self.realtime_callback = realtime_callback
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _get_log_file_path(self) -> Path:
        """
        Get the path to the current log file.

        Returns:
            Path to the log file (llm_output_YYYY-MM-DD.jsonl)
        """
        today = datetime.now().strftime("%Y-%m-%d")
        return self.log_dir / f"llm_output_{today}.jsonl"

    def _format_log_entry(
        self,
        caller_file: str,
        caller_line: int,
        model: str,
        prompt: str,
        output: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Format a single log entry with LLM execution metadata.

        Args:
            caller_file: Path to the file calling the LLM
            caller_line: Line number where the LLM was called
            model: The model name used
            prompt: The prompt sent to the LLM
            output: The LLM's output response
            metadata: Optional additional metadata

        Returns:
            Dictionary with log entry fields
        """
        entry = {
            "timestamp": datetime.now().isoformat(),
            "caller_file": caller_file,
            "caller_line": str(caller_line),
            "model": model,
            "prompt_length": len(prompt),
            "output": output,
        }
        if metadata:
            entry["metadata"] = metadata
        return entry

    def log_output(
        self,
        model: str,
        prompt: str,
        output: str,
        caller_file: str,
        caller_line: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Log a complete LLM output execution.

        Args:
            model: The model name used
            prompt: The prompt sent to the LLM
            output: The LLM's output response
            caller_file: Path to the file calling the LLM
            caller_line: Line number where the LLM was called
            metadata: Optional additional metadata
        """
        # Format the entry
        entry = self._format_log_entry(
            caller_file=caller_file,
            caller_line=caller_line,
            model=model,
            prompt=prompt,
            output=output,
            metadata=metadata,
        )

        # Write to JSONL file
        log_file = self._get_log_file_path()
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            # Don't fail the command if logging fails
            if self.logger:
                self.logger.warning(f"Failed to log LLM output: {e}")

        # Call real-time callback if provided
        if self.realtime_callback:
            try:
                self.realtime_callback(output)
            except Exception as e:
                if self.logger:
                    self.logger.warning(f"Failed to call realtime callback: {e}")

    @contextmanager
    def logged_output(
        self,
        model: str,
        prompt: str,
        caller_file: str,
        caller_line: int,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """
        Context manager for executing an LLM with automatic logging.

        Automatically extracts caller information (file and line) and logs the complete output.

        Args:
            model: The model name used
            prompt: The prompt sent to the LLM
            caller_file: Path to the file calling the LLM
            caller_line: Line number where the LLM was called
            metadata: Optional additional metadata

        Yields:
            Function to call with the output when done

        Example:
            with llm_logger.logged_output("GPT-5", prompt, __file__, __line__) as log_output:
                output = run_llm(prompt)
                log_output(output)
        """
        import inspect

        # Get caller information if not provided
        if caller_file == "<input>" or caller_line == 0:
            frame = inspect.currentframe()
            if frame and frame.f_back:
                caller_frame = frame.f_back
                caller_file = caller_frame.f_code.co_filename
                caller_line = caller_frame.f_lineno

        output_buffer = []

        def log_output_func(output: str) -> None:
            """Function to call when output is ready."""
            output_buffer.append(output)
            self.log_output(
                model=model,
                prompt=prompt,
                output=output,
                caller_file=caller_file,
                caller_line=caller_line,
                metadata=metadata,
            )

        try:
            yield log_output_func
        finally:
            # Ensure output is logged even if exception occurs
            if output_buffer:
                full_output = "".join(output_buffer)
                self.log_output(
                    model=model,
                    prompt=prompt,
                    output=full_output,
                    caller_file=caller_file,
                    caller_line=caller_line,
                    metadata=metadata,
                )

    def log_streaming_output(
        self,
        model: str,
        prompt: str,
        lines: List[str],
        caller_file: str,
        caller_line: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Log streaming output lines and return the combined output.

        Args:
            model: The model name used
            prompt: The prompt sent to the LLM
            lines: List of output lines
            caller_file: Path to the file calling the LLM
            caller_line: Line number where the LLM was called
            metadata: Optional additional metadata

        Returns:
            Combined output string
        """
        output = "\n".join(lines).strip()

        # Call real-time callback for each line if provided
        if self.realtime_callback:
            for line in lines:
                if line:
                    try:
                        self.realtime_callback(line)
                    except Exception as e:
                        if self.logger:
                            self.logger.warning(f"Failed to call realtime callback: {e}")

        # Log the complete output as one JSON object
        self.log_output(
            model=model,
            prompt=prompt,
            output=output,
            caller_file=caller_file,
            caller_line=caller_line,
            metadata=metadata,
        )

        return output


# Global logger instance
_llm_output_logger: Optional[LLMOutputLogger] = None


def get_llm_output_logger() -> LLMOutputLogger:
    """
    Get the global LLMOutputLogger instance.

    Returns:
        Global LLMOutputLogger instance
    """
    global _llm_output_logger
    if _llm_output_logger is None:
        _llm_output_logger = LLMOutputLogger()
    return _llm_output_logger


def set_llm_output_logger(logger: LLMOutputLogger) -> None:
    """
    Set the global LLMOutputLogger instance.

    Args:
        logger: LLMOutputLogger instance to use globally
    """
    global _llm_output_logger
    _llm_output_logger = logger
