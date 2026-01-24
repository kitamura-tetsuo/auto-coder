import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Mock fastapi and pydantic before importing auto_coder.webhook_server
mock_fastapi = MagicMock()
mock_background_tasks = MagicMock()
mock_fastapi.BackgroundTasks = mock_background_tasks
mock_fastapi.FastAPI = MagicMock()
mock_fastapi.Header = MagicMock()
mock_fastapi.HTTPException = MagicMock()
mock_fastapi.Request = MagicMock()
sys.modules["fastapi"] = mock_fastapi

mock_pydantic = MagicMock()
mock_base_model = MagicMock()
mock_pydantic.BaseModel = mock_base_model
sys.modules["pydantic"] = mock_pydantic

# Mock auto_coder modules
mock_config = MagicMock()


# Create a simple Candidate class for the test
class MockCandidate:
    def __init__(self, type, data, priority, issue_number=None):
        self.type = type
        self.data = data
        self.priority = priority
        self.issue_number = issue_number


mock_config.Candidate = MockCandidate
sys.modules["auto_coder.automation_config"] = mock_config

mock_engine_module = MagicMock()
mock_engine_class = MagicMock()
mock_engine_module.AutomationEngine = mock_engine_class
sys.modules["auto_coder.automation_engine"] = mock_engine_module

mock_logger_config = MagicMock()
mock_logger = MagicMock()
mock_logger_config.get_logger.return_value = mock_logger
sys.modules["auto_coder.logger_config"] = mock_logger_config

# Mock tomli_w
sys.modules["tomli_w"] = MagicMock()

# Mock llm_backend_config
sys.modules["auto_coder.llm_backend_config"] = MagicMock()

from auto_coder.webhook_server import process_github_payload

# from auto_coder.automation_engine import AutomationEngine # Don't import this anymore as we mocked it


class TestWebhookDelay(unittest.TestCase):
    @patch("auto_coder.webhook_server.asyncio.sleep", new_callable=AsyncMock)
    @patch("auto_coder.webhook_server.asyncio.get_running_loop")
    def test_issue_webhook_delay(self, mock_get_loop, mock_sleep):
        # Setup mocks
        mock_engine = MagicMock()  # Removed spec=AutomationEngine
        mock_engine.queue = AsyncMock()
        # In the code: await engine.queue.put(candidate) -> so it's likely an asyncio.Queue, need AsyncMock for put if we await it.
        # Actually queue.put is a coroutine in asyncio.Queue

        # We need a proper AsyncMock for queue.put
        mock_queue = MagicMock()
        mock_queue.put = AsyncMock()
        mock_engine.queue = mock_queue

        mock_engine.github = MagicMock()

        # Setup loop mock for run_in_executor
        mock_loop = MagicMock()
        mock_get_loop.return_value = mock_loop

        # Mock run_in_executor to return immediate values (simulating the executor running the function)
        async def side_effect(executor, func):
            return func()

        mock_loop.run_in_executor = AsyncMock(side_effect=side_effect)

        # Mock GitHub responses
        issue_obj = {"number": 123}
        issue_details = {"number": 123, "state": "open", "title": "Test Issue", "body": "Body"}

        mock_engine.github.get_issue.return_value = issue_obj
        mock_engine.github.get_issue_details.return_value = issue_details

        # Run the function
        payload = {"action": "opened", "issue": {"number": 123}}

        # We need to run the async function
        asyncio.run(process_github_payload("issues", payload, mock_engine, "owner/repo"))

        # Assertions

        # 1. Verify sleep was called with 300 seconds
        mock_sleep.assert_called_with(300)

        # 2. Verify we fetched issue details
        mock_engine.github.get_issue.assert_called()
        mock_engine.github.get_issue_details.assert_called()

        # 3. Verify queued
        mock_queue.put.assert_called_once()
        candidate = mock_queue.put.call_args[0][0]
        self.assertEqual(candidate.priority, 0)
        self.assertEqual(candidate.issue_number, 123)
        self.assertEqual(candidate.type, "issue")


if __name__ == "__main__":
    unittest.main()
