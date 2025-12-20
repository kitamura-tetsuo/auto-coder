import json
import os
import tempfile
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import time

from hishel import SyncSqliteStorage
from src.auto_coder.github_client import GitHubClient

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

    def test_e2e_etag_caching_with_real_server(self):
        """
        Tests the full caching flow using a real local HTTP server.
        """
        client = GitHubClient.get_instance(token="fake-token")

        # Redirect the client's API endpoint to our local server
        server_url = f"http://localhost:{self.server_port}"
        client.github._Github__requester._Requester__base_url = server_url

        # --- First call ---
        # This should make a real HTTP request to our server, which returns a 200 OK
        # response with an ETag. This response should be cached by `hishel`.
        repo1 = client.get_repository("test-owner/auto-coder")
        self.assertEqual(repo1.name, "auto-coder")

        # --- Second call ---
        # This should trigger a second HTTP request for revalidation. The client
        # should send the ETag, and our server will respond with 304 Not Modified.
        # `hishel` should intercept this and return the original cached data.
        repo2 = client.get_repository("test-owner/auto-coder")
        self.assertEqual(repo2.name, "auto-coder")

        # Verify that the server was contacted twice (once to prime, once to revalidate)
        self.assertEqual(GitHubAPIHandler.call_count, 2)
        # Verify that the data is consistent between the two calls
        self.assertEqual(repo1.raw_data, repo2.raw_data)


if __name__ == "__main__":
    unittest.main()
