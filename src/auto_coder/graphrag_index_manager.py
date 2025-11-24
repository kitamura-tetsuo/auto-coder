"""
GraphRAG Index Manager for Auto-Coder.

Manages indexing of codebase into Neo4j and Qdrant for graphrag_mcp.
"""

import hashlib
import json
import os
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, cast
from urllib.parse import urlsplit, urlunsplit

from .logger_config import get_logger
from .utils import is_running_in_container

logger = get_logger(__name__)


def _get_env_int(name: str, default: int) -> int:
    """Get an integer value from environment variables with a safe default.

    Any invalid or missing value falls back to ``default``.
    """

    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
        return value
    except Exception:
        return default


def _get_env_flag(name: str, default: bool) -> bool:
    """Get a boolean flag from environment variables.

    Accepts common truthy/falsey strings; anything else falls back to ``default``.
    """

    raw = os.environ.get(name)
    if raw is None:
        return default

    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass
class SnapshotMetadata:
    """Metadata for a single GraphRAG index snapshot for a repository."""

    repo_key: str
    snapshot_id: str
    indexed_at: str  # UTC ISO8601
    repo_path: str
    codebase_hash: Optional[str] = None


@dataclass
class SnapshotCleanupAction:
    """Represents a single snapshot deletion action."""

    repo_key: str
    snapshot_id: str
    reason: str


