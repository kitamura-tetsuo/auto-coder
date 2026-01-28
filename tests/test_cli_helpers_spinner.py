
import os
from unittest.mock import MagicMock, patch

import pytest
from click import ClickException

from src.auto_coder.cli_helpers import initialize_graphrag


class TestInitializeGraphRAG:
    """Tests for initialize_graphrag function in cli_helpers.py."""

    def test_initialize_graphrag_with_spinner_success(self):
        """Test initialize_graphrag uses Spinner on success."""
        # Mock dependencies
        with patch("src.auto_coder.cli_helpers.Path") as mock_path, \
             patch("src.auto_coder.cli_commands_graphrag.run_graphrag_setup_mcp_programmatically") as mock_setup, \
             patch("src.auto_coder.graphrag_mcp_integration.GraphRAGMCPIntegration") as mock_integration, \
             patch("src.auto_coder.cli_helpers.click.echo") as mock_echo, \
             patch("src.auto_coder.cli_ui.Spinner") as MockSpinner, \
             patch("src.auto_coder.logger_config.get_logger"):

            # Setup mocks
            mock_path.home.return_value = MagicMock()
            # MCP dir not exists
            mock_path.home.return_value.__truediv__.return_value.exists.return_value = False

            mock_setup.return_value = True # Success

            mock_integration_instance = mock_integration.return_value
            mock_integration_instance.ensure_ready.return_value = True

            mock_spinner_instance = MagicMock()
            MockSpinner.return_value = mock_spinner_instance
            mock_spinner_instance.__enter__.return_value = mock_spinner_instance

            # Run
            initialize_graphrag(force_reindex=False)

            # Verify Spinner usage
            MockSpinner.assert_any_call("Automatically setting up GraphRAG MCP server...", show_timer=True)
            mock_setup.assert_called_once()

            # Verify success message set
            assert mock_spinner_instance.success_message == "GraphRAG MCP server setup completed successfully"

    def test_initialize_graphrag_with_spinner_failure(self):
        """Test initialize_graphrag uses Spinner on failure."""
        # Mock dependencies
        with patch("src.auto_coder.cli_helpers.Path") as mock_path, \
             patch("src.auto_coder.cli_commands_graphrag.run_graphrag_setup_mcp_programmatically") as mock_setup, \
             patch("src.auto_coder.graphrag_mcp_integration.GraphRAGMCPIntegration") as mock_integration, \
             patch("src.auto_coder.cli_helpers.click.echo") as mock_echo, \
             patch("src.auto_coder.cli_ui.Spinner") as MockSpinner, \
             patch("src.auto_coder.logger_config.get_logger"):

            # Setup mocks
            mock_path.home.return_value = MagicMock()
            mock_path.home.return_value.__truediv__.return_value.exists.return_value = False

            mock_setup.return_value = False # Failure

            mock_spinner_instance = MagicMock()
            MockSpinner.return_value = mock_spinner_instance
            mock_spinner_instance.__enter__.return_value = mock_spinner_instance

            # Run and expect exception
            with pytest.raises(ClickException) as excinfo:
                initialize_graphrag(force_reindex=False)

            assert "Failed to set up GraphRAG MCP server" in str(excinfo.value)

            # Verify Spinner usage
            MockSpinner.assert_any_call("Automatically setting up GraphRAG MCP server...", show_timer=True)

            # Verify error message set
            assert mock_spinner_instance.error_message == "GraphRAG MCP server setup failed"
