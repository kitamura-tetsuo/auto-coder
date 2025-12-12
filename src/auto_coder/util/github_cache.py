"""
GitHub API Cache implementation.

This module provides a simple caching mechanism for GitHub API calls
to reduce rate limit usage.
"""

from typing import Any, Dict, Optional


class GitHubCache:
    """Singleton class for caching GitHub API responses."""

    _instance = None
    _cache: Dict[str, Any] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(GitHubCache, cls).__new__(cls)
            cls._instance._cache = {}
        return cls._instance

    def get(self, key: str) -> Optional[Any]:
        """Get a value from the cache."""
        return self._cache.get(key)

    def set(self, key: str, value: Any) -> None:
        """Set a value in the cache."""
        self._cache[key] = value

    def clear(self) -> None:
        """Clear the entire cache."""
        self._cache.clear()


def get_github_cache() -> GitHubCache:
    """Get the singleton instance of GitHubCache."""
    return GitHubCache()