@dataclass
class SnapshotCleanupResult:
    """Summary of a cleanup run for logging and tests."""

    dry_run: bool
    retention_days: int
    max_snapshots_per_repo: int
    deleted: list[SnapshotCleanupAction]
    total_snapshots_before: int
    total_snapshots_after: int


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
            index_state_file = str(self.repo_path / ".auto-coder" / "graphrag_index_state.json")
        self.index_state_file = Path(index_state_file)
        # Override path for testing - set to None for normal operation
        self._override_graph_builder_path: Optional[Path] = None

        # Snapshot book-keeping for retention/cleanup
        self._current_snapshot_id: Optional[str] = None
        self._current_snapshot_indexed_at: Optional[str] = None
        self._cached_repo_key: Optional[str] = None

        # Batch processing for smart updates
        self._batch_lock = threading.Lock()
        self._pending_files: set[str] = set()
        self._BATCH_DELAY_SECONDS = 2.0  # Wait 2 seconds for batch accumulation
        self._batch_timer: Optional[threading.Timer] = None

    def set_graph_builder_path_for_testing(self, path: Optional[Path]) -> None:
        """Set a custom path for graph-builder (for testing purposes).

        Args:
            path: Path to use for graph-builder, or None to use automatic detection
        """
        self._override_graph_builder_path = path

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
                files = [self.repo_path / f.strip() for f in result.stdout.split("\n") if f.strip()]
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

    def _load_index_state(self) -> dict[str, Any]:
        """Load index state from file.

        Returns:
            Dictionary with index state
        """
        if not self.index_state_file.exists():
            return {}

        try:
            with open(self.index_state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load index state: {e}")
            return {}

        if isinstance(data, dict):
            return cast(dict[str, Any], data)

        logger.warning(f"Invalid index state type {type(data)!r}, resetting to empty dict")
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
                logger.info(f"Indexed path mismatch: indexed={indexed_path}, " f"current={self.repo_path.resolve()}, index needs to be updated")
            return False

        # Check if codebase hash matches
        current_hash = self._get_codebase_hash()
        if current_hash != stored_hash:
            try:
                logger.info("Codebase has changed, index needs to be updated")
            except Exception:
                # Silently ignore logging errors during shutdown
                pass
            return False

        try:
            logger.info("Index is up to date")
        except Exception:
            # Silently ignore logging errors during shutdown
            pass
        return True

    def update_index(self, force: bool = False) -> bool:
        """Update index if needed.

        Args:
            force: Force update even if index is up to date

        Returns:
            True if index was updated successfully, False otherwise
        """
        if not force and self.is_index_up_to_date():
            try:
                logger.info("Index is already up to date, skipping update")
            except Exception:
                # Silently ignore logging errors during shutdown
                pass
            return True

        try:
            logger.info("Updating GraphRAG index...")
        except Exception:
            # Silently ignore logging errors during shutdown
            pass

        # Prepare snapshot metadata for this indexing run
        snapshot_id = uuid.uuid4().hex
        indexed_at_utc = datetime.now(timezone.utc).isoformat()
        self._current_snapshot_id = snapshot_id
        self._current_snapshot_indexed_at = indexed_at_utc

        # Perform actual indexing
        try:
            self._index_codebase()
        except Exception as e:
            # Clear snapshot markers before returning
            self._current_snapshot_id = None
            self._current_snapshot_indexed_at = None
            try:
                logger.error(f"Failed to index codebase: {e}")
            except Exception:
                # Silently ignore logging errors during shutdown
                pass
            return False

        # Clear snapshot markers (storage helpers no longer need them)
        self._current_snapshot_id = None
        self._current_snapshot_indexed_at = None

        # Update the hash to mark as indexed and append snapshot metadata
        current_hash = self._get_codebase_hash()
        state = self._load_index_state()
        state["codebase_hash"] = current_hash
        state["indexed_at"] = str(self.repo_path.resolve())

        snapshots = self._load_snapshots_from_state(state)
        repo_key = self._build_repo_key()
        snapshots.append(
            SnapshotMetadata(
                repo_key=repo_key,
                snapshot_id=snapshot_id,
                indexed_at=indexed_at_utc,
                repo_path=str(self.repo_path.resolve()),
                codebase_hash=current_hash,
            )
        )
        # Keep snapshots sorted oldest-first for predictable cleanup behaviour
        snapshots.sort(key=lambda s: s.indexed_at)
        state["snapshots"] = [self._snapshot_to_dict(s) for s in snapshots]
        self._save_index_state(state)

        try:
            logger.info(f"Index updated successfully (snapshot_id={snapshot_id}, repo_key={repo_key})")
        except Exception:
            # Silently ignore logging errors during shutdown
            pass

        # Optionally run cleanup after update
        if _get_env_flag("GRAPHRAG_CLEANUP_ON_UPDATE", True):
            try:
                self.cleanup_snapshots()
            except Exception as e:
                try:
                    logger.warning(f"GraphRAG cleanup after index update failed: {e}")
                except Exception:
                    pass

        return True

    def _index_codebase(self) -> None:
        """Index codebase into Qdrant and Neo4j using graph-builder.

        This implementation:
        1. Uses graph-builder to analyze Python and TypeScript code
        2. Extracts structured graph data (nodes and edges)
        3. Stores graph data in Neo4j
        4. Creates embeddings and stores in Qdrant
        """
        # Dependencies for Neo4j/Qdrant/SentenceTransformer are imported lazily in helper methods
        # to allow operation (and testing) without those optional packages installed.

        # Determine if running in container using robust detection
        in_container = is_running_in_container()

        # Step 1: Run graph-builder to analyze codebase
        logger.info("Running graph-builder to analyze codebase...")
        graph_data = self._run_graph_builder()

        if not graph_data or (not graph_data.get("nodes") and not graph_data.get("edges")):
            logger.warning("No graph data extracted from codebase")
            return

        logger.info(f"Extracted {len(graph_data.get('nodes', []))} nodes and {len(graph_data.get('edges', []))} edges")

        # Step 2: Store graph data in Neo4j
        logger.info("Storing graph data in Neo4j...")
        self._store_graph_in_neo4j(graph_data, in_container)

        # Step 3: Create embeddings and store in Qdrant
        logger.info("Creating embeddings and storing in Qdrant...")
        self._store_embeddings_in_qdrant(graph_data, in_container)

        logger.info("Successfully indexed codebase into Neo4j and Qdrant")

    def _check_cli_tool_availability(self, command: list[str]) -> tuple[bool, Optional[str]]:
        """Check if a CLI tool is available and working.

        Args:
            command: Command to check (e.g., ["node", "--version"])

        Returns:
            Tuple of (is_available, version_info) where version_info is version string or error message
        """
        try:
            result = subprocess.run(
                command + ["--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                version = result.stdout.strip() or result.stderr.strip()
                return True, version
            return False, f"Command failed with return code {result.returncode}"
        except subprocess.TimeoutExpired:
            return False, "Command timed out"
        except FileNotFoundError:
            return False, "Command not found"
        except Exception as e:
            return False, f"Error: {e}"

    def _check_graph_builder_cli_compatibility(self, graph_builder_path: Path, cli_type: str) -> tuple[bool, str]:
        """Check if a graph-builder CLI is compatible and working.

        Args:
            graph_builder_path: Path to graph-builder directory
            cli_type: Type of CLI ("typescript", "python")

        Returns:
            Tuple of (is_compatible, message)
        """
        try:
            if cli_type == "typescript":
                # Check for node
                node_available, node_version = self._check_cli_tool_availability(["node"])
                if not node_available:
                    return False, f"Node.js not available: {node_version}"

                # Check TypeScript CLI
                ts_cli_bundle = graph_builder_path / "dist" / "cli.bundle.js"
                ts_cli = graph_builder_path / "dist" / "cli.js"
                cli_path = ts_cli_bundle if ts_cli_bundle.exists() else ts_cli

                if not cli_path.exists():
                    return False, f"TypeScript CLI not found at {cli_path}"

                # Test the CLI
                result = subprocess.run(
                    ["node", str(cli_path), "scan", "--help"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode != 0:
                    return False, f"CLI test failed: {result.stderr}"

                return True, f"TypeScript CLI compatible (Node {node_version})"

            elif cli_type == "python":
                # Check for python3
                python_available, python_version = self._check_cli_tool_availability(["python3"])
                if not python_available:
                    return False, f"Python3 not available: {python_version}"

                # Check Python CLI
                py_cli = graph_builder_path / "src" / "cli_python.py"
                if not py_cli.exists():
                    return False, f"Python CLI not found at {py_cli}"

                # Test the CLI
                result = subprocess.run(
                    ["python3", str(py_cli), "scan", "--help"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode != 0:
                    return False, f"CLI test failed: {result.stderr}"

                return True, f"Python CLI compatible (Python {python_version})"

            return False, f"Unknown CLI type: {cli_type}"

        except Exception as e:
            return False, f"Compatibility check failed: {e}"

    def _run_graph_builder(self) -> dict[str, Any]:
        """Run graph-builder to analyze codebase.

        Returns:
            Dictionary with 'nodes' and 'edges' keys
        """
        # In pytest, avoid spawning external graph-builder; use fallback for speed/stability
        import os

        if os.environ.get("PYTEST_CURRENT_TEST"):
            # However, if a test explicitly sets an override path, honor it and run real CLI
            if getattr(self, "_override_graph_builder_path", None) is None:
                logger.info("Detected pytest environment; using fallback Python indexing instead of graph-builder")
                return self._fallback_python_indexing()

        # Find graph-builder installation
        graph_builder_path = self._find_graph_builder()
        if not graph_builder_path:
            auto_coder_pkg_dir = Path(__file__).parent
            logger.warning("graph-builder not found in common locations")
            logger.info(f"Searched locations: {auto_coder_pkg_dir}/graph_builder, {self.repo_path}/graph-builder, {Path.cwd()}/graph-builder, {Path.home()}/graph-builder")
            logger.info("Falling back to simple Python indexing")
            return self._fallback_python_indexing()

        logger.info(f"Found graph-builder at: {graph_builder_path}")

        # Check CLI compatibility before running
        ts_cli_bundle = graph_builder_path / "dist" / "cli.bundle.js"
        ts_cli = graph_builder_path / "dist" / "cli.js"
        py_cli = graph_builder_path / "src" / "cli_python.py"

        # Try TypeScript CLI first (preferred)
        if ts_cli_bundle.exists() or ts_cli.exists():
            is_compatible, message = self._check_graph_builder_cli_compatibility(graph_builder_path, "typescript")
            if is_compatible:
                logger.info(f"TypeScript CLI validation: {message}")
            else:
                logger.warning(f"TypeScript CLI not compatible: {message}")
                # Try Python CLI as fallback
                if py_cli.exists():
                    is_compatible, message = self._check_graph_builder_cli_compatibility(graph_builder_path, "python")
                    if is_compatible:
                        logger.info(f"Python CLI validation: {message}")
                    else:
                        logger.warning(f"Python CLI not compatible: {message}, falling back to simple indexing")
                        return self._fallback_python_indexing()
                else:
                    logger.warning("No compatible CLI found, falling back to simple indexing")
                    return self._fallback_python_indexing()
        elif py_cli.exists():
            is_compatible, message = self._check_graph_builder_cli_compatibility(graph_builder_path, "python")
            if is_compatible:
                logger.info(f"Python CLI validation: {message}")
            else:
                logger.warning(f"Python CLI not compatible: {message}, falling back to simple indexing")
                return self._fallback_python_indexing()
        else:
            logger.warning("No CLI found in graph-builder, falling back to simple indexing")
            return self._fallback_python_indexing()

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
                        logger.warning(f"graph-builder CLI not found at {ts_cli_bundle}, {ts_cli} or {py_cli}")
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
                    logger.warning(f"graph-builder failed with return code {result.returncode}")
                    logger.warning(f"stderr: {result.stderr}")
                    if result.stdout:
                        logger.warning(f"stdout: {result.stdout}")
                    return self._fallback_python_indexing()

                # Read output
                if output_path.exists():
                    with open(output_path, "r") as f:
                        data = cast(dict[str, Any], json.load(f))
                        logger.info(f"Successfully loaded graph data: {len(data.get('nodes', []))} nodes, {len(data.get('edges', []))} edges")
                        return data
                else:
                    logger.warning(f"graph-builder did not produce output at {output_path}")
                    logger.warning(f"Output directory contents: {list(Path(temp_dir).iterdir())}")
                    return self._fallback_python_indexing()

            except subprocess.TimeoutExpired:
                logger.warning("graph-builder timed out after 5 minutes")
                return self._fallback_python_indexing()
            except Exception as e:
                logger.warning(f"Failed to run graph-builder: {e}")
                import traceback

                logger.debug(f"Traceback: {traceback.format_exc()}")
                return self._fallback_python_indexing()

    def _get_auto_coder_package_dir(self) -> Path:
        """Get the auto-coder package directory where this module is located.

        This is a separate method to make testing easier.

        Returns:
            Path to the auto-coder package directory
        """
        return Path(__file__).parent

    def _get_graph_builder_candidates(self) -> list[Path]:
        """Get list of candidate paths to search for graph-builder.

        Returns:
            List of candidate Path objects to check
        """
        auto_coder_pkg_dir = self._get_auto_coder_package_dir()

        # Check common local directory locations
        # Priority: In-package > Target repository > Current directory > Home directory
        candidates = [
            auto_coder_pkg_dir / "graph_builder",  # In-package (for both development and pipx installs)
            self.repo_path / "graph-builder",  # In the target repository
            Path.cwd() / "graph-builder",  # Current directory
            Path.home() / "graph-builder",  # Home directory
        ]

        return candidates

    def _validate_graph_builder_path(self, candidate: Path) -> tuple[bool, str]:
        """Validate if a path contains a valid graph-builder installation.

        Args:
            candidate: Path to validate

        Returns:
            Tuple of (is_valid, reason) where reason explains why it's valid/invalid
        """
        if not candidate.exists():
            return False, f"Path does not exist: {candidate}"

        if not candidate.is_dir():
            return False, f"Path is not a directory: {candidate}"

        if not (candidate / "src").exists():
            return False, f"No 'src' directory found in: {candidate}"

        # Check for at least one CLI
        has_ts_cli_bundle = (candidate / "dist" / "cli.bundle.js").exists()
        has_ts_cli = (candidate / "dist" / "cli.js").exists()
        has_py_cli = (candidate / "src" / "cli_python.py").exists()

        if not (has_ts_cli_bundle or has_ts_cli or has_py_cli):
            return (
                False,
                f"No CLI found (expected dist/cli.bundle.js, dist/cli.js, or src/cli_python.py)",
            )

        valid_clis = []
        if has_ts_cli_bundle:
            valid_clis.append("dist/cli.bundle.js")
        if has_ts_cli:
            valid_clis.append("dist/cli.js")
        if has_py_cli:
            valid_clis.append("src/cli_python.py")

        return True, f"Valid graph-builder with CLIs: {', '.join(valid_clis)}"

    def _find_graph_builder(self) -> Optional[Path]:
        """Find graph-builder installation.

        Returns:
            Path to graph-builder directory or executable, or None if not found
        """
        # Use override path if set (for testing)
        if self._override_graph_builder_path is not None:
            is_valid, reason = self._validate_graph_builder_path(self._override_graph_builder_path)
            if is_valid:
                logger.info(f"Using test-specified graph-builder path: {self._override_graph_builder_path}")
                return self._override_graph_builder_path
            else:
                logger.warning(f"Test-specified graph-builder path is invalid: {reason}")
                return None

        candidates = self._get_graph_builder_candidates()

        logger.debug(f"Searching for graph-builder in: {[str(c) for c in candidates]}")

        for candidate in candidates:
            is_valid, reason = self._validate_graph_builder_path(candidate)
            logger.debug(f"Checking {candidate}: {reason}")

            if is_valid:
                logger.info(f"Found graph-builder at: {candidate}")
                return candidate

        logger.warning("graph-builder not found in any common location")
        logger.debug(f"Searched locations: {[str(c) for c in candidates]}")
        return None

    def _fallback_python_indexing(self) -> dict[str, Any]:
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
        neo4j_uri = "bolt://auto-coder-neo4j:7687" if in_container else "bolt://localhost:7687"
        neo4j_user = os.environ.get("NEO4J_USER", "neo4j")
        neo4j_password = os.environ.get("NEO4J_PASSWORD", "password")

        logger.info(f"Connecting to Neo4j at {neo4j_uri}")

        # Calculate repository hash for labels
        repo_path_str = str(self.repo_path.resolve())
        repo_hash = hashlib.md5(repo_path_str.encode()).hexdigest()[:8]
        repo_label = f"Repo_{repo_hash}"
        repo_key = self._build_repo_key()
        snapshot_id = self._current_snapshot_id
        snapshot_indexed_at = self._current_snapshot_indexed_at

        logger.info(f"Using repository label: {repo_label}")

        try:
            driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))

            with driver.session() as session:
                # Clear existing data for this repository (both labeled and unlabeled)
                # Delete nodes with repo label
                session.run(f"MATCH (n:{repo_label}:CodeNode) DETACH DELETE n")

                # Also delete unlabeled nodes matching this repo_path for cleanup
                session.run(
                    "MATCH (n:CodeNode) WHERE n.repo_path = $repo_path DETACH DELETE n",
                    repo_path=repo_path_str,
                )

                # Insert nodes
                nodes = graph_data.get("nodes", [])
                for node in nodes:
                    node_data = dict(node)
                    node_data["repo_path"] = repo_path_str
                    node_data["repo_hash"] = repo_hash
                    node_data["repo_label"] = repo_label
                    node_data["repo_key"] = repo_key
                    if snapshot_id:
                        node_data["snapshot_id"] = snapshot_id
                    if snapshot_indexed_at:
                        node_data["snapshot_indexed_at"] = snapshot_indexed_at

                    session.run(
                        """
                        CREATE (n:CodeNode)
                        SET n = $props
                        """,
                        props=node_data,
                    )

                logger.info(f"Inserted {len(nodes)} nodes with label {repo_label} into Neo4j")

                # Insert edges
                edges = graph_data.get("edges", [])
                for edge in edges:
                    session.run(
                        f"""
                        MATCH (from:{repo_label}:CodeNode {{id: $from_id, repo_path: $repo_path}})
                        MATCH (to:{repo_label}:CodeNode {{id: $to_id, repo_path: $repo_path}})
                        CREATE (from)-[r:RELATES {{type: $type, count: $count, repo_hash: $repo_hash, repo_key: $repo_key, snapshot_id: $snapshot_id}}]->(to)
                        """,
                        from_id=edge.get("from"),
                        to_id=edge.get("to"),
                        type=edge.get("type", "UNKNOWN"),
                        count=edge.get("count", 1),
                        repo_path=repo_path_str,
                        repo_hash=repo_hash,
                        repo_key=repo_key,
                        snapshot_id=snapshot_id,
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

            logger.warning(f"Required packages not installed, skipping Qdrant indexing: {e}")
            logger.debug(f"Python executable: {sys.executable}")
            logger.debug(f"Python path: {sys.path}")
            return

        # Connect to Qdrant and store embeddings (best-effort; skip on any failure)
        try:
            # Use container name if in container and connected to same network, otherwise localhost
            qdrant_url = "http://auto-coder-qdrant:6333" if in_container else "http://localhost:6333"
            logger.info(f"Connecting to Qdrant at {qdrant_url}")
            client = QdrantClient(url=qdrant_url, timeout=2)

            # Collection name
            collection_name = "code_embeddings"

            # Calculate repository hash for labels
            repo_path_str = str(self.repo_path.resolve())
            repo_hash = hashlib.md5(repo_path_str.encode()).hexdigest()[:8]
            repo_label = f"Repo_{repo_hash}"
            repo_key = self._build_repo_key()
            snapshot_id = self._current_snapshot_id
            snapshot_indexed_at = self._current_snapshot_indexed_at

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
                    embedding_list = embedding_result.tolist() if hasattr(embedding_result, "tolist") else embedding_result
                    # Ensure it's a list of floats for type checking
                    embedding: list[float] = cast(list[float], embedding_list)

                    # Create point
                    point = PointStruct(
                        id=idx,
                        vector=embedding,
                        payload={
                            "node_id": node.get("id", f"node_{idx}"),
                            "kind": node.get("kind", "Unknown"),
                            "fqname": node.get("fqname", ""),
                            "file": node.get("file", ""),
                            "repo_path": repo_path_str,
                            "repo_hash": repo_hash,
                            "repo_label": repo_label,
                            "repo_key": repo_key,
                            "snapshot_id": snapshot_id,
                            "snapshot_indexed_at": snapshot_indexed_at,
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
        except Exception as e:
            logger.warning(f"Skipping Qdrant indexing due to error: {e}")

    def _build_repo_key(self) -> str:
        """Build a stable key for this repository for snapshot grouping.

        The key uses the absolute repo path and, if available, the sanitized
        ``remote.origin.url`` with credentials stripped.
        """

        if self._cached_repo_key is not None:
            return self._cached_repo_key

        repo_path_str = str(self.repo_path.resolve())
        remote = None

        try:
            result = subprocess.run(
                ["git", "-C", repo_path_str, "config", "--get", "remote.origin.url"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                remote_raw = result.stdout.strip()
                if remote_raw:
                    parts = urlsplit(remote_raw)
                    netloc = parts.netloc.split("@", 1)[1] if "@" in parts.netloc else parts.netloc
                    remote = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
        except Exception:
            # If git or remote configuration is unavailable, fall back to path-only key
            remote = None

        key = f"{repo_path_str}|{remote}" if remote else repo_path_str
        self._cached_repo_key = key
        return key

    def _load_snapshots_from_state(self, state: dict[str, Any]) -> list[SnapshotMetadata]:
        """Deserialize snapshot metadata from index state."""

        raw = state.get("snapshots") or []
        snapshots: list[SnapshotMetadata] = []
        if not isinstance(raw, list):
            return snapshots

        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                repo_key = str(item.get("repo_key") or "")
                snapshot_id = str(item.get("snapshot_id") or "")
                indexed_at = str(item.get("indexed_at") or "")
                repo_path = str(item.get("repo_path") or str(self.repo_path.resolve()))
            except Exception:
                continue

            if not (repo_key and snapshot_id and indexed_at):
                continue

            snapshots.append(
                SnapshotMetadata(
                    repo_key=repo_key,
                    snapshot_id=snapshot_id,
                    indexed_at=indexed_at,
                    repo_path=repo_path,
                    codebase_hash=item.get("codebase_hash"),
                )
            )

        return snapshots

    def _snapshot_to_dict(self, snapshot: SnapshotMetadata) -> dict[str, Any]:
        return {
            "repo_key": snapshot.repo_key,
            "snapshot_id": snapshot.snapshot_id,
            "indexed_at": snapshot.indexed_at,
            "repo_path": snapshot.repo_path,
            "codebase_hash": snapshot.codebase_hash,
        }

    def cleanup_snapshots(
        self,
        dry_run: bool = False,
        retention_days: Optional[int] = None,
        max_snapshots_per_repo: Optional[int] = None,
    ) -> SnapshotCleanupResult:
        """Apply retention policy to GraphRAG index snapshots.

        Per ``repo_key`` this will:
        - Delete snapshots strictly older than ``retention_days``.
        - Ensure at most ``max_snapshots_per_repo`` snapshots remain.
        - Always keep the newest snapshot.
        """

        state = self._load_index_state()
        snapshots = self._load_snapshots_from_state(state)
        total_before = len(snapshots)

        retention_days_value = retention_days if retention_days is not None else _get_env_int("GRAPHRAG_RETENTION_DAYS", 7)
        max_per_repo_value = max_snapshots_per_repo if max_snapshots_per_repo is not None else _get_env_int("GRAPHRAG_MAX_SNAPSHOTS_PER_REPO", 9)
        if max_per_repo_value < 1:
            max_per_repo_value = 1

        if total_before == 0:
            return SnapshotCleanupResult(
                dry_run=dry_run,
                retention_days=retention_days_value,
                max_snapshots_per_repo=max_per_repo_value,
                deleted=[],
                total_snapshots_before=0,
                total_snapshots_after=0,
            )

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=retention_days_value)

        by_repo: dict[str, list[SnapshotMetadata]] = {}
        for snap in snapshots:
            by_repo.setdefault(snap.repo_key, []).append(snap)

        to_delete: list[SnapshotMetadata] = []
        actions: list[SnapshotCleanupAction] = []

        for repo_key, repo_snaps in by_repo.items():
            if len(repo_snaps) <= 1:
                # Always keep at least one snapshot per repo
                continue

            try:
                sorted_snaps = sorted(repo_snaps, key=lambda s: datetime.fromisoformat(s.indexed_at))
            except Exception:
                sorted_snaps = sorted(repo_snaps, key=lambda s: s.indexed_at)

            n = len(sorted_snaps)
            delete_indices: set[int] = set()
            reasons: dict[int, str] = {}

            # Time-based deletions (excluding newest snapshot)
            for idx, snap in enumerate(sorted_snaps[:-1]):
                try:
                    ts = datetime.fromisoformat(snap.indexed_at)
                except Exception:
                    # If timestamp cannot be parsed, skip time-based evaluation for this snapshot
                    continue

                if ts < cutoff:
                    delete_indices.add(idx)
                    reasons[idx] = "time"

            # Count-based deletions (still excluding newest snapshot)
            remaining = n - len(delete_indices)
            if remaining > max_per_repo_value:
                extra = remaining - max_per_repo_value
                for idx in range(n - 1):
                    if extra <= 0:
                        break
                    if idx in delete_indices:
                        continue
                    delete_indices.add(idx)
                    reasons[idx] = "time+count" if idx in reasons else "count"
                    extra -= 1

            for idx in sorted(delete_indices):
                snap = sorted_snaps[idx]
                to_delete.append(snap)
                actions.append(
                    SnapshotCleanupAction(
                        repo_key=repo_key,
                        snapshot_id=snap.snapshot_id,
                        reason=reasons.get(idx, "unknown"),
                    )
                )

        if not to_delete:
            try:
                logger.info("GraphRAG cleanup: no snapshots to delete " f"(total_snapshots={total_before}, retention_days={retention_days_value}, " f"max_per_repo={max_per_repo_value})")
            except Exception:
                pass

            return SnapshotCleanupResult(
                dry_run=dry_run,
                retention_days=retention_days_value,
                max_snapshots_per_repo=max_per_repo_value,
                deleted=[],
                total_snapshots_before=total_before,
                total_snapshots_after=total_before,
            )

        if dry_run:
            for action in actions:
                try:
                    logger.info("GraphRAG cleanup (dry-run): would delete snapshot " f"repo_key={action.repo_key}, snapshot_id={action.snapshot_id}, reason={action.reason}")
                except Exception:
                    pass

            total_after = total_before
        else:
            for snap, action in zip(to_delete, actions):
                try:
                    logger.info("GraphRAG cleanup: deleting snapshot " f"repo_key={action.repo_key}, snapshot_id={action.snapshot_id}, reason={action.reason}")
                except Exception:
                    pass

                try:
                    self._delete_snapshot_from_stores(snap)
                except Exception as e:
                    try:
                        logger.warning("GraphRAG cleanup: failed to delete snapshot " f"{action.snapshot_id}: {e}")
                    except Exception:
                        pass

            remaining_snaps = [s for s in snapshots if s not in to_delete]
            state["snapshots"] = [self._snapshot_to_dict(s) for s in remaining_snaps]
            self._save_index_state(state)
            total_after = len(remaining_snaps)

        try:
            logger.info(
                "GraphRAG cleanup %s: deleted %d/%d snapshots " "(retention_days=%d, max_per_repo=%d, remaining=%d)",
                "dry-run" if dry_run else "executed",
                len(actions),
                total_before,
                retention_days_value,
                max_per_repo_value,
                total_after,
            )
        except Exception:
            pass

        return SnapshotCleanupResult(
            dry_run=dry_run,
            retention_days=retention_days_value,
            max_snapshots_per_repo=max_per_repo_value,
            deleted=actions,
            total_snapshots_before=total_before,
            total_snapshots_after=total_after,
        )

    def _delete_snapshot_from_stores(self, snapshot: SnapshotMetadata) -> None:
        """Delete snapshot data from Neo4j and Qdrant.

        All operations are best-effort; failures are logged and do not raise.
        """

        try:
            self._delete_snapshot_from_neo4j(snapshot)
        except Exception as e:
            try:
                logger.warning("GraphRAG cleanup: Neo4j deletion failed for snapshot " f"{snapshot.snapshot_id}: {e}")
            except Exception:
                pass

        try:
            self._delete_snapshot_from_qdrant(snapshot)
        except Exception as e:
            try:
                logger.warning("GraphRAG cleanup: Qdrant deletion failed for snapshot " f"{snapshot.snapshot_id}: {e}")
            except Exception:
                pass

    def _delete_snapshot_from_neo4j(self, snapshot: SnapshotMetadata) -> None:
        """Delete nodes/edges for a snapshot from Neo4j (best-effort)."""

        try:
            from neo4j import GraphDatabase
        except ImportError:
            # Optional dependency; nothing to do if missing.
            return

        in_container = is_running_in_container()
        neo4j_uri = "bolt://auto-coder-neo4j:7687" if in_container else "bolt://localhost:7687"
        neo4j_user = os.environ.get("NEO4J_USER", "neo4j")
        neo4j_password = os.environ.get("NEO4J_PASSWORD", "password")

        repo_path_str = snapshot.repo_path
        repo_hash = hashlib.md5(repo_path_str.encode()).hexdigest()[:8]
        repo_label = f"Repo_{repo_hash}"

        driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
        try:
            with driver.session() as session:
                if snapshot.snapshot_id:
                    session.run(
                        f"""
                        MATCH (n:{repo_label}:CodeNode {{repo_path: $repo_path, snapshot_id: $snapshot_id}})
                        DETACH DELETE n
                        """,
                        repo_path=repo_path_str,
                        snapshot_id=snapshot.snapshot_id,
                    )
                    session.run(
                        """
                        MATCH ()-[r:RELATES {repo_hash: $repo_hash, snapshot_id: $snapshot_id}]-()
                        DELETE r
                        """,
                        repo_hash=repo_hash,
                        snapshot_id=snapshot.snapshot_id,
                    )
                else:
                    # Fallback: delete by repo_path only
                    session.run(
                        f"MATCH (n:{repo_label}:CodeNode {{repo_path: $repo_path}}) DETACH DELETE n",
                        repo_path=repo_path_str,
                    )
        finally:
            driver.close()

    def _delete_snapshot_from_qdrant(self, snapshot: SnapshotMetadata) -> None:
        """Delete snapshot vectors from Qdrant (best-effort).

        The current integration uses a single collection for all repos.
        Deleting the whole collection keeps behaviour consistent with indexing,
        which recreates the collection on every run.
        """

        try:
            from qdrant_client import QdrantClient
        except ImportError:
            return

        in_container = is_running_in_container()
        qdrant_url = "http://auto-coder-qdrant:6333" if in_container else "http://localhost:6333"
        collection_name = "code_embeddings"

        client = QdrantClient(url=qdrant_url, timeout=2)

        try:
            client.delete_collection(collection_name)
        except Exception:
            # Ignore failures; cleanup is best-effort.
            pass

    def ensure_index_up_to_date(self) -> bool:
        """Ensure index is up to date, updating if necessary.

        Returns:
            True if index is up to date (or was successfully updated), False otherwise
        """
        if self.is_index_up_to_date():
            return True

        return self.update_index()

    def smart_update_trigger(self, changed_files: list[str]) -> bool:
        """
        Smart update logic that avoids unnecessary full re-indexing.
        Only re-index when significant code structure changes occur.

        Args:
            changed_files: List of file paths that have changed

        Returns:
            True if update is not needed or completed successfully, False if update failed
        """
        # Check if any changed file is a significant code file
        significant_patterns = [
            "*.py",
            "*.ts",
            "*.js",  # Code files
            "requirements.txt",
            "package.json",
            "pyproject.toml",  # Config files
        ]

        has_significant_changes = any(any(changed_file.endswith(pattern[1:]) for pattern in significant_patterns) for changed_file in changed_files)

        if not has_significant_changes:
            logger.debug("No significant code changes detected, skipping GraphRAG update")
            return True  # Success (no update needed)

        # Only then proceed with full update
        return self.update_index()

    def batch_update_trigger(self, file_batch: list[str], max_batch_size: int = 5) -> None:
        """
        Batch multiple file changes to prevent excessive updates.
        Waits for a quiet period before triggering update.

        Args:
            file_batch: List of file paths that have changed
            max_batch_size: Maximum number of files to batch before processing immediately
        """
        # Update pending files and decide whether to process now under lock
        with self._batch_lock:
            self._pending_files.update(file_batch)

            logger.debug(f"Added {len(file_batch)} files to batch, total pending: {len(self._pending_files)}")

            # Cancel existing timer if any
            if self._batch_timer is not None:
                self._batch_timer.cancel()

            process_now = len(self._pending_files) >= max_batch_size

            if not process_now:
                # Schedule delayed processing; timer thread must be daemon to avoid blocking interpreter exit
                self._batch_timer = threading.Timer(self._BATCH_DELAY_SECONDS, self._process_pending_batch)
                try:
                    self._batch_timer.daemon = True
                except Exception:
                    pass
                self._batch_timer.start()
                logger.debug(f"Scheduled batch processing in {self._BATCH_DELAY_SECONDS} seconds")

        if process_now:
            # Call outside lock to avoid deadlock with _process_pending_batch (which acquires the same lock)
            logger.debug(f"Batch size ({len(self._pending_files)}) >= max_batch_size ({max_batch_size}), processing immediately")
            self._process_pending_batch()

    def _process_pending_batch(self) -> None:
        """
        Process the pending batch of files.
        This is called either when the batch is full or the delay timer expires.
        """
        with self._batch_lock:
            if not self._pending_files:
                return

            files_to_process = list(self._pending_files)
            self._pending_files.clear()
            logger.debug(f"Processing batch of {len(files_to_process)} files")

        # Process outside of lock to avoid blocking
        try:
            success = self.smart_update_trigger(files_to_process)
            if success:
                logger.debug(f"Batch update successful for {len(files_to_process)} files")
            else:
                logger.warning(f"Batch update failed for {len(files_to_process)} files")
        except Exception as e:
            logger.error(f"Error during batch update: {e}")

    def cleanup_batch_timer(self) -> None:
        """Clean up the batch timer. Call this when shutting down."""
        with self._batch_lock:
            if self._batch_timer is not None:
                self._batch_timer.cancel()
                self._batch_timer = None

    def lightweight_update_check(self) -> bool:
        """
        Lightweight check to see if GraphRAG update is needed.
        Used by file watchers to avoid full hash computation.

        Returns:
            True if update is needed or completed successfully, False if update should be skipped
        """
        # Quick check if files are code files
        # If only non-code files changed, skip update
        if not self._has_recent_code_changes():
            return True  # Skip update

        # Proceed with full update
        return self.update_index()

    def _has_recent_code_changes(self) -> bool:
        """
        Check if there have been recent code changes.
        This is a lightweight check to avoid expensive hash computation.

        Returns:
            True if there may be code changes, False if only non-code files exist
        """
        try:
            # Get list of tracked files from git
            result = subprocess.run(
                ["git", "ls-files"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                # If git fails, assume there are code changes
                return True

            files = [f.strip() for f in result.stdout.split("\n") if f.strip()]

            # Check if any tracked files are code files
            code_extensions = (".py", ".ts", ".js")
            for file_path in files:
                if file_path.endswith(code_extensions):
                    return True

            return False
        except Exception as e:
            logger.debug(f"Failed to check for code changes: {e}")
            # If check fails, assume there are code changes
            return True
