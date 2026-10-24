"""Tests for LLMOutputLogger functionality."""

import json
import multiprocessing
import os
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from src.auto_coder.llm_output_logger import LLMOutputLogger
from src.auto_coder.runtime_locks import file_lock


def _write_after_process_barrier(log_path, barrier, connection):
    """Contend on the production logger path from a distinct OS process."""
    import src.auto_coder.llm_output_logger as logger_module

    @contextmanager
    def observed_file_lock(*args, **kwargs):
        barrier.wait(timeout=5)
        connection.send("attempting")
        with file_lock(*args, **kwargs):
            yield

    logger_module.file_lock = observed_file_lock
    LLMOutputLogger(log_path=log_path, enabled=True).log_request("process-contender", prompt_length=2)
    connection.send("complete")
    connection.close()


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

    def test_first_write_creates_log_directory(self):
        """Construction is side-effect free and the first write creates the directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "logs" / "test.jsonl"
            logger = LLMOutputLogger(log_path=log_file)
            assert not log_file.parent.exists()
            logger.log_request("codex")
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
                backend="antigravity",
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
            assert data["backend"] == "antigravity"
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
            assert data["response"] == "test response"
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
                backend="antigravity",
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
            logger.log_request(backend="antigravity", model="gemini-2.5-flash", prompt_length=200)
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
            assert entry2["backend"] == "antigravity"

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
            assert data["response"] == long_response

    def test_log_file_permissions(self):
        """Test that the log file is created with secure permissions (0o600)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "test_permissions.jsonl"
            logger = LLMOutputLogger(log_path=log_file, enabled=True)

            logger.log_request(backend="codex", model="codex")
            logger.flush()
            logger.close()

            assert log_file.exists()

            # Check file permissions
            stat = os.stat(log_file)
            mode = stat.st_mode & 0o777
            # On Windows, permissions might behave differently, but for this environment it should be 0o600
            if os.name != "nt":
                assert mode == 0o600, f"File permissions are {oct(mode)}, expected 0o600"


