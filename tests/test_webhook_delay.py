import asyncio
from unittest.mock import AsyncMock, MagicMock

from src.auto_coder.webhook_server import process_github_payload


def test_issue_webhook_is_durably_invalidated_without_snapshot_delay():
    engine = MagicMock()
    engine.invalidate_entity = AsyncMock(return_value=True)
    payload = {"action": "opened", "issue": {"number": 123, "title": "untrusted snapshot"}}

    asyncio.run(process_github_payload("issues", payload, engine, "owner/repo", "delivery-123"))

    engine.invalidate_entity.assert_awaited_once_with("owner/repo", "issue", 123, "delivery-123:0")
    engine.github.get_issue.assert_not_called()
