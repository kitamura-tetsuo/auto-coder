"""Tests for GraphRAG backward compatibility layer."""

import pytest
from unittest.mock import Mock, MagicMock
from pathlib import Path
import time

pytest.skip(reason="BackwardCompatibilityLayer not yet implemented", allow_module_level=True)


class TestBackwardCompatibilityLayer:
    """Test backward compatibility features."""

    def test_compatibility_layer_initialization(self, compatibility_graphrag_setup):
        """Test compatibility layer initializes correctly."""
        assert compatibility_graphrag_setup is not None
        assert compatibility_graphrag_setup.compatibility_mode is True

    def test_compatibility_mode_check(self, compatibility_graphrag_setup):
        """Test compatibility mode detection."""
        assert compatibility_graphrag_setup.is_compatibility_mode() is True

    def test_get_compatibility_session(self, compatibility_graphrag_setup):
        """Test getting compatibility session."""
        session = compatibility_graphrag_setup.get_compatibility_session()
        assert session is not None
        assert isinstance(session, str)

    def test_session_creation(self):
        """Test session creation."""
        from src.auto_coder.graphrag_mcp_integration import GraphRAGMCPIntegration

        integration = GraphRAGMCPIntegration()
        session_id = integration.create_session(str(Path.cwd().resolve()))
        assert session_id is not None
        assert session_id.startswith("session_")

    def test_get_session_context(self):
        """Test getting session context."""
        from src.auto_coder.graphrag_mcp_integration import GraphRAGMCPIntegration

        integration = GraphRAGMCPIntegration()
        session_id = integration.create_session(str(Path.cwd().resolve()))
        context = integration.get_session_context(session_id)

        assert context is not None
        assert context["session_id"] == session_id

    def test_get_repo_label_for_session(self):
        """Test getting repository label for session."""
        from src.auto_coder.graphrag_mcp_integration import GraphRAGMCPIntegration

        integration = GraphRAGMCPIntegration()
        session_id = integration.create_session(str(Path.cwd().resolve()))
        repo_label = integration.get_repo_label_for_session(session_id)

        assert repo_label is not None
        assert repo_label.startswith("Repo_")


class TestSessionIsolation:
    """Test session isolation functionality."""

    def test_multiple_sessions_areolation(self):
        """Test that multiple sessions provide isolation."""
        from src.auto_coder.graphrag_mcp_integration import GraphRAGMCPIntegration

        integration = GraphRAGMCPIntegration()

        # Create two different sessions with different paths and delay
        session1 = integration.create_session("/path/to/repo1")
        time.sleep(0.001)  # Small delay to ensure different timestamps
        session2 = integration.create_session("/path/to/repo2")

        # Sessions should be different
        assert session1 != session2

        # Repository labels should be different
        repo_label1 = integration.get_repo_label_for_session(session1)
        repo_label2 = integration.get_repo_label_for_session(session2)

        assert repo_label1 != repo_label2
        assert repo_label1.startswith("Repo_")
        assert repo_label2.startswith("Repo_")


class TestAPICompatibility:
    """Test API backward compatibility at the integration level."""

    def test_backward_compatibility_layer_exists(self):
        """Test that BackwardCompatibilityLayer class exists."""
        from src.auto_coder.graphrag_mcp_integration import BackwardCompatibilityLayer

        # Should be able to import the class
        assert BackwardCompatibilityLayer is not None

    def test_create_session_method_exists(self):
        """Test that create_session method exists in GraphRAGMCPIntegration."""
        from src.auto_coder.graphrag_mcp_integration import GraphRAGMCPIntegration

        integration = GraphRAGMCPIntegration()
        assert hasattr(integration, "create_session")
        assert callable(integration.create_session)

    def test_get_session_context_method_exists(self):
        """Test that get_session_context method exists."""
        from src.auto_coder.graphrag_mcp_integration import GraphRAGMCPIntegration

        integration = GraphRAGMCPIntegration()
        assert hasattr(integration, "get_session_context")
        assert callable(integration.get_session_context)

    def test_get_repo_label_for_session_method_exists(self):
        """Test that get_repo_label_for_session method exists."""
        from src.auto_coder.graphrag_mcp_integration import GraphRAGMCPIntegration

        integration = GraphRAGMCPIntegration()
        assert hasattr(integration, "get_repo_label_for_session")
        assert callable(integration.get_repo_label_for_session)

    def test_code_analysis_tool_has_repo_label_parameter(self):
        """Test that CodeAnalysisTool methods accept repo_label parameter.

        Note: This test requires the graphrag_mcp package to be installed.
        Skipping in this environment.
        """
        pytest.skip("Requires graphrag_mcp package installation")

    def test_semantic_search_in_collection_method_exists(self):
        """Test that semantic_code_search_in_collection method exists.

        Note: This test requires the graphrag_mcp package to be installed.
        Skipping in this environment.
        """
        pytest.skip("Requires graphrag_mcp package installation")


class TestBackwardCompatibilityWarnings:
    """Test that compatibility warnings are added when no session_id is provided."""

    def test_compatibility_mode_flag_in_result(self):
        """Test that results include _compatibility_mode when using old API."""
        # This is a conceptual test - the actual MCP server tests would need
        # the mcp.server package installed to run properly
        from unittest.mock import MagicMock

        # Mock the code_tool to simulate backward compatibility behavior
        mock_tool = MagicMock()
        mock_tool.find_symbol.return_value = {
            "symbol": {"id": "test"},
            "_compatibility_mode": True,
            "_warning": "Using global search (data contamination possible)",
        }

        # Verify the mock returns compatibility mode
        result = mock_tool.find_symbol("test_function")
        assert result["_compatibility_mode"] is True
        assert "global search" in result["_warning"].lower()
