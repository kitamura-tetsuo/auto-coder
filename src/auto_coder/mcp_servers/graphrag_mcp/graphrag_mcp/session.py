"""
GraphRAG MCP Session Management.

Provides isolated session management for multiple repository contexts.
"""

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional
import threading


class GraphRAGMCPSession:
    """Represents an isolated session for a specific repository context."""

    def __init__(self, session_id: str, repo_path: str):
        self.session_id = session_id
        self.repo_path = Path(repo_path).resolve()
        self.collection_name = self._generate_collection_name()
        self.created_at = datetime.now()
        self.last_accessed = datetime.now()
        self._lock = threading.RLock()

    def _generate_collection_name(self) -> str:
        """Generate repository-specific collection name.

        Creates a unique collection name based on the repository path hash.
        This ensures each repository gets its own isolated collection.

        Returns:
            Collection name in format 'repo_<hash[:16]>'
        """
        repo_hash = hashlib.sha256(str(self.repo_path).encode()).hexdigest()[:16]
        return f"repo_{repo_hash}"

    def update_access(self):
        """Update last accessed timestamp (thread-safe).

        This method is called whenever the session is accessed to track
        usage and enable cleanup of expired sessions.
        """
        with self._lock:
            self.last_accessed = datetime.now()

    def to_dict(self) -> dict:
        """Convert session to dictionary.

        Returns:
            Dictionary representation of the session
        """
        return {
            "session_id": self.session_id,
            "repo_path": str(self.repo_path),
            "collection_name": self.collection_name,
            "created_at": self.created_at.isoformat(),
            "last_accessed": self.last_accessed.isoformat()
        }

    def __repr__(self) -> str:
        """String representation of the session."""
        return (
            f"GraphRAGMCPSession("
            f"id={self.session_id}, "
            f"repo={self.repo_path.name}, "
            f"collection={self.collection_name}"
            f")"
        )
