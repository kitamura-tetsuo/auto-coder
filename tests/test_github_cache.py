"""Tests for GitHubCache."""

import unittest
from unittest.mock import Mock, patch

from src.auto_coder.util.github_cache import GitHubCache, get_github_cache


class TestGitHubCache(unittest.TestCase):
    """Test cases for GitHubCache."""

    def setUp(self):
        """Set up test fixtures."""
        # Clear cache before each test
        get_github_cache().clear()

    def test_singleton(self):
        """Test that GitHubCache is a singleton."""
        cache1 = GitHubCache()
        cache2 = GitHubCache()
        cache3 = get_github_cache()

        self.assertIs(cache1, cache2)
        self.assertIs(cache1, cache3)

    def test_set_get(self):
        """Test setting and getting values."""
        cache = get_github_cache()
        cache.set("key1", "value1")
        
        self.assertEqual(cache.get("key1"), "value1")
        self.assertIsNone(cache.get("key2"))

    def test_clear(self):
        """Test clearing the cache."""
        cache = get_github_cache()
        cache.set("key1", "value1")
        cache.clear()
        
        self.assertIsNone(cache.get("key1"))


if __name__ == "__main__":
    unittest.main()
