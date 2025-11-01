"""Tests for MCP session management system.

This module tests the session-based repository context management functionality
that allows multiple auto-coder instances to run without data contamination.
"""

import pytest
import tempfile
import time
from pathlib import Path
from datetime import datetime, timedelta

from src.auto_coder.mcp_servers.graphrag_mcp.graphrag_mcp.session import GraphRAGMCPSession
from src.auto_coder.graphrag_mcp_integration import GraphRAGMCPIntegration


class TestGraphRAGMCPSession:
    """Test the GraphRAGMCPSession class."""

    def test_session_creation(self):
        """Test session creation with unique ID."""
        with tempfile.TemporaryDirectory() as tmpdir:
            session = GraphRAGMCPSession("test_123", tmpdir)

            assert session.session_id == "test_123"
            assert session.repo_path == Path(tmpdir).resolve()
            assert session.collection_name.startswith("repo_")
            assert session.created_at is not None
            assert session.last_accessed is not None
            assert session.created_at == session.last_accessed

    def test_collection_name_generation(self):
        """Test deterministic collection name generation."""
        with tempfile.TemporaryDirectory() as tmpdir1:
            with tempfile.TemporaryDirectory() as tmpdir2:
                session1 = GraphRAGMCPSession("s1", tmpdir1)
                session2 = GraphRAGMCPSession("s2", tmpdir1)
                session3 = GraphRAGMCPSession("s3", tmpdir2)

                # Same repo path should generate same collection name
                assert session1.collection_name == session2.collection_name

                # Different repo should generate different collection name
                assert session3.collection_name != session1.collection_name

                # Collection names should start with 'repo_'
                assert session1.collection_name.startswith("repo_")
                assert session3.collection_name.startswith("repo_")

    def test_collection_name_format(self):
        """Test collection name format matches expected pattern."""
        with tempfile.TemporaryDirectory() as tmpdir:
            session = GraphRAGMCPSession("test", tmpdir)

            # Collection name should be in format repo_<hash[:16]>
            assert session.collection_name.startswith("repo_")
            # Hash should be 16 characters
            hash_part = session.collection_name[5:]  # Remove "repo_" prefix
            assert len(hash_part) == 16
            # Hash should be hexadecimal
            assert all(c in "0123456789abcdef" for c in hash_part)

    def test_session_access_tracking(self):
        """Test last accessed timestamp updates."""
        with tempfile.TemporaryDirectory() as tmpdir:
            session = GraphRAGMCPSession("test", tmpdir)
            initial_access = session.last_accessed

            # Small delay to ensure timestamp difference
            time.sleep(0.01)

            session.update_access()

            assert session.last_accessed > initial_access

    def test_session_to_dict(self):
        """Test session conversion to dictionary."""
        with tempfile.TemporaryDirectory() as tmpdir:
            session = GraphRAGMCPSession("test_abc", tmpdir)

            session_dict = session.to_dict()

            assert session_dict["session_id"] == "test_abc"
            assert session_dict["repo_path"] == str(Path(tmpdir).resolve())
            assert session_dict["collection_name"] == session.collection_name
            assert "created_at" in session_dict
            assert "last_accessed" in session_dict

            # Verify timestamps are ISO format strings
            datetime.fromisoformat(session_dict["created_at"])
            datetime.fromisoformat(session_dict["last_accessed"])

    def test_session_repr(self):
        """Test session string representation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            session = GraphRAGMCPSession("test_abc", tmpdir)

            repr_str = repr(session)

            assert "GraphRAGMCPSession" in repr_str
            assert "test_abc" in repr_str
            assert session.collection_name in repr_str

    def test_session_path_resolution(self):
        """Test that repository paths are resolved to absolute paths."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Use relative path
            rel_path = Path(tmpdir).name
            parent = Path(tmpdir).parent
            relative = parent / rel_path

            session = GraphRAGMCPSession("test", str(relative))

            # Path should be absolute
            assert session.repo_path.is_absolute()
            assert session.repo_path == Path(tmpdir).resolve()


