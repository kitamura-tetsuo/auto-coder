import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock, patch

from hishel import SyncSqliteStorage

from src.auto_coder.util.gh_cache import GitHubClient

# --- HTTP Server for realistic testing ---


class StoppableHTTPServer(HTTPServer):
    """A stoppable version of HTTPServer for clean shutdown in tests."""

    def run(self):
        self.serve_forever()

    def stop(self):
        self.shutdown()
        self.server_close()


class GitHubAPIHandler(BaseHTTPRequestHandler):
    """A handler that mimics GitHub API's ETag behavior."""

    repo_data = json.dumps({"name": "auto-coder", "description": "test"}).encode("utf-8")
    etag = 'W/"12345"'
    call_count = 0

    def do_GET(self):
        self.class_outer = GitHubAPIHandler
        self.class_outer.call_count += 1

        if self.path == "/repos/test-owner/auto-coder":
            if self.headers.get("If-None-Match") == self.etag:
                self.send_response(304)
                self.send_header("ETag", self.etag)
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("ETag", self.etag)
                self.send_header("Content-Length", str(len(self.repo_data)))
                self.end_headers()
                self.wfile.write(self.repo_data)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")


# --- Test Case ---


class TestGitHubClientCachingWithServer(unittest.TestCase):
    http_server: StoppableHTTPServer = None
    server_thread: threading.Thread = None

    @classmethod
    def setUpClass(cls):
        # Start a local HTTP server in a separate thread
        cls.http_server = StoppableHTTPServer(("localhost", 0), GitHubAPIHandler)
        cls.server_port = cls.http_server.server_port
        cls.server_thread = threading.Thread(target=cls.http_server.run)
        cls.server_thread.daemon = True
        cls.server_thread.start()
        time.sleep(0.1)  # Give the server a moment to start

    @classmethod
    def tearDownClass(cls):
        # Stop the server and wait for the thread to terminate
        cls.http_server.stop()
        cls.server_thread.join()

    def setUp(self):
        GitHubClient.reset_singleton()
        GitHubAPIHandler.call_count = 0
        self.tmpfile = tempfile.NamedTemporaryFile(delete=False)
        self.db_path = self.tmpfile.name
        self.tmpfile.close()

        # Patch the storage location to ensure test isolation
        patcher = patch("src.auto_coder.util.gh_cache.SyncSqliteStorage")
        self.mock_storage = patcher.start()
        self.mock_storage.return_value = SyncSqliteStorage(database_path=self.db_path)
        self.addCleanup(patcher.stop)

    def tearDown(self):
        os.unlink(self.db_path)

    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    def test_e2e_etag_caching_with_real_server(self, mock_get_caching_client):
        """
        Tests the full caching flow using mocked caching client.
        """
        # Create mock response object
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"ETag": 'W/"12345"'}
        mock_response.json.return_value = {"name": "auto-coder", "description": "test"}

        # Set up the mock to return our mock response
        mock_client = MagicMock()
        mock_client.request.return_value = mock_response
        mock_get_caching_client.return_value = mock_client

        client = GitHubClient.get_instance(token="fake-token")

        # --- First call ---
        # This should make a request that returns 200 OK with ETag
        repo1 = client.get_repository("test-owner/auto-coder")
        self.assertEqual(repo1["name"], "auto-coder")

        # --- Second call ---
        # This should trigger another request (in real scenario, would use 304)
        repo2 = client.get_repository("test-owner/auto-coder")
        self.assertEqual(repo2["name"], "auto-coder")

        # Verify that the client.request was called twice
        self.assertEqual(mock_client.request.call_count, 2)
        # Verify that the data is consistent between the two calls
        self.assertEqual(repo1["name"], repo2["name"])


if __name__ == "__main__":
    unittest.main()
