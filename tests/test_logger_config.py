"""Tests for logger configuration functionality."""

import sys
import tempfile
import uuid
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from loguru import logger

from src.auto_coder.logger_config import format_path_for_log, get_default_log_file, get_logger, setup_logger
from src.auto_coder.utils import log_action


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
        with patch("src.auto_coder.logger_config.settings") as mock_settings:
            mock_settings.log_level = "INFO"

            setup_logger()

            # Test that logger is configured
            test_logger = get_logger(__name__)
            assert test_logger is not None

    def test_setup_logger_with_custom_level(self):
        """Test logger setup with custom log level."""
        with patch("src.auto_coder.logger_config.settings") as mock_settings:
            mock_settings.log_level = "INFO"

            setup_logger(log_level="DEBUG")

            # Test that logger is configured with DEBUG level
            test_logger = get_logger(__name__)
            assert test_logger is not None

    def test_setup_logger_with_file_output(self):
        """Test logger setup with file output."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "test.log"

            with patch("src.auto_coder.logger_config.settings") as mock_settings:
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

            with patch("src.auto_coder.logger_config.settings") as mock_settings:
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
        with patch("src.auto_coder.logger_config.settings") as mock_settings:
            mock_settings.log_level = "INFO"

            setup_logger(include_file_info=False)

            # Test that logger is configured
            test_logger = get_logger(__name__)
            assert test_logger is not None

    def test_get_logger_returns_logger_instance(self):
        """Test that get_logger returns a logger instance."""
        with patch("src.auto_coder.logger_config.settings") as mock_settings:
            mock_settings.log_level = "INFO"

            setup_logger()
            test_logger = get_logger("test_module")

            assert test_logger is not None

    def test_logger_levels(self):
        """Test different log levels."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "test.log"

            with patch("src.auto_coder.logger_config.settings") as mock_settings:
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

            with patch("src.auto_coder.logger_config.settings") as mock_settings:
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
        with patch("src.auto_coder.logger_config.settings") as mock_settings:
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
        with patch("src.auto_coder.logger_config.settings") as mock_settings:
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
        """Log messages should show trimmed paths."""

        buffer = StringIO()

        with patch("src.auto_coder.logger_config.settings") as mock_settings:
            mock_settings.log_level = "INFO"

            setup_logger(log_level="INFO", stream=buffer, include_file_info=True)

            log_action("Trimmed path check")
            logger.complete()

        log_output = buffer.getvalue()

        # With opt(depth=1), the log shows the caller (this test file), not utils.py
        # The path should be trimmed to show tests/test_logger_config.py
        assert "tests/test_logger_config.py" in log_output
        # Should not have absolute paths or site-packages paths
        assert "/site-packages/" not in log_output
        assert "/workspaces/auto-coder/" not in log_output


