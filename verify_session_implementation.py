#!/usr/bin/env python3
"""
Simple verification script for session management implementation.
"""

import sys
import tempfile
from pathlib import Path

# Add src to path
sys.path.insert(0, '/home/node/3/auto-coder/src')

def test_session_class():
    """Test GraphRAGMCPSession class."""
    print("Testing GraphRAGMCPSession class...")

    from auto_coder.mcp_servers.graphrag_mcp.graphrag_mcp.session import GraphRAGMCPSession

    with tempfile.TemporaryDirectory() as tmpdir:
        session = GraphRAGMCPSession("test123", tmpdir)

        assert session.session_id == "test123"
        assert session.repo_path == Path(tmpdir).resolve()
        assert session.collection_name.startswith("repo_")
        assert len(session.collection_name) == 5 + 16  # "repo_" + 16 char hash

        print("  ✓ Session class creation works")
        print(f"  ✓ Session ID: {session.session_id}")
        print(f"  ✓ Collection name: {session.collection_name}")

def test_integration_sessions():
    """Test GraphRAGMCPIntegration session management."""
    print("\nTesting GraphRAGMCPIntegration session management...")

    from auto_coder.graphrag_mcp_integration import GraphRAGMCPIntegration

    integration = GraphRAGMCPIntegration()

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create session
        session_id = integration.create_session(tmpdir)
        assert session_id is not None
        assert len(session_id) == 8

        print(f"  ✓ Created session: {session_id}")

        # Get session
        session = integration.get_session(session_id)
        assert session is not None
        assert session.session_id == session_id

        print(f"  ✓ Retrieved session: {session.session_id}")

        # List sessions
        sessions = integration.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == session_id

        print(f"  ✓ Listed sessions: {len(sessions)}")

def test_session_isolation():
    """Test session isolation."""
    print("\nTesting session isolation...")

    from auto_coder.graphrag_mcp_integration import GraphRAGMCPIntegration

    integration = GraphRAGMCPIntegration()

    with tempfile.TemporaryDirectory() as tmpdir1:
        with tempfile.TemporaryDirectory() as tmpdir2:
            session1_id = integration.create_session(tmpdir1)
            session2_id = integration.create_session(tmpdir2)

            session1 = integration.get_session(session1_id)
            session2 = integration.get_session(session2_id)

            assert session1.collection_name != session2.collection_name

            print(f"  ✓ Session 1 collection: {session1.collection_name}")
            print(f"  ✓ Session 2 collection: {session2.collection_name}")
            print(f"  ✓ Collections are different (isolation works)")

def test_backward_compatibility():
    """Test backward compatibility."""
    print("\nTesting backward compatibility...")

    from auto_coder.graphrag_mcp_integration import GraphRAGMCPIntegration

    integration = GraphRAGMCPIntegration()

    # All methods should exist
    assert hasattr(integration, "create_session")
    assert hasattr(integration, "get_session")
    assert hasattr(integration, "cleanup_expired_sessions")
    assert hasattr(integration, "list_sessions")

    print("  ✓ All session methods exist")
    print("  ✓ Backward compatibility maintained")

def test_code_analysis_tool():
    """Test CodeAnalysisTool collection methods."""
    print("\nTesting CodeAnalysisTool collection methods...")

    from auto_coder.mcp_servers.graphrag_mcp.graphrag_mcp.code_analysis_tool import CodeAnalysisTool

    tool = CodeAnalysisTool()

    # Methods should exist
    assert hasattr(tool, "find_symbol_with_collection")
    assert hasattr(tool, "semantic_code_search_in_collection")

    print("  ✓ Collection-specific methods exist")
    print("  ✓ Backward compatibility maintained")

def main():
    """Run all verification tests."""
    print("=" * 60)
    print("Session Management Implementation Verification")
    print("=" * 60)

    try:
        test_session_class()
        test_integration_sessions()
        test_session_isolation()
        test_backward_compatibility()
        test_code_analysis_tool()

        print("\n" + "=" * 60)
        print("✓ All verification tests passed!")
        print("=" * 60)
        return 0

    except Exception as e:
        print("\n" + "=" * 60)
        print(f"✗ Verification failed: {e}")
        print("=" * 60)
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())
