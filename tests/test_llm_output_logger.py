"""Tests for LLMOutputLogger functionality."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.auto_coder.llm_output_logger import LLMOutputLogger


class TestLLMOutputLogger:
    """Test cases for LLMOutputLogger class."""

    def test_init_with_default_settings(self):
        """Test logger initialization with default settings."""
        logger = LLMOutputLogger()
        assert logger.enabled is True
        assert logger.env_var == "AUTO_CODER_LLM_OUTPUT_LOG_ENABLED"
        # Check that default path is constructed correctly
        expected_path = Path.home() / ".auto-coder" / "logs" / "llm_output.jsonl"
        assert logger.log_path == expected_path

    def test_init_with_custom_log_path(self):
        """Test logger initialization with custom log path."""
        with tempfile.TemporaryDirectory() as temp_dir:
            custom_path = Path(temp_dir) / "custom.jsonl"
            logger = LLMOutputLogger(log_path=custom_path)
            assert logger.log_path == custom_path

    def test_init_disabled_via_parameter(self):
        """Test logger can be disabled via parameter."""
        logger = LLMOutputLogger(enabled=False)
        assert logger.enabled is False

    def test_init_enabled_via_env_var_true(self):
        """Test logger is enabled when env var is set to true."""
        with patch.dict(os.environ, {"AUTO_CODER_LLM_OUTPUT_LOG_ENABLED": "1"}):
            logger = LLMOutputLogger()
            assert logger.enabled is True

    def test_init_enabled_via_env_var_false(self):
        """Test logger is disabled when env var is set to false."""
        with patch.dict(os.environ, {"AUTO_CODER_LLM_OUTPUT_LOG_ENABLED": "0"}):
            # When env var is set to false, logger should be disabled even if enabled=True
            logger = LLMOutputLogger(enabled=True)
            # Env var should override the parameter
            assert logger.enabled is False

    def test_init_enabled_via_env_var_various_values(self):
        """Test logger respects various truthy/falsy env var values."""
        truthy_values = ["1", "true", "TRUE", "True", "yes", "YES", "on", "ON"]
        falsy_values = ["0", "false", "FALSE", "False", "no", "NO", "off", "OFF", ""]

        for value in truthy_values:
            with patch.dict(os.environ, {"AUTO_CODER_LLM_OUTPUT_LOG_ENABLED": value}, clear=True):
                logger = LLMOutputLogger()
                assert logger.enabled is True, f"Failed for value: {value}"

        for value in falsy_values:
            with patch.dict(os.environ, {"AUTO_CODER_LLM_OUTPUT_LOG_ENABLED": value}, clear=True):
                # When no explicit enabled parameter, env var should control
                logger = LLMOutputLogger()
                # Empty string or falsy value should disable (default to True if no env var)
                if value == "":
                    assert logger.enabled is True, f"Failed for empty value: {value}"
                else:
                    assert logger.enabled is False, f"Failed for value: {value}"

    def test_init_creates_log_directory(self):
        """Test that logger creates log directory if it doesn't exist."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "logs" / "test.jsonl"
            logger = LLMOutputLogger(log_path=log_file)
            # Directory should be created during init
            assert log_file.parent.exists()

    def test_log_request_creates_jsonl_entry(self):
        """Test that log_request creates a valid JSON line."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "test.jsonl"
            logger = LLMOutputLogger(log_path=log_file, enabled=True)

            logger.log_request(
                backend="codex",
                model="codex",
                prompt_length=100,
            )

            logger.flush()
            logger.close()

            # Verify JSONL file was created and contains valid JSON
            assert log_file.exists()
            content = log_file.read_text().strip()
            assert content

            # Parse as JSON to verify it's valid
            data = json.loads(content)
            assert data["event_type"] == "llm_request"
            assert data["backend"] == "codex"
            assert data["model"] == "codex"
            assert data["prompt_length"] == 100
            assert "timestamp" in data

    def test_log_response_creates_jsonl_entry(self):
        """Test that log_response creates a valid JSON line."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "test.jsonl"
            logger = LLMOutputLogger(log_path=log_file, enabled=True)

            logger.log_response(
                backend="gemini",
                model="gemini-2.5-flash",
                response_length=500,
                duration_ms=1234,
                status="success",
            )

            logger.flush()
            logger.close()

            # Verify JSONL file was created and contains valid JSON
            assert log_file.exists()
            content = log_file.read_text().strip()
            data = json.loads(content)

            assert data["event_type"] == "llm_response"
            assert data["backend"] == "gemini"
            assert data["model"] == "gemini-2.5-flash"
            assert data["response_length"] == 500
            assert data["duration_ms"] == 1234
            assert data["status"] == "success"

    def test_log_interaction_creates_jsonl_entry(self):
        """Test that log_interaction creates a valid JSON line."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "test.jsonl"
            logger = LLMOutputLogger(log_path=log_file, enabled=True)

            logger.log_interaction(
                backend="qwen",
                model="qwen-turbo",
                prompt="test prompt",
                response="test response",
                duration_ms=567,
                status="success",
            )

            logger.flush()
            logger.close()

            # Verify JSONL file was created and contains valid JSON
            assert log_file.exists()
            content = log_file.read_text().strip()
            data = json.loads(content)

            assert data["event_type"] == "llm_interaction"
            assert data["backend"] == "qwen"
            assert data["model"] == "qwen-turbo"
            assert data["prompt_length"] == len("test prompt")
            assert data["response_length"] == len("test response")
            assert data["duration_ms"] == 567
            assert data["status"] == "success"

    def test_log_with_error(self):
        """Test logging with error information."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "test.jsonl"
            logger = LLMOutputLogger(log_path=log_file, enabled=True)

            logger.log_response(
                backend="codex",
                status="error",
                error="Rate limit exceeded",
            )

            logger.flush()
            logger.close()

            # Verify error is logged
            assert log_file.exists()
            content = log_file.read_text().strip()
            data = json.loads(content)

            assert data["status"] == "error"
            assert data["error"] == "Rate limit exceeded"

    def test_log_with_metadata(self):
        """Test logging with additional metadata."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "test.jsonl"
            logger = LLMOutputLogger(log_path=log_file, enabled=True)

            metadata = {
                "token_count": 1500,
                "temperature": 0.7,
                "custom_field": "custom_value",
            }

            logger.log_request(
                backend="gemini",
                metadata=metadata,
            )

            logger.flush()
            logger.close()

            # Verify metadata is included
            assert log_file.exists()
            content = log_file.read_text().strip()
            data = json.loads(content)

            assert data["token_count"] == 1500
            assert data["temperature"] == 0.7
            assert data["custom_field"] == "custom_value"

    def test_multiple_log_entries(self):
        """Test that multiple log entries are written correctly."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "test.jsonl"
            logger = LLMOutputLogger(log_path=log_file, enabled=True)

            # Write multiple entries
            logger.log_request(backend="codex", model="codex", prompt_length=100)
            logger.log_request(backend="gemini", model="gemini-2.5-flash", prompt_length=200)
            logger.log_response(backend="codex", status="success", response_length=500)

            logger.flush()
            logger.close()

            # Verify all entries are present
            assert log_file.exists()
            lines = log_file.read_text().strip().split("\n")
            assert len(lines) == 3

            # Parse each line
            entry1 = json.loads(lines[0])
            entry2 = json.loads(lines[1])
            entry3 = json.loads(lines[2])

            assert entry1["event_type"] == "llm_request"
            assert entry1["backend"] == "codex"

            assert entry2["event_type"] == "llm_request"
            assert entry2["backend"] == "gemini"

            assert entry3["event_type"] == "llm_response"
            assert entry3["backend"] == "codex"

    def test_logging_disabled_via_env_var(self):
        """Test that nothing is written when logging is disabled via env var."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "test.jsonl"

            # Set env var to disable
            with patch.dict(os.environ, {"AUTO_CODER_LLM_OUTPUT_LOG_ENABLED": "0"}, clear=True):
                logger = LLMOutputLogger(log_path=log_file, enabled=True)

                logger.log_request(backend="codex", model="codex")

                logger.flush()
                logger.close()

                # File should not exist or be empty
                assert not log_file.exists() or log_file.read_text().strip() == ""

    def test_logging_disabled_via_parameter(self):
        """Test that nothing is written when logging is disabled via parameter."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "test.jsonl"
            logger = LLMOutputLogger(log_path=log_file, enabled=False)

            logger.log_request(backend="codex", model="codex")

            logger.flush()
            logger.close()

            # File should not exist
            assert not log_file.exists()

    def test_context_manager(self):
        """Test that logger can be used as a context manager."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "test.jsonl"

            with LLMOutputLogger(log_path=log_file) as logger:
                logger.log_request(backend="codex", model="codex")
                # File should be closed automatically

            # Verify file was created and closed properly
            assert log_file.exists()
            content = log_file.read_text().strip()
            assert content

    def test_repr(self):
        """Test string representation of logger."""
        logger = LLMOutputLogger()
        repr_str = repr(logger)
        assert "LLMOutputLogger" in repr_str
        assert "enabled=True" in repr_str
        assert "llm_output.jsonl" in repr_str

    def test_automatic_timestamp(self):
        """Test that timestamp is automatically added if not provided."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "test.jsonl"
            logger = LLMOutputLogger(log_path=log_file, enabled=True)

            logger.log_request(backend="codex")

            logger.flush()
            logger.close()

            # Verify timestamp was added
            assert log_file.exists()
            content = log_file.read_text().strip()
            data = json.loads(content)

            assert "timestamp" in data
            # Verify it's a valid ISO timestamp
            from datetime import datetime

            timestamp = data["timestamp"]
            # Should be able to parse as ISO format
            parsed_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            assert parsed_time is not None

    def test_custom_environment_variable(self):
        """Test logger with custom environment variable."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "test.jsonl"

            # Use custom env var to enable
            with patch.dict(os.environ, {"MY_CUSTOM_VAR": "1"}, clear=True):
                # When enabled=True is set, env var should override
                logger = LLMOutputLogger(
                    log_path=log_file,
                    enabled=False,
                    environment_variable="MY_CUSTOM_VAR",
                )
                assert logger.enabled is True

            # Use custom env var to disable
            with patch.dict(os.environ, {"MY_CUSTOM_VAR": "0"}, clear=True):
                # When no explicit enabled, env var should control
                logger = LLMOutputLogger(
                    log_path=log_file,
                    enabled=True,
                    environment_variable="MY_CUSTOM_VAR",
                )
                assert logger.enabled is False

            # When no env var and no explicit enabled, should default to True
            with patch.dict(os.environ, {}, clear=True):
                logger = LLMOutputLogger(
                    log_path=log_file,
                    environment_variable="MY_CUSTOM_VAR",
                )
                assert logger.enabled is True

    def test_json_serialization_safety(self):
        """Test that logger handles non-serializable data gracefully."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "test.jsonl"
            logger = LLMOutputLogger(log_path=log_file, enabled=True)

            # Attempt to log with metadata that might not be JSON serializable
            logger.log_request(
                backend="codex",
                metadata={
                    "function": lambda x: x,  # Non-serializable
                    "number": 123,
                    "string": "test",
                },
            )

            logger.flush()
            logger.close()

            # Should handle gracefully (though the behavior depends on json.dumps)
            # This test just ensures no exception is raised during normal operation

    def test_long_prompt_and_response(self):
        """Test logging with long prompt and response text."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "test.jsonl"
            logger = LLMOutputLogger(log_path=log_file, enabled=True)

            long_prompt = "A" * 10000
            long_response = "B" * 10000

            logger.log_interaction(
                backend="codex",
                prompt=long_prompt,
                response=long_response,
                status="success",
            )

            logger.flush()
            logger.close()

            # Verify lengths are recorded correctly
            assert log_file.exists()
            content = log_file.read_text().strip()
            data = json.loads(content)

            assert data["prompt_length"] == 10000
            assert data["response_length"] == 10000
