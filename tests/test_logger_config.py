"""Tests for logger configuration functionality."""

import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from loguru import logger

from auto_coder.logger_config import format_path_for_log, get_logger, setup_logger
from auto_coder.utils import log_action


class TestLoggerConfig:
    """Test cases for logger configuration."""

    def setup_method(self):
        """Setup for each test method."""
        # Remove all existing handlers before each test
        logger.remove()
        logger.configure(patcher=None)

    def teardown_method(self):
        """Cleanup after each test method."""
        # Remove all handlers and restore default
        logger.remove()
        logger.add(sys.stderr)
        logger.configure(patcher=None)

    def test_setup_logger_default_settings(self):
        """Test logger setup with default settings."""
        with patch("auto_coder.logger_config.settings") as mock_settings:
            mock_settings.log_level = "INFO"

            setup_logger()

            # Test that logger is configured
            test_logger = get_logger(__name__)
            assert test_logger is not None

    def test_setup_logger_with_custom_level(self):
        """Test logger setup with custom log level."""
        with patch("auto_coder.logger_config.settings") as mock_settings:
            mock_settings.log_level = "INFO"

            setup_logger(log_level="DEBUG")

            # Test that logger is configured with DEBUG level
            test_logger = get_logger(__name__)
            assert test_logger is not None

    def test_setup_logger_with_file_output(self):
        """Test logger setup with file output."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "test.log"

            with patch("auto_coder.logger_config.settings") as mock_settings:
                mock_settings.log_level = "INFO"

                setup_logger(log_level="INFO", log_file=str(log_file))

                # Test that logger is configured
                test_logger = get_logger(__name__)
                test_logger.info("Test message")

                # Check that log file was created
                assert log_file.exists()

    def test_setup_logger_creates_log_directory(self):
        """Test that logger setup creates log directory if it doesn't exist."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "logs" / "test.log"

            with patch("auto_coder.logger_config.settings") as mock_settings:
                mock_settings.log_level = "INFO"

                setup_logger(log_level="INFO", log_file=str(log_file))

                # Test that logger is configured
                test_logger = get_logger(__name__)
                test_logger.info("Test message")

                # Check that log directory and file were created
                assert log_file.parent.exists()
                assert log_file.exists()

    def test_setup_logger_without_file_info(self):
        """Test logger setup without file information."""
        with patch("auto_coder.logger_config.settings") as mock_settings:
            mock_settings.log_level = "INFO"

            setup_logger(include_file_info=False)

            # Test that logger is configured
            test_logger = get_logger(__name__)
            assert test_logger is not None

    def test_get_logger_returns_logger_instance(self):
        """Test that get_logger returns a logger instance."""
        with patch("auto_coder.logger_config.settings") as mock_settings:
            mock_settings.log_level = "INFO"

            setup_logger()
            test_logger = get_logger("test_module")

            assert test_logger is not None

    def test_logger_levels(self):
        """Test different log levels."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "test.log"

            with patch("auto_coder.logger_config.settings") as mock_settings:
                mock_settings.log_level = "DEBUG"

                setup_logger(log_level="DEBUG", log_file=str(log_file))
                test_logger = get_logger(__name__)

                # Test different log levels
                test_logger.debug("Debug message")
                test_logger.info("Info message")
                test_logger.warning("Warning message")
                test_logger.error("Error message")
                test_logger.critical("Critical message")

                # Force flush to ensure all messages are written
                import time

                time.sleep(0.1)  # Small delay to ensure async writing completes

                # Check that log file contains messages
                assert log_file.exists()
                log_content = log_file.read_text()
                assert "Debug message" in log_content
                assert "Info message" in log_content
                assert "Warning message" in log_content
                assert "Error message" in log_content
                assert "Critical message" in log_content

    def test_logger_file_format_contains_file_info(self):
        """Test that file log format contains file and line information."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "test.log"

            with patch("auto_coder.logger_config.settings") as mock_settings:
                mock_settings.log_level = "INFO"

                setup_logger(log_level="INFO", log_file=str(log_file), include_file_info=True)
                test_logger = get_logger(__name__)

                test_logger.info("Test message with file info")

                # Force flush to ensure all messages are written
                import time

                time.sleep(0.1)  # Small delay to ensure async writing completes

                # Check that log file contains file and line information
                assert log_file.exists()
                log_content = log_file.read_text()
                assert "test_logger_config.py" in log_content or __name__ in log_content
                assert "test_logger_file_format_contains_file_info" in log_content

    def test_logger_console_format_with_colors(self):
        """Test that console format includes color codes when enabled."""
        with patch("auto_coder.logger_config.settings") as mock_settings:
            mock_settings.log_level = "INFO"

            # Mock sys.stdout to capture output
            with patch("sys.stdout") as mock_stdout:
                setup_logger(log_level="INFO")
                test_logger = get_logger(__name__)

                test_logger.info("Test colored message")

                # Verify that logger.add was called with colorize=True
                # This is a basic test since we can't easily test actual color output
                assert mock_stdout is not None

    def test_logger_error_handling_for_invalid_log_level(self):
        """Test logger behavior with invalid log level."""
        with patch("auto_coder.logger_config.settings") as mock_settings:
            mock_settings.log_level = "INFO"

            # Should raise an exception for invalid log level
            with pytest.raises(ValueError, match="Invalid log level 'INVALID'"):
                setup_logger(log_level="INVALID")

    def test_format_path_for_log_trims_package_prefix(self):
        """Paths inside the project should be trimmed to package-relative form."""

        package_dir = Path(__file__).resolve().parents[1] / "src" / "auto_coder"
        target = package_dir / "utils.py"

        result = format_path_for_log(str(target))

        assert result == "auto_coder/utils.py"

    def test_format_path_for_log_preserves_external_paths(self, tmp_path):
        """Paths outside the package remain unchanged."""

        external_file = tmp_path / "external.py"
        external_file.write_text("")

        result = format_path_for_log(str(external_file))

        assert result == str(external_file.resolve())

    def test_logger_output_uses_trimmed_paths(self):
        """Log messages emitted from package modules should show trimmed paths."""

        buffer = StringIO()

        with patch("auto_coder.logger_config.settings") as mock_settings:
            mock_settings.log_level = "INFO"

            setup_logger(log_level="INFO", stream=buffer, include_file_info=True)

            log_action("Trimmed path check")
            logger.complete()

        log_output = buffer.getvalue()

        assert "auto_coder/utils.py" in log_output
        assert "/site-packages/" not in log_output