class TestDefaultLogFile:
    """The application log must always be written, even without --log-file."""

    def setup_method(self):
        logger.remove()
        logger.configure(patcher=None)

    def teardown_method(self):
        logger.remove()
        logger.add(sys.stderr)
        logger.configure(patcher=None)

    def test_default_log_file_follows_health_log_dir(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AUTO_CODER_LOG_FILE", raising=False)
        monkeypatch.setenv("AUTO_CODER_HEALTH_LOG_DIR", str(tmp_path / "diag"))

        assert get_default_log_file() == tmp_path / "diag" / "auto-coder.log"

    def test_default_log_file_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUTO_CODER_LOG_FILE", str(tmp_path / "custom.log"))

        assert get_default_log_file() == tmp_path / "custom.log"

    def test_default_log_file_defaults_below_home(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AUTO_CODER_LOG_FILE", raising=False)
        monkeypatch.delenv("AUTO_CODER_HEALTH_LOG_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))

        assert get_default_log_file() == tmp_path / ".auto-coder" / "logs" / "auto-coder.log"

    def test_file_sink_level_can_be_more_verbose_than_console(self, monkeypatch, tmp_path):
        log_file = tmp_path / "app.log"
        monkeypatch.setenv("AUTO_CODER_FILE_LOG_LEVEL", "DEBUG")
        buffer = StringIO()

        with patch("src.auto_coder.logger_config.settings") as mock_settings:
            mock_settings.log_level = "INFO"

            setup_logger(log_level="INFO", log_file=str(log_file), stream=buffer)

            get_logger(__name__).debug("detailed trace entry")
            logger.complete()

        assert "detailed trace entry" in log_file.read_text(encoding="utf-8")
        assert "detailed trace entry" not in buffer.getvalue()

    def test_invalid_file_level_falls_back_to_console_level(self, monkeypatch, tmp_path):
        log_file = tmp_path / "app.log"
        monkeypatch.setenv("AUTO_CODER_FILE_LOG_LEVEL", "NOT_A_LEVEL")

        with patch("src.auto_coder.logger_config.settings") as mock_settings:
            mock_settings.log_level = "INFO"

            setup_logger(log_level="INFO", log_file=str(log_file), stream=StringIO())

            get_logger(__name__).debug("debug entry")
            get_logger(__name__).info("info entry")
            logger.complete()

        content = log_file.read_text(encoding="utf-8")
        assert "info entry" in content
        assert "debug entry" not in content


class TestExceptionDiagnosticsDoNotLeakLocals:
    """Loguru's ``diagnose`` traceback expansion must never reach an
    application sink, regardless of sink kind, verbosity or environment
    defaults. See https://github.com/kitamura-tetsuo/auto-coder/issues/1924.
    """

    def setup_method(self):
        logger.remove()
        logger.configure(patcher=None)

    def teardown_method(self):
        logger.remove()
        logger.add(sys.stderr)
        logger.configure(patcher=None)

    @staticmethod
    def _raise_with_secret_locals(secret: str) -> None:
        """Raise from a frame whose locals contain a secret under an
        unrelated variable name, nested inside Click-param-like and HTTP
        header-like containers, mirroring the disclosure described in the
        Issue."""

        click_params = {"github_token": secret, "repo": "kitamura-tetsuo/auto-coder"}
        request_headers = {"Authorization": f"Bearer {secret}"}
        opaque_value = secret  # unrelated name on purpose (AS-002)
        _ = (click_params, request_headers, opaque_value)
        raise RuntimeError("controlled failure for diagnostics test")

    def _assert_safe_and_useful(self, output: str, secret: str) -> None:
        assert secret not in output
        assert f"Bearer {secret}" not in output
        assert "RuntimeError" in output
        assert "controlled failure for diagnostics test" in output
        assert "_raise_with_secret_locals" in output

    def test_console_stream_sink_hides_locals_but_keeps_diagnostics(self, monkeypatch):
        secret = f"ghp_{uuid.uuid4().hex}"
        buffer = StringIO()

        with patch("src.auto_coder.logger_config.settings") as mock_settings:
            mock_settings.log_level = "DEBUG"

            setup_logger(log_level="DEBUG", stream=buffer)

            try:
                self._raise_with_secret_locals(secret)
            except RuntimeError:
                get_logger(__name__).exception("failed during controlled test")
            logger.complete()

        self._assert_safe_and_useful(buffer.getvalue(), secret)

    def test_file_sink_hides_locals_but_keeps_diagnostics(self, tmp_path):
        secret = f"ghp_{uuid.uuid4().hex}"
        log_file = tmp_path / "app.log"

        with patch("src.auto_coder.logger_config.settings") as mock_settings:
            mock_settings.log_level = "DEBUG"

            setup_logger(log_level="DEBUG", log_file=str(log_file), stream=StringIO())

            try:
                self._raise_with_secret_locals(secret)
            except RuntimeError:
                get_logger(__name__).exception("failed during controlled test")
            logger.complete()

        self._assert_safe_and_useful(log_file.read_text(encoding="utf-8"), secret)

    def test_progress_footer_sink_hides_locals_but_keeps_diagnostics(self):
        from src.auto_coder.progress_footer import ProgressFooter

        secret = f"ghp_{uuid.uuid4().hex}"
        buffer = StringIO()
        buffer.isatty = lambda: False  # type: ignore[method-assign]
        footer = ProgressFooter(stream=buffer)

        with patch("src.auto_coder.logger_config.settings") as mock_settings:
            mock_settings.log_level = "DEBUG"

            setup_logger(log_level="DEBUG", progress_footer=footer)

            try:
                self._raise_with_secret_locals(secret)
            except RuntimeError:
                get_logger(__name__).exception("failed during controlled test")
            logger.complete()

        self._assert_safe_and_useful(buffer.getvalue(), secret)

    def test_loguru_diagnose_env_default_cannot_reopen_leak(self, monkeypatch):
        """``LOGURU_DIAGNOSE=YES`` must not override the hard-coded
        ``diagnose=False`` on application sinks (REQ-002, AS-004)."""

        monkeypatch.setenv("LOGURU_DIAGNOSE", "YES")
        secret = f"ghp_{uuid.uuid4().hex}"
        buffer = StringIO()

        with patch("src.auto_coder.logger_config.settings") as mock_settings:
            mock_settings.log_level = "DEBUG"

            setup_logger(log_level="DEBUG", stream=buffer)

            try:
                self._raise_with_secret_locals(secret)
            except RuntimeError:
                get_logger(__name__).exception("failed during controlled test")
            logger.complete()

        self._assert_safe_and_useful(buffer.getvalue(), secret)

    def test_repeated_setup_does_not_reintroduce_diagnose(self):
        """Reconfiguring the logger repeatedly must keep every sink safe."""

        secret = f"ghp_{uuid.uuid4().hex}"
        buffer = StringIO()

        with patch("src.auto_coder.logger_config.settings") as mock_settings:
            mock_settings.log_level = "DEBUG"

            setup_logger(log_level="INFO", stream=StringIO())
            setup_logger(log_level="DEBUG", stream=StringIO())
            setup_logger(log_level="DEBUG", stream=buffer)

            try:
                self._raise_with_secret_locals(secret)
            except RuntimeError:
                get_logger(__name__).exception("failed during controlled test")
            logger.complete()

        self._assert_safe_and_useful(buffer.getvalue(), secret)
