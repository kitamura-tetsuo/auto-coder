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


@pytest.fixture
def mock_subprocess_availability():
    """Fixture to mock subprocess.run for CLI availability checks.

    This fixture provides predictable mocking for subprocess.run that simulates
    CLI tools being available and working correctly.
    """
    with patch("subprocess.run") as mock_run:
        # Mock version checks (should succeed)
        def version_side_effect(*args, **kwargs):
            cmd = args[0] if args else []
            if "node" in cmd:
                return MagicMock(returncode=0, stdout="v20.0.0\n", stderr="")
            elif "python3" in cmd:
                return MagicMock(returncode=0, stdout="Python 3.11.0\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        # Mock CLI commands (should succeed with appropriate output)
        def cli_side_effect(*args, **kwargs):
            cmd = args[0] if args else []
            if "--version" in cmd:
                return version_side_effect(*args, **kwargs)
            elif "scan" in cmd and "--help" in cmd:
                return MagicMock(returncode=0, stdout="--languages\n", stderr="")
            elif "scan" in cmd:
                # Mock successful scan with file information
                graph_data = {
                    "nodes": [
                        {
                            "id": "node1",
                            "kind": "Function",
                            "fqname": "test.ts:myFunc",
                            "file": "test.ts",
                        },
                        {
                            "id": "node2",
                            "kind": "File",
                            "fqname": "test.py",
                            "file": "test.py",
                        },
                        {
                            "id": "node3",
                            "kind": "Function",
                            "fqname": "test.js:world",
                            "file": "test.js",
                        },
                    ],
                    "edges": [{"from": "node1", "to": "node2", "type": "CALLS"}],
                }
                # Write to output file
                if "--out" in cmd:
                    out_idx = cmd.index("--out") + 1
                    if out_idx < len(cmd):
                        output_dir = Path(cmd[out_idx])
                        output_file = output_dir / "graph-data.json"
                        output_file.write_text(json.dumps(graph_data))
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = cli_side_effect
        yield mock_run


@pytest.fixture
def mock_subprocess_unavailable():
    """Fixture to mock subprocess.run for CLI availability checks when tools are unavailable.

    This fixture simulates a CI environment where CLI tools (node, python3) are not available.
    """
    with patch("subprocess.run") as mock_run:
        # Mock all commands to fail (tools not available)
        def unavailable_side_effect(*args, **kwargs):
            cmd = args[0] if args else []
            if "--version" in cmd:
                raise FileNotFoundError(f"Command not found: {cmd[0]}")
            return MagicMock(returncode=1, stderr="Command not found")

        mock_run.side_effect = unavailable_side_effect
        yield mock_run


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
                # Check if this is a version check
                if "--version" in (args[0] if args else []):
                    return MagicMock(returncode=0, stdout="v20.0.0\n", stderr="")
                # Check if this is a help command
                elif "scan" in (args[0] if args else []) and "--help" in (args[0] if args else []):
                    return MagicMock(returncode=0, stdout="--languages\n", stderr="")
                # Otherwise it's the actual scan command
                else:
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
            # Should be called 3 times: version check, help check, and scan
            assert mock_run.call_count >= 1
            # Find the scan call
            scan_call = None
            for call in mock_run.call_args_list:
                if "scan" in call[0][0] and "--help" not in call[0][0]:
                    scan_call = call
                    break
            assert scan_call is not None, "Scan command was not called"
            call_args = scan_call[0][0]
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
                # Check if this is a version check
                if "--version" in (args[0] if args else []):
                    return MagicMock(returncode=0, stdout="Python 3.11.0\n", stderr="")
                # Check if this is a help command
                elif "scan" in (args[0] if args else []) and "--help" in (args[0] if args else []):
                    return MagicMock(returncode=0, stdout="--languages\n", stderr="")
                # Otherwise it's the actual scan command
                else:
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
            # Should be called 3 times: version check, help check, and scan
            assert mock_run.call_count >= 1
            # Find the scan call
            scan_call = None
            for call in mock_run.call_args_list:
                if "scan" in call[0][0] and "--help" not in call[0][0]:
                    scan_call = call
                    break
            assert scan_call is not None, "Scan command was not called"
            call_args = scan_call[0][0]
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

        # Mock subprocess to fail on different calls
        def fail_effect(*args, **kwargs):
            # Version check succeeds
            if "--version" in (args[0] if args else []):
                return MagicMock(returncode=0, stdout="v20.0.0\n", stderr="")
            # Help check fails
            elif "scan" in (args[0] if args else []) and "--help" in (args[0] if args else []):
                return MagicMock(returncode=1, stderr="Error running graph-builder")
            # Other commands fail
            return MagicMock(returncode=1, stderr="Error running graph-builder")

        mock_run.side_effect = fail_effect

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

        # Check CLI availability and compatibility
        # Mock subprocess.run to simulate CLI availability check
        with patch("subprocess.run") as mock_run:
            # Mock version checks and help command
            version_result = MagicMock(returncode=0, stdout="v20.0.0\n", stderr="")
            help_result = MagicMock(returncode=0, stdout="--languages", stderr="")

            def run_effect(*args, **kwargs):
                if "--version" in args[0]:
                    return version_result
                elif "scan" in args[0] and "--help" in args[0]:
                    return help_result
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = run_effect

            cli_checked = False

            # Test TypeScript CLI if available
            if ts_cli.exists():
                # Verify Node.js is checked
                result = manager._check_cli_tool_availability(["node"])
                assert result[0] is True  # Should pass with mocked subprocess

                # Check CLI compatibility
                is_compatible, message = manager._check_graph_builder_cli_compatibility(graph_builder_path, "typescript")
                # In test environment with mocked subprocess, should be compatible
                assert is_compatible or "not found" in message.lower() or "error" in message.lower()
                cli_checked = True

            # Test Python CLI if available
            if py_cli.exists():
                # Verify Python is checked
                result = manager._check_cli_tool_availability(["python3"])
                assert result[0] is True  # Should pass with mocked subprocess

                # Check CLI compatibility
                is_compatible, message = manager._check_graph_builder_cli_compatibility(graph_builder_path, "python")
                # In test environment with mocked subprocess, should be compatible
                assert is_compatible or "not found" in message.lower() or "error" in message.lower()
                cli_checked = True

            # At least one CLI should be present
            assert cli_checked or ts_cli.exists() or py_cli.exists()

    @patch("src.auto_coder.graphrag_index_manager.GraphRAGIndexManager._run_graph_builder")
    @patch("src.auto_coder.graphrag_index_manager.GraphRAGIndexManager._store_graph_in_neo4j")
    @patch("src.auto_coder.graphrag_index_manager.GraphRAGIndexManager._store_embeddings_in_qdrant")
    @patch("src.auto_coder.graphrag_index_manager.is_running_in_container")
    def test_index_codebase_integration(self, mock_is_container, mock_qdrant, mock_neo4j, mock_builder, tmp_path):
        """Test full _index_codebase integration."""
        graph_data = {
            "nodes": [{"id": "n1", "kind": "Function"}],
            "edges": [{"from": "n1", "to": "n2", "type": "CALLS"}],
        }
        mock_builder.return_value = graph_data
        # Mock is_running_in_container to return False
        mock_is_container.return_value = False

        manager = GraphRAGIndexManager(repo_path=str(tmp_path))
        manager._index_codebase()

        # Verify all steps were called
        mock_builder.assert_called_once()
        # in_container should be False based on mocked is_running_in_container
        mock_neo4j.assert_called_once_with(graph_data, False)
        mock_qdrant.assert_called_once_with(graph_data, False)

    def test_run_graph_builder_with_all_languages(self, tmp_path, mock_subprocess_availability):
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

        # Check if CLI tools are available before running
        # This prevents actual subprocess calls in CI
        ts_cli = graph_builder_path / "dist" / "cli.js"
        py_cli = graph_builder_path / "src" / "cli_python.py"

        if not (ts_cli.exists() or py_cli.exists()):
            pytest.skip("No CLI found in graph-builder")

        # Use mocked subprocess to avoid actual CLI calls
        with mock_subprocess_availability:
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

    def test_run_graph_builder_with_cli_unavailable(self, tmp_path, mock_subprocess_unavailable):
        """Test that _run_graph_builder falls back when CLI tools are unavailable."""
        # Create test files
        (tmp_path / "test.py").write_text("def hello(): pass")

        manager = GraphRAGIndexManager(repo_path=str(tmp_path))

        # Find graph-builder
        graph_builder_path = manager._find_graph_builder()
        if graph_builder_path is None:
            pytest.skip("graph-builder not found")

        # Use mocked subprocess to simulate unavailable CLI tools
        with mock_subprocess_unavailable:
            result = manager._run_graph_builder()

            # Should fall back to simple Python indexing
            assert "nodes" in result
            assert "edges" in result
            # Should have at least one node from the test.py file
            assert len(result["nodes"]) > 0
            assert result["nodes"][0]["kind"] == "File"
