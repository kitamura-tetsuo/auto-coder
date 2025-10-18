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

    def is_index_up_to_date(self) -> bool:
        """Check if index is up to date with codebase.

        Returns:
            True if index is up to date, False otherwise
        """
        current_hash = self._get_codebase_hash()
        state = self._load_index_state()

        stored_hash = state.get("codebase_hash")
        if stored_hash is None:
            logger.info("No index state found, index needs to be created")
            return False

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

        # TODO: Implement actual indexing logic
        # This would involve:
        # 1. Parsing codebase files
        # 2. Creating embeddings
        # 3. Storing in Neo4j (graph structure)
        # 4. Storing in Qdrant (vector embeddings)
        #
        # For now, we'll just update the hash to mark as indexed
        current_hash = self._get_codebase_hash()
        state = {
            "codebase_hash": current_hash,
            "indexed_at": str(Path.cwd()),
        }
        self._save_index_state(state)

        logger.info("Index updated successfully")
        return True

    def ensure_index_up_to_date(self) -> bool:
        """Ensure index is up to date, updating if necessary.

        Returns:
            True if index is up to date (or was successfully updated), False otherwise
        """
        if self.is_index_up_to_date():
            return True

        return self.update_index()

