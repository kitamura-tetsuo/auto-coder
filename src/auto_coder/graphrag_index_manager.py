"""
GraphRAG Index Manager for Auto-Coder.

Manages indexing of codebase into Neo4j and Qdrant for graphrag_mcp.
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from .logger_config import get_logger

logger = get_logger(__name__)


class GraphRAGIndexManager:
    """Manages indexing of codebase into Neo4j and Qdrant."""

    def __init__(
        self,
        repo_path: Optional[str] = None,
        index_state_file: Optional[str] = None,
    ):
        """Initialize GraphRAG Index Manager.

        Args:
            repo_path: Path to repository to index. If None, uses current directory.
            index_state_file: Path to index state file. If None, uses default location.
        """
        self.repo_path = Path(repo_path) if repo_path else Path.cwd()
        if index_state_file is None:
            # Default to .auto-coder/graphrag_index_state.json in repository
            index_state_file = str(
                self.repo_path / ".auto-coder" / "graphrag_index_state.json"
            )
        self.index_state_file = Path(index_state_file)

    def _get_codebase_hash(self) -> str:
        """Calculate hash of codebase to detect changes.

        Returns:
            SHA256 hash of codebase
        """
        hasher = hashlib.sha256()

        # Get list of tracked files from git
        try:
            result = subprocess.run(
                ["git", "ls-files"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.warning("Failed to get git tracked files, using all files")
                files = list(self.repo_path.rglob("*.py"))
            else:
                files = [
                    self.repo_path / f.strip()
                    for f in result.stdout.split("\n")
                    if f.strip()
                ]
        except Exception as e:
            logger.warning(f"Failed to get git tracked files: {e}, using all files")
            files = list(self.repo_path.rglob("*.py"))

        # Sort files for consistent hashing
        files = sorted(files)

        # Hash file contents
        for file_path in files:
            if not file_path.is_file():
                continue

            try:
                # Hash file path
                hasher.update(str(file_path.relative_to(self.repo_path)).encode())

                # Hash file content
                with open(file_path, "rb") as f:
                    hasher.update(f.read())
            except Exception as e:
                logger.debug(f"Failed to hash file {file_path}: {e}")
                continue

        return hasher.hexdigest()

    def _load_index_state(self) -> dict:
        """Load index state from file.

        Returns:
            Dictionary with index state
        """
        if not self.index_state_file.exists():
            return {}

        try:
            with open(self.index_state_file, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load index state: {e}")
            return {}

    def _save_index_state(self, state: dict) -> None:
        """Save index state to file.

        Args:
            state: Dictionary with index state
        """
        # Create directory if it doesn't exist
        self.index_state_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            with open(self.index_state_file, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save index state: {e}")

    def check_indexed_path(self) -> tuple[bool, Optional[str]]:
        """Check if indexed path matches current repo path.

        Returns:
            Tuple of (matches, indexed_path) where:
            - matches: True if indexed path matches current repo path
            - indexed_path: The path that was indexed, or None if no index exists
        """
        state = self._load_index_state()
        indexed_at = state.get("indexed_at")

        if indexed_at is None:
            return False, None

        # Resolve both paths to absolute paths for comparison
        indexed_path = Path(indexed_at).resolve()
        current_path = self.repo_path.resolve()

        matches = indexed_path == current_path
        return matches, str(indexed_path)

    def is_index_up_to_date(self) -> bool:
        """Check if index is up to date with codebase.

        Returns:
            True if index is up to date, False otherwise
        """
        state = self._load_index_state()

        # Check if index exists
        stored_hash = state.get("codebase_hash")
        if stored_hash is None:
            logger.info("No index state found, index needs to be created")
            return False

        # Check if indexed path matches current repo path
        path_matches, indexed_path = self.check_indexed_path()
        if not path_matches:
            if indexed_path is None:
                logger.info("No indexed path found, index needs to be created")
            else:
                logger.info(
                    f"Indexed path mismatch: indexed={indexed_path}, "
                    f"current={self.repo_path.resolve()}, index needs to be updated"
                )
            return False

        # Check if codebase hash matches
        current_hash = self._get_codebase_hash()
        if current_hash != stored_hash:
            logger.info("Codebase has changed, index needs to be updated")
            return False

        logger.info("Index is up to date")
        return True

    def update_index(self, force: bool = False) -> bool:
        """Update index if needed.

        Args:
            force: Force update even if index is up to date

        Returns:
            True if index was updated successfully, False otherwise
        """
        if not force and self.is_index_up_to_date():
            logger.info("Index is already up to date, skipping update")
            return True

        logger.info("Updating GraphRAG index...")

        # Perform actual indexing
        try:
            self._index_codebase()
        except Exception as e:
            logger.error(f"Failed to index codebase: {e}")
            return False

        # Update the hash to mark as indexed
        current_hash = self._get_codebase_hash()
        state = {
            "codebase_hash": current_hash,
            "indexed_at": str(self.repo_path.resolve()),
        }
        self._save_index_state(state)

        logger.info("Index updated successfully")
        return True

    def _index_codebase(self) -> None:
        """Index codebase into Qdrant and Neo4j using graph-builder.

        This implementation:
        1. Uses graph-builder to analyze Python and TypeScript code
        2. Extracts structured graph data (nodes and edges)
        3. Stores graph data in Neo4j
        4. Creates embeddings and stores in Qdrant
        """
        try:
            from neo4j import GraphDatabase
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, PointStruct, VectorParams
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            import sys

            logger.error(f"Required packages not installed: {e}")
            logger.error(f"Python executable: {sys.executable}")
            logger.error(f"Python path: {sys.path}")
            logger.info(
                "Install with: pip install qdrant-client sentence-transformers neo4j"
            )
            raise

        # Determine if running in container
        def is_running_in_container() -> bool:
            """Check if running inside a Docker container."""
            return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")

        in_container = is_running_in_container()

        # Step 1: Run graph-builder to analyze codebase
        logger.info("Running graph-builder to analyze codebase...")
        graph_data = self._run_graph_builder()

        if not graph_data or (
            not graph_data.get("nodes") and not graph_data.get("edges")
        ):
            logger.warning("No graph data extracted from codebase")
            return

        logger.info(
            f"Extracted {len(graph_data.get('nodes', []))} nodes and {len(graph_data.get('edges', []))} edges"
        )

        # Step 2: Store graph data in Neo4j
        logger.info("Storing graph data in Neo4j...")
        self._store_graph_in_neo4j(graph_data, in_container)

        # Step 3: Create embeddings and store in Qdrant
        logger.info("Creating embeddings and storing in Qdrant...")
        self._store_embeddings_in_qdrant(graph_data, in_container)

        logger.info("Successfully indexed codebase into Neo4j and Qdrant")

    def _run_graph_builder(self) -> dict:
        """Run graph-builder to analyze codebase.

        Returns:
            Dictionary with 'nodes' and 'edges' keys
        """
        # Find graph-builder installation
        graph_builder_path = self._find_graph_builder()
        if not graph_builder_path:
            auto_coder_pkg_dir = Path(__file__).parent
            logger.warning("graph-builder not found in common locations")
            logger.info(
                f"Searched locations: {auto_coder_pkg_dir}/graph_builder, {self.repo_path}/graph-builder, {Path.cwd()}/graph-builder, {Path.home()}/graph-builder"
            )
            logger.info("Falling back to simple Python indexing")
            return self._fallback_python_indexing()

        logger.info(f"Found graph-builder at: {graph_builder_path}")

        # Create temporary output directory
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "graph-data.json"

            # Run graph-builder scan
            try:
                # Check if TypeScript version is available (prefer bundled version)
                ts_cli_bundle = graph_builder_path / "dist" / "cli.bundle.js"
                ts_cli = graph_builder_path / "dist" / "cli.js"

                if ts_cli_bundle.exists():
                    logger.info("Using bundled TypeScript version of graph-builder")
                    cmd = [
                        "node",
                        str(ts_cli_bundle),
                        "scan",
                        "--project",
                        str(self.repo_path),
                        "--out",
                        str(temp_dir),
                        "--languages",
                        "typescript,javascript,python",
                    ]
                elif ts_cli.exists():
                    logger.info("Using TypeScript version of graph-builder")
                    cmd = [
                        "node",
                        str(ts_cli),
                        "scan",
                        "--project",
                        str(self.repo_path),
                        "--out",
                        str(temp_dir),
                        "--languages",
                        "typescript,javascript,python",
                    ]
                else:
                    # Use Python version
                    py_cli = graph_builder_path / "src" / "cli_python.py"
                    if not py_cli.exists():
                        logger.warning(
                            f"graph-builder CLI not found at {ts_cli_bundle}, {ts_cli} or {py_cli}"
                        )
                        return self._fallback_python_indexing()

                    logger.info("Using Python version of graph-builder")
                    cmd = [
                        "python3",
                        str(py_cli),
                        "scan",
                        "--project",
                        str(self.repo_path),
                        "--out",
                        str(temp_dir),
                        "--languages",
                        "typescript,javascript,python",
                    ]

                logger.info(f"Running: {' '.join(cmd)}")
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=300,  # 5 minutes timeout
                )

                # Log stdout for debugging
                if result.stdout:
                    logger.debug(f"graph-builder stdout:\n{result.stdout}")

                if result.returncode != 0:
                    logger.warning(
                        f"graph-builder failed with return code {result.returncode}"
                    )
                    logger.warning(f"stderr: {result.stderr}")
                    if result.stdout:
                        logger.warning(f"stdout: {result.stdout}")
                    return self._fallback_python_indexing()

                # Read output
                if output_path.exists():
                    with open(output_path, "r") as f:
                        data = json.load(f)
                        logger.info(
                            f"Successfully loaded graph data: {len(data.get('nodes', []))} nodes, {len(data.get('edges', []))} edges"
                        )
                        return data
                else:
                    logger.warning(
                        f"graph-builder did not produce output at {output_path}"
                    )
                    logger.warning(
                        f"Output directory contents: {list(Path(temp_dir).iterdir())}"
                    )
                    return self._fallback_python_indexing()

            except subprocess.TimeoutExpired:
                logger.warning("graph-builder timed out after 5 minutes")
                return self._fallback_python_indexing()
            except Exception as e:
                logger.warning(f"Failed to run graph-builder: {e}")
                import traceback

                logger.debug(f"Traceback: {traceback.format_exc()}")
                return self._fallback_python_indexing()

    def _find_graph_builder(self) -> Optional[Path]:
        """Find graph-builder installation.

        Returns:
            Path to graph-builder directory or executable, or None if not found
        """
        # Get auto-coder installation directory (where this file is located)
        auto_coder_pkg_dir = Path(__file__).parent

        # Check common local directory locations
        # Priority: パッケージ内 > 対象リポジトリ内 > カレントディレクトリ > ホームディレクトリ
        candidates = [
            auto_coder_pkg_dir
            / "graph_builder",  # パッケージ内（開発時・pipxインストール時共通）
            self.repo_path / "graph-builder",  # 対象リポジトリ内
            Path.cwd() / "graph-builder",  # カレントディレクトリ
            Path.home() / "graph-builder",  # ホームディレクトリ
        ]

        logger.debug(f"Searching for graph-builder in: {[str(c) for c in candidates]}")

        for candidate in candidates:
            logger.debug(f"Checking: {candidate}")
            if candidate.exists() and candidate.is_dir():
                logger.debug(f"  Directory exists: {candidate}")
                # Check if it has the expected structure
                if (candidate / "src").exists():
                    logger.debug(f"  Found src directory: {candidate / 'src'}")
                    # Also check for dist/cli.bundle.js, dist/cli.js or src/cli_python.py
                    has_ts_cli_bundle = (candidate / "dist" / "cli.bundle.js").exists()
                    has_ts_cli = (candidate / "dist" / "cli.js").exists()
                    has_py_cli = (candidate / "src" / "cli_python.py").exists()
                    logger.debug(
                        f"  TypeScript bundled CLI exists: {has_ts_cli_bundle}"
                    )
                    logger.debug(f"  TypeScript CLI exists: {has_ts_cli}")
                    logger.debug(f"  Python CLI exists: {has_py_cli}")
                    if has_ts_cli_bundle or has_ts_cli or has_py_cli:
                        logger.info(f"Found graph-builder at: {candidate}")
                        return candidate
                    else:
                        logger.debug(f"  No CLI found in {candidate}")
                else:
                    logger.debug(f"  No src directory in {candidate}")
            else:
                logger.debug(f"  Does not exist or not a directory: {candidate}")

        return None

    def _fallback_python_indexing(self) -> dict:
        """Fallback to simple Python file indexing when graph-builder is not available.

        Returns:
            Dictionary with 'nodes' and 'edges' keys
        """
        logger.info("Using fallback Python indexing...")
        python_files = list(self.repo_path.rglob("*.py"))

        nodes = []
        for idx, file_path in enumerate(python_files):
            try:
                content = file_path.read_text(encoding="utf-8")
                if not content.strip():
                    continue

                nodes.append(
                    {
                        "id": f"file_{idx}",
                        "kind": "File",
                        "fqname": str(file_path.relative_to(self.repo_path)),
                        "file": str(file_path.relative_to(self.repo_path)),
                        "content": content,
                    }
                )
            except Exception as e:
                logger.warning(f"Failed to read {file_path}: {e}")

        return {"nodes": nodes, "edges": []}

    def _store_graph_in_neo4j(self, graph_data: dict, in_container: bool) -> None:
        """Store graph data in Neo4j.

        Args:
            graph_data: Dictionary with 'nodes' and 'edges' keys
            in_container: Whether running in a container
        """
        try:
            from neo4j import GraphDatabase
        except ImportError as e:
            import sys

            logger.warning(f"neo4j package not installed, skipping Neo4j indexing: {e}")
            logger.debug(f"Python executable: {sys.executable}")
            logger.debug(f"Python path: {sys.path}")
            return

        # Connect to Neo4j
        # Use container name if in container and connected to same network, otherwise localhost
        neo4j_uri = (
            "bolt://auto-coder-neo4j:7687" if in_container else "bolt://localhost:7687"
        )
        neo4j_user = os.environ.get("NEO4J_USER", "neo4j")
        neo4j_password = os.environ.get("NEO4J_PASSWORD", "password")

        logger.info(f"Connecting to Neo4j at {neo4j_uri}")

        try:
            driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))

            with driver.session() as session:
                # Clear existing data for this repository
                session.run(
                    "MATCH (n) WHERE n.repo_path = $repo_path DETACH DELETE n",
                    repo_path=str(self.repo_path.resolve()),
                )

                # Insert nodes
                nodes = graph_data.get("nodes", [])
                for node in nodes:
                    node_data = dict(node)
                    node_data["repo_path"] = str(self.repo_path.resolve())

                    session.run(
                        """
                        CREATE (n:CodeNode)
                        SET n = $props
                        """,
                        props=node_data,
                    )

                logger.info(f"Inserted {len(nodes)} nodes into Neo4j")

                # Insert edges
                edges = graph_data.get("edges", [])
                for edge in edges:
                    session.run(
                        """
                        MATCH (from:CodeNode {id: $from_id, repo_path: $repo_path})
                        MATCH (to:CodeNode {id: $to_id, repo_path: $repo_path})
                        CREATE (from)-[r:RELATES {type: $type, count: $count}]->(to)
                        """,
                        from_id=edge.get("from"),
                        to_id=edge.get("to"),
                        type=edge.get("type", "UNKNOWN"),
                        count=edge.get("count", 1),
                        repo_path=str(self.repo_path.resolve()),
                    )

                logger.info(f"Inserted {len(edges)} edges into Neo4j")

            driver.close()

        except Exception as e:
            logger.error(f"Failed to store graph in Neo4j: {e}")
            raise

    def _store_embeddings_in_qdrant(self, graph_data: dict, in_container: bool) -> None:
        """Store embeddings in Qdrant.

        Args:
            graph_data: Dictionary with 'nodes' and 'edges' keys
            in_container: Whether running in a container
        """
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, PointStruct, VectorParams
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            import sys

            logger.warning(
                f"Required packages not installed, skipping Qdrant indexing: {e}"
            )
            logger.debug(f"Python executable: {sys.executable}")
            logger.debug(f"Python path: {sys.path}")
            return

        # Connect to Qdrant
        # Use container name if in container and connected to same network, otherwise localhost
        qdrant_url = (
            "http://auto-coder-qdrant:6333" if in_container else "http://localhost:6333"
        )
        logger.info(f"Connecting to Qdrant at {qdrant_url}")
        client = QdrantClient(url=qdrant_url, timeout=10)

        # Collection name
        collection_name = "code_embeddings"

        # Load embedding model
        logger.info("Loading embedding model...")
        model = SentenceTransformer("all-MiniLM-L6-v2")

        # Create or recreate collection
        try:
            client.delete_collection(collection_name)
            logger.info(f"Deleted existing collection: {collection_name}")
        except Exception:
            pass

        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=384, distance=Distance.COSINE),
        )
        logger.info(f"Created collection: {collection_name}")

        # Index nodes
        nodes = graph_data.get("nodes", [])
        points = []

        for idx, node in enumerate(nodes):
            try:
                # Create text representation for embedding
                text_parts = []

                if node.get("fqname"):
                    text_parts.append(f"Name: {node['fqname']}")

                if node.get("sig"):
                    text_parts.append(f"Signature: {node['sig']}")

                if node.get("short"):
                    text_parts.append(f"Summary: {node['short']}")

                # Use content if available (from fallback indexing)
                if node.get("content"):
                    text_parts.append(node["content"][:1000])

                if not text_parts:
                    continue

                text = "\n".join(text_parts)

                # Create embedding
                embedding_result = model.encode(text)
                # Handle both numpy arrays and lists
                embedding = (
                    embedding_result.tolist()
                    if hasattr(embedding_result, "tolist")
                    else embedding_result
                )

                # Create point
                point = PointStruct(
                    id=idx,
                    vector=embedding,
                    payload={
                        "node_id": node.get("id", f"node_{idx}"),
                        "kind": node.get("kind", "Unknown"),
                        "fqname": node.get("fqname", ""),
                        "file": node.get("file", ""),
                        "repo_path": str(self.repo_path.resolve()),
                    },
                )
                points.append(point)

                # Batch insert every 100 nodes
                if len(points) >= 100:
                    client.upsert(collection_name=collection_name, points=points)
                    logger.info(f"Indexed {idx + 1}/{len(nodes)} nodes")
                    points = []

            except Exception as e:
                logger.warning(f"Failed to index node {idx}: {e}")

        # Insert remaining points
        if points:
            client.upsert(collection_name=collection_name, points=points)

        logger.info(f"Successfully indexed {len(nodes)} nodes into Qdrant")

    def ensure_index_up_to_date(self) -> bool:
        """Ensure index is up to date, updating if necessary.

        Returns:
            True if index is up to date (or was successfully updated), False otherwise
        """
        if self.is_index_up_to_date():
            return True

        return self.update_index()
