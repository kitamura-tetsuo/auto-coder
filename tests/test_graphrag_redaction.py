import subprocess
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

from src.auto_coder.graphrag_index_manager import GraphRAGIndexManager
from src.auto_coder.logger_config import setup_logger


class TestGraphRAGRedaction:
    """Test redaction of sensitive information in GraphRAG logs."""

    @pytest.fixture
    def mock_subprocess_run(self):
        with patch("subprocess.run") as mock:
            yield mock

    def test_run_graph_builder_redacts_stderr(self, mock_subprocess_run):
        """Test that _run_graph_builder redacts sensitive info from stderr when command fails."""
        # Setup
        setup_logger(log_level="DEBUG")
        manager = GraphRAGIndexManager()
        manager.set_graph_builder_path_for_testing(MagicMock())

        with patch.object(manager, "_validate_graph_builder_path", return_value=(True, "OK")):
            with patch.object(manager, "_check_graph_builder_cli_compatibility", return_value=(True, "OK")):
                # Mock failure with sensitive info in stderr
                mock_result = MagicMock()
                mock_result.returncode = 1
                mock_result.stdout = ""
                # Use a valid GitHub token format
                mock_result.stderr = "Error: Invalid token ghp_secret1234567890abcdef for user"
                mock_subprocess_run.return_value = mock_result

                # Execute
                logs = []
                logger.add(lambda msg: logs.append(msg))

                # Call private method for testing
                manager._run_graph_builder()

                # Verify
                log_text = "".join([str(log_entry) for log_entry in logs])

                # Check if error is logged
                assert "graph-builder failed with return code 1" in log_text

                # Assert that the secret is NOT present (redacted)
                if "ghp_secret1234567890abcdef" in log_text:
                    pytest.fail("Sensitive information found in logs! 'ghp_secret1234567890abcdef' should be redacted.")

                if "[REDACTED]" not in log_text:
                    pytest.fail("Redaction marker [REDACTED] not found in logs.")
