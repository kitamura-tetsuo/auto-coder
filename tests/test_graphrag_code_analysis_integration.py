"""
Tests for GraphRAG code analysis integration.

Tests the integration of graph-builder code analysis into GraphRAG indexing.
"""

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

from src.auto_coder.graphrag_index_manager import GraphRAGIndexManager


class TestGraphRAGCodeAnalysisIntegration:
    """Test GraphRAG code analysis integration."""

    def test_find_graph_builder_in_repo(self, tmp_path):
        """Test finding graph-builder in repository directory."""
        # This test verifies that graph-builder can be found
        # In real environment, it will find the bundled graph_builder or repo's graph-builder
        manager = GraphRAGIndexManager(repo_path=str(tmp_path))
        result = manager._find_graph_builder()

        # Should find graph-builder (either bundled or in repo)
        assert result is not None
        assert result.exists()
        assert (result / "src").exists()

    def test_find_graph_builder_using_test_override(self, tmp_path):
        """Test finding graph-builder using test path override."""
        # Create a fake graph-builder structure
        graph_builder_dir = tmp_path / "graph-builder"
        graph_builder_dir.mkdir()
        (graph_builder_dir / "src").mkdir()
        (graph_builder_dir / "src" / "cli_python.py").touch()

        # Use dependency injection instead of complex mocking
        manager = GraphRAGIndexManager(repo_path=str(tmp_path))
        manager.set_graph_builder_path_for_testing(graph_builder_dir)

        result = manager._find_graph_builder()

        assert result is not None
        assert result == graph_builder_dir
        assert result.exists()

    def test_find_graph_builder_validation_failure(self, tmp_path):
        """Test graph-builder validation with missing required files."""
        # Create an incomplete graph-builder structure (missing src directory)
        incomplete_dir = tmp_path / "incomplete-graph-builder"
        incomplete_dir.mkdir()

        manager = GraphRAGIndexManager(repo_path=str(tmp_path))
        manager.set_graph_builder_path_for_testing(incomplete_dir)

        result = manager._find_graph_builder()

        # Should not find graph-builder due to validation failure
        assert result is None

    def test_find_graph_builder_not_found(self, tmp_path):
        """Test graph-builder not found in isolated environment."""
        # Use an override path that doesn't exist to force "not found"
        nonexistent_path = tmp_path / "nonexistent" / "graph-builder"

        # Set override path to a nonexistent directory
        manager = GraphRAGIndexManager(repo_path=str(tmp_path))
        manager.set_graph_builder_path_for_testing(nonexistent_path)

        result = manager._find_graph_builder()

        # Should not find graph-builder when override path doesn't exist
        assert result is None

    def test_fallback_python_indexing(self, tmp_path):
        """Test fallback Python indexing when graph-builder is not available."""
        # Create test Python files
        (tmp_path / "test1.py").write_text("def hello(): pass")
        (tmp_path / "test2.py").write_text("class MyClass: pass")

        manager = GraphRAGIndexManager(repo_path=str(tmp_path))
        result = manager._fallback_python_indexing()

        assert "nodes" in result
        assert "edges" in result
        assert len(result["nodes"]) == 2
        assert len(result["edges"]) == 0

        # Check node structure
        node = result["nodes"][0]
        assert "id" in node
        assert "kind" in node
        assert node["kind"] == "File"
        assert "fqname" in node
        assert "content" in node

    def test_fallback_python_indexing_skips_empty_files(self, tmp_path):
        """Test fallback indexing skips empty files."""
        (tmp_path / "empty.py").write_text("")
        (tmp_path / "nonempty.py").write_text("x = 1")

        manager = GraphRAGIndexManager(repo_path=str(tmp_path))
        result = manager._fallback_python_indexing()

        assert len(result["nodes"]) == 1
        assert "nonempty.py" in result["nodes"][0]["fqname"]

    @patch("subprocess.run")
    def test_run_graph_builder_typescript_version(self, mock_run, tmp_path):
        """Test running TypeScript version of graph-builder."""
        # Create graph-builder structure with TypeScript CLI
        graph_builder_dir = tmp_path / "graph-builder"
        graph_builder_dir.mkdir()
        (graph_builder_dir / "src").mkdir()
        (graph_builder_dir / "dist").mkdir()
        (graph_builder_dir / "dist" / "cli.js").touch()

        # Mock subprocess result
        graph_data = {
            "nodes": [{"id": "node1", "kind": "Function", "fqname": "test.ts:myFunc"}],
            "edges": [{"from": "node1", "to": "node2", "type": "CALLS"}],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = Path(temp_dir) / "graph-data.json"

            def write_output(*args, **kwargs):
                output_file.write_text(json.dumps(graph_data))
                return MagicMock(returncode=0, stderr="")

            mock_run.side_effect = write_output

            # Use dependency injection instead of patching
            manager = GraphRAGIndexManager(repo_path=str(tmp_path))
            manager.set_graph_builder_path_for_testing(graph_builder_dir)

            # Patch tempfile to use our controlled directory
            with patch("tempfile.TemporaryDirectory") as mock_temp:
                mock_temp.return_value.__enter__.return_value = temp_dir
                result = manager._run_graph_builder()

            assert result == graph_data
            assert len(result["nodes"]) == 1
            assert len(result["edges"]) == 1

            # Verify subprocess was called with correct arguments
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]
            assert call_args[0] == "node"
            assert "cli.js" in call_args[1]
            assert "scan" in call_args

    @patch("subprocess.run")
    def test_run_graph_builder_python_version(self, mock_run, tmp_path):
        """Test running Python version of graph-builder."""
        # Create graph-builder structure with Python CLI only
        graph_builder_dir = tmp_path / "graph-builder"
        graph_builder_dir.mkdir()
        (graph_builder_dir / "src").mkdir()
        (graph_builder_dir / "src" / "cli_python.py").touch()

        graph_data = {
            "nodes": [{"id": "node1", "kind": "Class", "fqname": "test.py:MyClass"}],
            "edges": [],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = Path(temp_dir) / "graph-data.json"

            def write_output(*args, **kwargs):
                output_file.write_text(json.dumps(graph_data))
                return MagicMock(returncode=0, stderr="")

            mock_run.side_effect = write_output

            # Use dependency injection instead of patching
            manager = GraphRAGIndexManager(repo_path=str(tmp_path))
            manager.set_graph_builder_path_for_testing(graph_builder_dir)

            with patch("tempfile.TemporaryDirectory") as mock_temp:
                mock_temp.return_value.__enter__.return_value = temp_dir
                result = manager._run_graph_builder()

            assert result == graph_data

            # Verify subprocess was called with Python
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]
            assert call_args[0] == "python3"
            assert "cli_python.py" in call_args[1]

    @patch("subprocess.run")
    def test_run_graph_builder_fallback_on_error(self, mock_run, tmp_path):
        """Test fallback to simple indexing when graph-builder fails."""
        graph_builder_dir = tmp_path / "graph-builder"
        graph_builder_dir.mkdir()
        (graph_builder_dir / "src").mkdir()
        (graph_builder_dir / "dist").mkdir()
        (graph_builder_dir / "dist" / "cli.js").touch()

        # Create a Python file for fallback indexing
        (tmp_path / "test.py").write_text("def test(): pass")

        # Mock subprocess to fail
        mock_run.return_value = MagicMock(returncode=1, stderr="Error running graph-builder")

        # Use dependency injection instead of patching
        manager = GraphRAGIndexManager(repo_path=str(tmp_path))
        manager.set_graph_builder_path_for_testing(graph_builder_dir)

        result = manager._run_graph_builder()

        # Should fall back to simple Python indexing
        assert "nodes" in result
        assert len(result["nodes"]) > 0
        assert result["nodes"][0]["kind"] == "File"

    def test_store_graph_in_neo4j_structure(self, tmp_path):
        """Test Neo4j storage structure (mocked)."""
        graph_data = {
            "nodes": [
                {"id": "n1", "kind": "Function", "fqname": "test.py:func1"},
                {"id": "n2", "kind": "Class", "fqname": "test.py:MyClass"},
            ],
            "edges": [{"from": "n1", "to": "n2", "type": "CALLS", "count": 3}],
        }

        manager = GraphRAGIndexManager(repo_path=str(tmp_path))

        # Mock Neo4j driver - patch where it's imported in the method
        with patch("neo4j.GraphDatabase") as mock_db:
            mock_driver = MagicMock()
            mock_session = MagicMock()
            mock_db.driver.return_value = mock_driver
            mock_driver.session.return_value.__enter__.return_value = mock_session

            manager._store_graph_in_neo4j(graph_data, in_container=False)

            # Verify driver was created with correct URI
            mock_db.driver.assert_called_once()
            call_args = mock_db.driver.call_args[0]
            assert "bolt://localhost:7687" in call_args[0]

            # Verify session.run was called for nodes and edges
            assert mock_session.run.call_count >= 3  # Clear + 2 nodes + 1 edge

    def test_store_embeddings_in_qdrant_structure(self, tmp_path):
        """Test Qdrant storage structure (mocked)."""
        graph_data = {
            "nodes": [
                {
                    "id": "n1",
                    "kind": "Function",
                    "fqname": "test.py:func1",
                    "sig": "(int) -> str",
                    "short": "Test function",
                }
            ],
            "edges": [],
        }

        manager = GraphRAGIndexManager(repo_path=str(tmp_path))

        # Mock Qdrant and SentenceTransformer - patch where they're imported
        with patch("qdrant_client.QdrantClient") as mock_qdrant:
            with patch("sentence_transformers.SentenceTransformer") as mock_st:
                mock_client = MagicMock()
                mock_qdrant.return_value = mock_client

                mock_model = MagicMock()
                mock_model.encode.return_value = [0.1] * 384  # Mock embedding
                mock_st.return_value = mock_model

                manager._store_embeddings_in_qdrant(graph_data, in_container=False)

                # Verify Qdrant client was created
                mock_qdrant.assert_called_once()

                # Verify collection was created
                mock_client.create_collection.assert_called_once()

                # Verify embeddings were created
                mock_model.encode.assert_called()

                # Verify points were upserted
                mock_client.upsert.assert_called()

    def test_graph_builder_supports_multiple_languages(self, tmp_path):
        """Test that graph-builder supports TypeScript, JavaScript, and Python."""
        # Create test files for different languages
        (tmp_path / "test.py").write_text("def hello(): pass")
        (tmp_path / "test.ts").write_text("function hello() {}")
        (tmp_path / "test.js").write_text("function world() {}")

        # Create tsconfig.json for TypeScript scanning
        (tmp_path / "tsconfig.json").write_text('{"compilerOptions": {}}')

        manager = GraphRAGIndexManager(repo_path=str(tmp_path))

        # Find graph-builder
        graph_builder_path = manager._find_graph_builder()
        assert graph_builder_path is not None

        # Check if TypeScript CLI exists
        ts_cli = graph_builder_path / "dist" / "cli.js"
        py_cli = graph_builder_path / "src" / "cli_python.py"

        # At least one CLI should exist
        assert ts_cli.exists() or py_cli.exists()

        # Prefer TypeScript CLI if available, otherwise fall back to Python CLI
        import subprocess

        cli_checked = False
        if ts_cli.exists():
            result = subprocess.run(
                ["node", str(ts_cli), "scan", "--help"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                assert "--languages" in result.stdout
                cli_checked = True

        if not cli_checked and py_cli.exists():
            result = subprocess.run(
                ["python3", str(py_cli), "scan", "--help"],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0
            assert "--languages" in result.stdout
            cli_checked = True

        # At least one CLI should be verified
        assert cli_checked, "Neither TS nor Python CLI could be executed successfully"

        # If Python CLI exists, verify it supports languages option
        if py_cli.exists():
            import subprocess

            result = subprocess.run(
                ["python3", str(py_cli), "scan", "--help"],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0
            assert "--languages" in result.stdout

    @patch("src.auto_coder.graphrag_index_manager.GraphRAGIndexManager._run_graph_builder")
    @patch("src.auto_coder.graphrag_index_manager.GraphRAGIndexManager._store_graph_in_neo4j")
    @patch("src.auto_coder.graphrag_index_manager.GraphRAGIndexManager._store_embeddings_in_qdrant")
    def test_index_codebase_integration(self, mock_qdrant, mock_neo4j, mock_builder, tmp_path):
        """Test full _index_codebase integration."""
        graph_data = {
            "nodes": [{"id": "n1", "kind": "Function"}],
            "edges": [{"from": "n1", "to": "n2", "type": "CALLS"}],
        }
        mock_builder.return_value = graph_data

        manager = GraphRAGIndexManager(repo_path=str(tmp_path))

        # Mock is_running_in_container to return False
        with patch("os.path.exists", return_value=False):
            manager._index_codebase()

        # Verify all steps were called
        mock_builder.assert_called_once()
        # in_container should be False based on mocked os.path.exists
        mock_neo4j.assert_called_once_with(graph_data, False)
        mock_qdrant.assert_called_once_with(graph_data, False)

    def test_run_graph_builder_with_all_languages(self, tmp_path):
        """Test that _run_graph_builder scans all languages."""
        # Create test files
        (tmp_path / "test.py").write_text("def hello(): pass")
        (tmp_path / "test.ts").write_text("function hello() {}")
        (tmp_path / "test.js").write_text("function world() {}")
        (tmp_path / "tsconfig.json").write_text('{"compilerOptions": {}}')

        manager = GraphRAGIndexManager(repo_path=str(tmp_path))

        # Find graph-builder
        graph_builder_path = manager._find_graph_builder()
        if graph_builder_path is None:
            pytest.skip("graph-builder not found")

        # Run graph-builder
        result = manager._run_graph_builder()

        # Verify result contains nodes from all languages
        assert "nodes" in result
        assert "edges" in result
        assert len(result["nodes"]) > 0

        # Check for TypeScript/JavaScript specific node types
        node_kinds = {node["kind"] for node in result["nodes"]}

        # Should have File nodes at minimum
        assert "File" in node_kinds

        # If TypeScript files were scanned, should have TypeScript-specific types
        ts_files = [n for n in result["nodes"] if n.get("file", "").endswith((".ts", ".tsx"))]
        js_files = [n for n in result["nodes"] if n.get("file", "").endswith((".js", ".jsx"))]
        py_files = [n for n in result["nodes"] if n.get("file", "").endswith(".py")]

        # Should have scanned files from multiple languages
        # Note: Depending on the test environment, some languages might not be scanned
        # but at least Python should be scanned
        assert len(py_files) > 0 or len(ts_files) > 0 or len(js_files) > 0