class TestBoundedLLMOutputLogger:
    """Real-filesystem regression coverage for the managed log family."""

    @staticmethod
    def _settings(max_bytes="4096", backups="2"):
        return patch.dict(
            os.environ,
            {
                "AUTO_CODER_LLM_OUTPUT_LOG_MAX_BYTES": max_bytes,
                "AUTO_CODER_LLM_OUTPUT_LOG_BACKUP_COUNT": backups,
            },
            clear=False,
        )

    def test_disabled_constructor_has_no_filesystem_effect(self, tmp_path):
        path = tmp_path / "missing" / "out.jsonl"
        with patch.dict(os.environ, {"AUTO_CODER_LLM_OUTPUT_LOG_ENABLED": "0"}):
            logger = LLMOutputLogger(path)
            logger.log_request("codex", prompt="ignored")
        assert not path.parent.exists()

    def test_rotation_retention_and_unrelated_files(self, tmp_path):
        path = tmp_path / "out.jsonl"
        unrelated = tmp_path / "out.jsonl.1"
        unrelated.write_text("untouched")
        with self._settings(backups="1"):
            logger = LLMOutputLogger(path)
            for number in range(12):
                logger.log_response("codex", response=f"{number}:" + "x" * 700)
        managed = [path, *tmp_path.glob("out.jsonl.auto-coder-archive.*.jsonl")]
        assert len(managed) == 2
        assert all(item.stat().st_size <= 4096 for item in managed)
        assert sum(item.stat().st_size for item in managed) <= 8192
        assert unrelated.read_text() == "untouched"
        assert all(json.loads(line) for item in managed for line in item.read_text().splitlines())

    @pytest.mark.parametrize("backup_count", ["0", "4"])
    def test_noncanonical_archive_aliases_are_never_owned(self, tmp_path, backup_count):
        path = tmp_path / "out.jsonl"
        prefix = path.name + ".auto-coder-archive."
        aliases = [
            tmp_path / f"{prefix}000000000000000000001.jsonl",
            tmp_path / f"{prefix}０００００００００００００００００００１.jsonl",
        ]
        alias_state = {}
        for index, alias in enumerate(aliases):
            content = f"unrelated-{index}\n".encode()
            alias.write_bytes(content)
            os.chmod(alias, 0o640)
            alias_state[alias] = (content, alias.stat().st_mode & 0o777)

        canonical_generations = (0, 1, 10**20)
        canonical = []
        for generation in canonical_generations:
            archive = tmp_path / f"{prefix}{generation:020d}.jsonl"
            archive.write_text(json.dumps({"generation": generation}) + "\n")
            canonical.append(archive)

        with self._settings(backups=backup_count):
            LLMOutputLogger(path, enabled=True).log_request("codex", prompt_length=1)

        for alias, (content, mode) in alias_state.items():
            assert alias.read_bytes() == content
            assert alias.stat().st_mode & 0o777 == mode
        if backup_count == "0":
            assert not any(archive.exists() for archive in canonical)
        else:
            assert all(archive.exists() for archive in canonical)

    def test_reconstructed_logger_enforces_smaller_size_and_retention(self, tmp_path):
        path = tmp_path / "out.jsonl"
        unrelated = tmp_path / "out.jsonl.1"
        unrelated.write_bytes(b"unrelated")
        os.chmod(unrelated, 0o640)
        unrelated_state = (unrelated.read_bytes(), unrelated.stat().st_mode & 0o777)

        with self._settings(max_bytes="8192", backups="3"):
            older_logger = LLMOutputLogger(path, enabled=True)
            for generation in range(4):
                older_logger.log_response("codex", response="x" * 6000)
                older_logger.log_request(f"marker-{generation}", prompt_length=1)

        with self._settings(max_bytes="4096", backups="1"):
            smaller_logger = LLMOutputLogger(path, enabled=True)
            smaller_logger.log_request("downsized", prompt_length=1)

        archives = sorted(tmp_path.glob("out.jsonl.auto-coder-archive.*.jsonl"))
        managed = [path, *archives]
        assert older_logger.max_bytes == 8192
        assert smaller_logger.max_bytes == 4096
        assert len(archives) == 1
        assert all(segment.stat().st_size <= 4096 for segment in managed)
        assert sum(segment.stat().st_size for segment in managed) <= 8192
        assert [json.loads(line)["backend"] for line in archives[0].read_text().splitlines()] == ["marker-2"]
        assert [json.loads(line)["backend"] for line in path.read_text().splitlines()] == ["marker-3", "downsized"]
        assert (unrelated.read_bytes(), unrelated.stat().st_mode & 0o777) == unrelated_state

        with self._settings(max_bytes="4096", backups="0"):
            restarted_logger = LLMOutputLogger(path, enabled=True)
            restarted_logger.log_request("no-archives", prompt_length=1)
        assert restarted_logger.backup_count == 0
        assert not list(tmp_path.glob("out.jsonl.auto-coder-archive.*.jsonl"))
        assert path.stat().st_size <= 4096
        assert (unrelated.read_bytes(), unrelated.stat().st_mode & 0o777) == unrelated_state

    def test_oversized_utf8_record_is_a_bounded_summary(self, tmp_path):
        path = tmp_path / "out.jsonl"
        with self._settings():
            logger = LLMOutputLogger(path)
            logger.log_response(
                "codex",
                response="界" * 5000,
                metadata={"response_length": 2**80, "huge": "z" * 20000},
            )
        raw = path.read_bytes()
        record = json.loads(raw)
        assert len(raw) <= 4096
        assert record["log_record_truncated"] is True
        assert record["original_record_bytes"] > 4096
        assert record["response_length"] == 5000
        assert record["omitted_length_fields"] == []

    def test_invalid_public_counter_is_named_in_summary(self, tmp_path):
        path = tmp_path / "out.jsonl"
        with self._settings():
            logger = LLMOutputLogger(path)
            logger.log_response("codex", response_length=-1, metadata={"huge": "x" * 10000})
        assert json.loads(path.read_bytes())["omitted_length_fields"] == ["response_length"]

    @pytest.mark.parametrize(
        ("method", "counter_name"),
        [("log_request", "prompt_length"), ("log_response", "response_length")],
    )
    def test_huge_public_counter_writes_exact_bounded_summary(self, tmp_path, method, counter_name):
        path = tmp_path / f"{method}.jsonl"
        huge_counter = 10**5000
        with self._settings():
            logger = LLMOutputLogger(path, enabled=True)
            getattr(logger, method)("codex", **{counter_name: huge_counter})
        raw = path.read_bytes()
        summary = json.loads(raw)
        ordinary_with_zero = (
            json.dumps(
                {
                    "event_type": "llm_request" if method == "log_request" else "llm_response",
                    "backend": "codex",
                    **({"status": "success"} if method == "log_response" else {}),
                    counter_name: 0,
                    "timestamp": summary["timestamp"],
                },
                ensure_ascii=False,
            )
            + "\n"
        ).encode()
        assert len(raw) <= 4096
        assert summary["original_record_bytes"] == len(ordinary_with_zero) + 5000
        assert summary["omitted_length_fields"] == [counter_name]

    def test_deeply_malformed_line_does_not_poison_recovery(self, tmp_path):
        path = tmp_path / "out.jsonl"
        path.write_bytes(b'{"x":' + b"[" * 1500 + b'\n{"marker": true}\n')
        with self._settings():
            logger = LLMOutputLogger(path, enabled=True)
            logger.log_request("first", prompt_length=1)
            logger.log_request("second", prompt_length=2)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert len(records) == 3
        assert records[0] == {"marker": True}
        assert [record.get("backend") for record in records] == [None, "first", "second"]
        assert path.stat().st_size <= 4096

    def test_normalizes_partial_and_oversized_legacy_segments(self, tmp_path):
        path = tmp_path / "out.jsonl"
        archive = tmp_path / "out.jsonl.auto-coder-archive.00000000000000000003.jsonl"
        path.write_bytes(b'{"old": 1}\n' + b"x" * 9000 + b'\n{"new": 2}\n{"partial"')
        archive.write_bytes(b"bad\n" + b'{"archive": true}\n')
        stage = tmp_path / "out.jsonl.auto-coder-stage.jsonl"
        stage.write_bytes(b"abandoned")
        with self._settings():
            LLMOutputLogger(path).log_request("codex", prompt_length=4)
        assert not stage.exists()
        active_lines = [json.loads(line) for line in path.read_text().splitlines()]
        assert {"new": 2} in active_lines
        assert active_lines[-1]["event_type"] == "llm_request"
        assert archive.read_text() == '{"archive": true}\n'
        assert path.stat().st_size <= 4096

    def test_old_instance_appends_to_current_inode_after_rotation(self, tmp_path):
        path = tmp_path / "out.jsonl"
        with self._settings(backups="1"):
            old = LLMOutputLogger(path)
            old.log_response("codex", response="a" * 3000)
            newer = LLMOutputLogger(path)
            newer.log_response("codex", response="b" * 3000)
            old.log_request("old-instance", prompt_length=1)
        active = [json.loads(line) for line in path.read_text().splitlines()]
        assert active[-1]["backend"] == "old-instance"

    def test_thread_writer_waits_for_contested_shared_lock(self, tmp_path, monkeypatch):
        path = tmp_path / "out.jsonl"
        logger = LLMOutputLogger(path, enabled=True)
        logger.log_request("before-thread", prompt_length=1)
        lock_path = path.with_name(path.name + ".auto-coder.lock").resolve()
        attempted = threading.Barrier(2)
        completed = threading.Event()

        @contextmanager
        def observed_file_lock(*args, **kwargs):
            attempted.wait(timeout=5)
            with file_lock(*args, **kwargs):
                yield

        monkeypatch.setattr("src.auto_coder.llm_output_logger.file_lock", observed_file_lock)

        def write_contender():
            LLMOutputLogger(path, enabled=True).log_request("thread-contender", prompt_length=2)
            completed.set()

        with file_lock(lock_path, timeout=5):
            contender = threading.Thread(target=write_contender)
            contender.start()
            attempted.wait(timeout=5)
            assert not completed.wait(timeout=0.25)
        contender.join(timeout=5)
        assert completed.is_set()
        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert [record["backend"] for record in records] == [
            "before-thread",
            "thread-contender",
        ]

    def test_process_writer_waits_for_contested_shared_lock(self, tmp_path):
        path = tmp_path / "out.jsonl"
        LLMOutputLogger(path, enabled=True).log_request("before-process", prompt_length=1)
        lock_path = path.with_name(path.name + ".auto-coder.lock").resolve()
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        parent_connection, child_connection = context.Pipe(duplex=False)
        contender = context.Process(
            target=_write_after_process_barrier,
            args=(path, barrier, child_connection),
        )

        with file_lock(lock_path, timeout=5):
            contender.start()
            child_connection.close()
            barrier.wait(timeout=5)
            assert parent_connection.poll(5)
            assert parent_connection.recv() == "attempting"
            assert not parent_connection.poll(0.25)
        assert parent_connection.poll(5)
        assert parent_connection.recv() == "complete"
        contender.join(timeout=5)
        assert contender.exitcode == 0
        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert [record["backend"] for record in records] == [
            "before-process",
            "process-contender",
        ]

    def test_invalid_settings_fall_back_independently(self, capsys):
        with self._settings(max_bytes="3", backups="nope"):
            logger = LLMOutputLogger(enabled=False)
        assert logger.max_bytes == 10485760
        assert logger.backup_count == 5
        diagnostic = capsys.readouterr().err
        assert len(diagnostic) < 1200
        assert "MAX_BYTES" in diagnostic and "BACKUP_COUNT" in diagnostic

    def test_nonfatal_failure_allows_same_instance_retry(self, tmp_path):
        path = tmp_path / "parent" / "out.jsonl"
        path.parent.write_text("not a directory")
        logger = LLMOutputLogger(path)
        logger.log_request("codex", prompt_length=1)
        path.parent.unlink()
        logger.log_request("codex", prompt_length=2)
        assert json.loads(path.read_bytes())["prompt_length"] == 2
        assert path.stat().st_mode & 0o777 == 0o600

    def test_unavailable_stderr_cannot_make_config_or_storage_failure_fatal(self, tmp_path, monkeypatch):
        class BrokenStderr:
            def write(self, value):
                raise BrokenPipeError("closed")

            def flush(self):
                raise BrokenPipeError("closed")

        path = tmp_path / "parent" / "out.jsonl"
        path.parent.write_text("not a directory")
        original_stderr = sys.stderr
        monkeypatch.setattr("sys.stderr", BrokenStderr())
        with self._settings(max_bytes="invalid"):
            logger = LLMOutputLogger(path, enabled=True)
        logger.log_request("failed", prompt_length=1)
        monkeypatch.setattr("sys.stderr", original_stderr)
        path.parent.unlink()
        logger.log_request("recovered", prompt_length=2)
        assert json.loads(path.read_bytes())["backend"] == "recovered"

    @pytest.mark.parametrize("method", ["log_request", "log_response", "log_interaction"])
    def test_circular_metadata_is_nonfatal_and_same_instance_retries(self, tmp_path, method):
        path = tmp_path / f"{method}.jsonl"
        circular = {}
        circular["self"] = circular
        logger = LLMOutputLogger(path, enabled=True)
        getattr(logger, method)("invalid", metadata=circular)
        logger.log_request("recovered", prompt_length=1)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["backend"] == "recovered"