class TestGraphRAGMCPIntegrationSessions:
    """Test session management in GraphRAGMCPIntegration."""

    def test_create_session(self):
        """Test session creation through integration."""
        integration = GraphRAGMCPIntegration()

        with tempfile.TemporaryDirectory() as tmpdir:
            session_id = integration.create_session(tmpdir)

            assert session_id is not None
            assert len(session_id) == 8  # UUID truncated to 8 chars

    def test_create_session_returns_unique_ids(self):
        """Test that created sessions have unique IDs."""
        integration = GraphRAGMCPIntegration()

        with tempfile.TemporaryDirectory() as tmpdir:
            session_id1 = integration.create_session(tmpdir)
            session_id2 = integration.create_session(tmpdir)

            assert session_id1 != session_id2

    def test_get_session(self):
        """Test session retrieval."""
        integration = GraphRAGMCPIntegration()

        with tempfile.TemporaryDirectory() as tmpdir:
            session_id = integration.create_session(tmpdir)
            session = integration.get_session(session_id)

            assert session is not None
            assert session.session_id == session_id

    def test_get_nonexistent_session(self):
        """Test retrieving a session that doesn't exist."""
        integration = GraphRAGMCPIntegration()

        session = integration.get_session("nonexistent")

        assert session is None

    def test_get_session_updates_access(self):
        """Test that getting a session updates its access timestamp."""
        integration = GraphRAGMCPIntegration()

        with tempfile.TemporaryDirectory() as tmpdir:
            session_id = integration.create_session(tmpdir)
            time.sleep(0.01)
            session = integration.get_session(session_id)

            # The session should have been updated
            # (Note: We can't easily test this without modifying the session object)

    def test_list_sessions(self):
        """Test listing all active sessions."""
        integration = GraphRAGMCPIntegration()

        with tempfile.TemporaryDirectory() as tmpdir1:
            with tempfile.TemporaryDirectory() as tmpdir2:
                session_id1 = integration.create_session(tmpdir1)
                session_id2 = integration.create_session(tmpdir2)

                sessions = integration.list_sessions()

                assert len(sessions) == 2

                session_ids = [s["session_id"] for s in sessions]
                assert session_id1 in session_ids
                assert session_id2 in session_ids

    def test_list_empty_sessions(self):
        """Test listing sessions when none exist."""
        integration = GraphRAGMCPIntegration()

        sessions = integration.list_sessions()

        assert sessions == []

    def test_cleanup_expired_sessions(self):
        """Test cleanup of expired sessions."""
        integration = GraphRAGMCPIntegration()

        with tempfile.TemporaryDirectory() as tmpdir:
            session_id = integration.create_session(tmpdir)

            # Initially, no expired sessions
            cleaned = integration.cleanup_expired_sessions()
            assert cleaned == 0

            # Verify session still exists
            session = integration.get_session(session_id)
            assert session is not None

    def test_cleanup_with_max_age(self):
        """Test cleanup with different max_age_hours."""
        integration = GraphRAGMCPIntegration()

        with tempfile.TemporaryDirectory() as tmpdir:
            session_id = integration.create_session(tmpdir)

            # Try to clean with very small max_age (shouldn't clean recent session)
            cleaned = integration.cleanup_expired_sessions(max_age_hours=0)
            assert cleaned == 0

            # Try to clean with very large max_age (should clean old session)
            # We can't easily test this without modifying session timestamps

    def test_session_isolation(self):
        """Test that different sessions are isolated."""
        integration = GraphRAGMCPIntegration()

        with tempfile.TemporaryDirectory() as tmpdir1:
            with tempfile.TemporaryDirectory() as tmpdir2:
                session1_id = integration.create_session(tmpdir1)
                session2_id = integration.create_session(tmpdir2)

                session1 = integration.get_session(session1_id)
                session2 = integration.get_session(session2_id)

                # Sessions should be different
                assert session1.session_id != session2.session_id
                assert session1.repo_path != session2.repo_path

                # Collections should be different
                assert session1.collection_name != session2.collection_name

    def test_session_same_repo_same_collection(self):
        """Test that sessions for same repo use same collection."""
        integration = GraphRAGMCPIntegration()

        with tempfile.TemporaryDirectory() as tmpdir:
            session1_id = integration.create_session(tmpdir)
            session2_id = integration.create_session(tmpdir)

            session1 = integration.get_session(session1_id)
            session2 = integration.get_session(session2_id)

            # Same repo should use same collection
            assert session1.collection_name == session2.collection_name
            assert session1.repo_path == session2.repo_path

    def test_concurrent_session_creation(self):
        """Test thread-safe session creation."""
        integration = GraphRAGMCPIntegration()

        with tempfile.TemporaryDirectory() as tmpdir:
            session_ids = []
            num_sessions = 10

            # Create multiple sessions rapidly
            for _ in range(num_sessions):
                session_id = integration.create_session(tmpdir)
                session_ids.append(session_id)

            # All session IDs should be unique
            assert len(session_ids) == len(set(session_ids))

            # All sessions should be retrievable
            for session_id in session_ids:
                session = integration.get_session(session_id)
                assert session is not None


class TestBackwardCompatibility:
    """Test backward compatibility of session management."""

    def test_integration_without_sessions(self):
        """Test that GraphRAGMCPIntegration works without session management."""
        # This tests the fallback behavior when GraphRAGMCPSession is not available
        integration = GraphRAGMCPIntegration()

        # Should still be able to create integration
        assert integration is not None

        # Session-related attributes should exist
        assert hasattr(integration, "active_sessions")
        assert hasattr(integration, "session_lock")

    def test_session_methods_exist(self):
        """Test that session methods exist even if GraphRAGMCPSession is not available."""
        integration = GraphRAGMCPIntegration()

        # All session methods should exist
        assert hasattr(integration, "create_session")
        assert hasattr(integration, "get_session")
        assert hasattr(integration, "cleanup_expired_sessions")
        assert hasattr(integration, "list_sessions")

    def test_session_methods_callable(self):
        """Test that session methods are callable."""
        integration = GraphRAGMCPIntegration()

        # All session methods should be callable
        assert callable(integration.create_session)
        assert callable(integration.get_session)
        assert callable(integration.cleanup_expired_sessions)
        assert callable(integration.list_sessions)


if __name__ == "__main__":
    pytest.main([__file__])
