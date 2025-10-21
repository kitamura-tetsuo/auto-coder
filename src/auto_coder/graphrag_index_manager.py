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
            index_state_file = str(self.repo_path / ".auto-coder" / "graphrag_index_state.json")
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
        """Index codebase into Qdrant and Neo4j.

        This is a simplified implementation that:
        1. Finds Python files in the repository
        2. Creates embeddings using sentence-transformers
        3. Stores embeddings in Qdrant
        """
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, VectorParams, PointStruct
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            logger.error(f"Required packages not installed: {e}")
            logger.info("Install with: pip install qdrant-client sentence-transformers")
            raise

        # Determine if running in container
        def is_running_in_container() -> bool:
            """Check if running inside a Docker container."""
            return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")

        # Connect to Qdrant
        in_container = is_running_in_container()
        qdrant_url = "http://auto-coder-qdrant:6333" if in_container else "http://localhost:6333"
        logger.info(f"Connecting to Qdrant at {qdrant_url}")
        client = QdrantClient(url=qdrant_url, timeout=10)

        # Collection name
        collection_name = "code_embeddings"

        # Load embedding model
        logger.info("Loading embedding model...")
        model = SentenceTransformer("all-MiniLM-L6-v2")

        # Get Python files
        python_files = list(self.repo_path.rglob("*.py"))
        if not python_files:
            logger.warning("No Python files found in repository")
            return

        logger.info(f"Found {len(python_files)} Python files")

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

        # Index files
        points = []
        for idx, file_path in enumerate(python_files):
            try:
                # Read file content
                content = file_path.read_text(encoding="utf-8")

                # Skip empty files
                if not content.strip():
                    continue

                # Create embedding
                embedding = model.encode(content).tolist()

                # Create point
                point = PointStruct(
                    id=idx,
                    vector=embedding,
                    payload={
                        "file_path": str(file_path.relative_to(self.repo_path)),
                        "content": content[:1000],  # Store first 1000 chars
                        "type": "python_file",
                    }
                )
                points.append(point)

                # Batch insert every 100 files
                if len(points) >= 100:
                    client.upsert(collection_name=collection_name, points=points)
                    logger.info(f"Indexed {idx + 1}/{len(python_files)} files")
                    points = []

            except Exception as e:
                logger.warning(f"Failed to index {file_path}: {e}")

        # Insert remaining points
        if points:
            client.upsert(collection_name=collection_name, points=points)

        logger.info(f"Successfully indexed {len(python_files)} Python files into Qdrant")

    def ensure_index_up_to_date(self) -> bool:
        """Ensure index is up to date, updating if necessary.

        Returns:
            True if index is up to date (or was successfully updated), False otherwise
        """
        if self.is_index_up_to_date():
            return True

        return self.update_index()

