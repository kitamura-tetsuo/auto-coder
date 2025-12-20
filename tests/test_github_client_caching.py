import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import httpx
from hishel import SyncSqliteStorage
from hishel.httpx import SyncCacheClient

from src.auto_coder.github_client import GitHubClient


class TestGitHubClientCaching(unittest.TestCase):
    def setUp(self):
        # Reset the singleton before each test
        GitHubClient.reset_singleton()
        # Create a temporary file for the cache
        self.tmpfile = tempfile.NamedTemporaryFile(delete=False)
        self.db_path = self.tmpfile.name
        self.tmpfile.close()

    def tearDown(self):
        # Clean up the temporary file
        os.unlink(self.db_path)

    @patch("src.auto_coder.github_client.get_caching_client")
    def test_get_repo_caching(self, mock_get_caching_client):
        # Arrange
        mock_transport = MagicMock(spec=httpx.HTTPTransport)

        # Configure the mock transport to return a cacheable response
        response_content = json.dumps({"name": "auto-coder", "description": "test"}).encode("utf-8")
        mock_response = httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "Cache-Control": "max-age=60",
                "ETag": "fake-etag",  # Add ETag for revalidation
            },
            content=response_content,
            request=httpx.Request("GET", "https://api.github.com/repos/test-owner/auto-coder"),
        )
        mock_transport.handle_request.return_value = mock_response

        # Create a real caching client with sqlite storage and the mock transport
        caching_client = SyncCacheClient(storage=SyncSqliteStorage(database_path=self.db_path), transport=mock_transport)
        mock_get_caching_client.return_value = caching_client

        # Act
        client = GitHubClient.get_instance(token="fake-token")
        repo_name = "test-owner/auto-coder"

        # Call the method twice
        repo1 = client.get_repository(repo_name)
        repo2 = client.get_repository(repo_name)

        # Assert
        # The underlying transport should only be called once, because the second call should be cached
        mock_transport.handle_request.assert_called_once()
        self.assertEqual(repo1.name, "auto-coder")
        self.assertEqual(repo2.name, "auto-coder")


if __name__ == "__main__":
    unittest.main()
