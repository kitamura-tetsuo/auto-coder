"""
Unit tests for CodexWhamClient and internal WHAM backend API integration.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from auto_coder.codex_usage_checker import CodexOAuthCredentials
from auto_coder.codex_wham_client import (
    CodexWhamClient,
    FollowUpDeliveryOutcome,
    WhamFollowUpPayload,
    WhamTask,
    WhamTurn,
)


class TestWhamFollowUpPayload:
    """Test suite for WhamFollowUpPayload structure."""

    def test_payload_structure(self):
        """Verify the payload matches the exact schema observed in Codex Cloud Web UI."""
        payload = WhamFollowUpPayload(
            task_id="task_e_1234567890",
            turn_id="task_e_1234567890~assttrn_e_abcdef",
            text="Review the current pull request status and continue working on it.",
            run_environment_in_qa_mode=False,
        )
        data = payload.to_dict()

        assert data == {
            "follow_up": {
                "task_id": "task_e_1234567890",
                "turn_id": "task_e_1234567890~assttrn_e_abcdef",
                "run_environment_in_qa_mode": False,
            },
            "input_items": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "content_type": "text",
                            "text": "Review the current pull request status and continue working on it.",
                        }
                    ],
                }
            ],
        }


class TestCodexWhamClient:
    """Test suite for CodexWhamClient operations."""

    @pytest.fixture
    def mock_credentials(self):
        return CodexOAuthCredentials(
            access_token="test_oauth_access_token_123",
            account_id="test_chatgpt_account_id_456",
        )

    @pytest.fixture
    def client(self, mock_credentials):
        with patch("auto_coder.codex_wham_client.load_codex_oauth_credentials", return_value=mock_credentials):
            wham_client = CodexWhamClient(base_url="https://chatgpt.com/backend-api/wham")
            yield wham_client

    def test_headers_use_oauth_and_not_api_key(self, client, mock_credentials):
        """Verify headers use Bearer OAuth token and ChatGPT-Account-Id without any API keys."""
        headers = client._get_headers()
        assert headers is not None
        assert headers["Authorization"] == f"Bearer {mock_credentials.access_token}"
        assert headers["ChatGPT-Account-Id"] == mock_credentials.account_id
        assert "CODEX_API_KEY" not in headers
        assert "api-key" not in headers

    def test_headers_return_none_when_credentials_missing(self):
        """Verify headers are None when OAuth credentials cannot be loaded."""
        with patch("auto_coder.codex_wham_client.load_codex_oauth_credentials", return_value=None):
            wham_client = CodexWhamClient()
            assert wham_client._get_headers() is None

    def test_get_task_success(self, client):
        """Test get_task parses task details and turns."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "task": {
                "id": "task_e_100",
                "status": "ready",
                "title": "Fix issue #1",
                "turns": [
                    {"id": "task_e_100~usrtrn_1", "role": "user", "status": "completed"},
                    {"id": "task_e_100~assttrn_1", "role": "assistant", "status": "completed"},
                ],
            }
        }

        with patch("httpx.get", return_value=mock_response) as mock_get:
            task = client.get_task("task_e_100")
            assert task is not None
            assert task.id == "task_e_100"
            assert task.status == "ready"
            assert task.title == "Fix issue #1"
            assert len(task.turns) == 2
            assert task.turns[1].id == "task_e_100~assttrn_1"
            assert task.turns[1].role == "assistant"

            mock_get.assert_called_once()
            assert mock_get.call_args[0][0] == "https://chatgpt.com/backend-api/wham/tasks/task_e_100"

    def test_get_task_auth_failure(self, client):
        """Test get_task returns None and fails safely on HTTP 401/403."""
        for code in (401, 403):
            mock_response = MagicMock(status_code=code)
            with patch("httpx.get", return_value=mock_response):
                task = client.get_task("task_e_100")
                assert task is None

    def test_get_task_http_error(self, client):
        """Test get_task handles 500 or network exceptions safely."""
        with patch("httpx.get", side_effect=httpx.ConnectError("Connection refused")):
            task = client.get_task("task_e_100")
            assert task is None

    def test_get_task_turns_success_from_list(self, client):
        """Test get_task_turns when response is a JSON list."""
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = [
            {"id": "task_e_200~usrtrn_1", "role": "user", "status": "completed"},
            {"id": "task_e_200~assttrn_1", "role": "assistant", "status": "completed"},
            {"id": "task_e_200~assttrn_2", "role": "assistant", "status": "completed"},
        ]

        with patch("httpx.get", return_value=mock_response):
            turns = client.get_task_turns("task_e_200")
            assert len(turns) == 3
            assert turns[2].id == "task_e_200~assttrn_2"
            assert turns[2].role == "assistant"

    def test_get_task_turns_success_from_dict(self, client):
        """Test get_task_turns when response is a dict with 'turns' key."""
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "turns": [
                {"id": "assttrn_10", "author": {"role": "assistant"}, "status": "completed"},
            ]
        }

        with patch("httpx.get", return_value=mock_response):
            turns = client.get_task_turns("task_e_300")
            assert len(turns) == 1
            assert turns[0].id == "assttrn_10"
            assert turns[0].role == "assistant"

    def test_resolve_latest_assistant_turn_selects_latest(self, client):
        """Verify resolve_latest_assistant_turn selects the chronologically latest assistant turn."""
        turns = [
            WhamTurn(id="task_e_400~usrtrn_1", role="user", status="completed"),
            WhamTurn(id="task_e_400~assttrn_1", role="assistant", status="completed"),
            WhamTurn(id="task_e_400~usrtrn_2", role="user", status="completed"),
            WhamTurn(id="task_e_400~assttrn_2", role="assistant", status="completed"),
        ]

        with patch.object(client, "get_task_turns", return_value=turns):
            turn_id = client.resolve_latest_assistant_turn("task_e_400")
            assert turn_id == "task_e_400~assttrn_2"

    def test_resolve_latest_assistant_turn_normalizes_prefix(self, client):
        """Verify turn IDs without task prefix are normalized to task_id~turn_id."""
        turns = [
            WhamTurn(id="assttrn_abc", role="assistant", status="completed"),
        ]

        with patch.object(client, "get_task_turns", return_value=turns):
            turn_id = client.resolve_latest_assistant_turn("task_e_500")
            assert turn_id == "task_e_500~assttrn_abc"

    def test_resolve_latest_assistant_turn_rejects_cross_task_turns(self, client):
        """Verify turns with mismatched task prefix are rejected."""
        turns = [
            WhamTurn(id="task_e_DIFFERENT~assttrn_1", role="assistant", status="completed"),
        ]

        with patch.object(client, "get_task_turns", return_value=turns):
            turn_id = client.resolve_latest_assistant_turn("task_e_600")
            assert turn_id is None

    def test_resolve_latest_assistant_turn_ignores_failed_turns(self, client):
        """Verify failed or errored assistant turns are skipped in favor of a valid prior turn."""
        turns = [
            WhamTurn(id="task_e_700~assttrn_1", role="assistant", status="completed"),
            WhamTurn(id="task_e_700~assttrn_2", role="assistant", status="failed"),
        ]

        with patch.object(client, "get_task_turns", return_value=turns):
            turn_id = client.resolve_latest_assistant_turn("task_e_700")
            assert turn_id == "task_e_700~assttrn_1"

    def test_resolve_latest_assistant_turn_no_assistant_turns(self, client):
        """Verify None is returned when only user turns exist."""
        turns = [
            WhamTurn(id="task_e_800~usrtrn_1", role="user", status="completed"),
        ]

        with patch.object(client, "get_task_turns", return_value=turns):
            turn_id = client.resolve_latest_assistant_turn("task_e_800")
            assert turn_id is None

    def test_resolve_latest_assistant_turn_fallback_to_get_task(self, client):
        """Verify fallback to get_task when get_task_turns returns empty."""
        task_with_turns = WhamTask(
            id="task_e_900",
            status="ready",
            turns=[WhamTurn(id="task_e_900~assttrn_99", role="assistant", status="completed")],
        )

        with (
            patch.object(client, "get_task_turns", return_value=[]),
            patch.object(client, "get_task", return_value=task_with_turns),
        ):
            turn_id = client.resolve_latest_assistant_turn("task_e_900")
            assert turn_id == "task_e_900~assttrn_99"

    def test_reconcile_follow_up_matches_exposed_user_message(self, client):
        prompt = "Fix stable feedback identity 123"
        turns = [
            WhamTurn(id="task_e_900~assttrn_1", role="assistant"),
            WhamTurn(id="task_e_900~usrtrn_2", role="user", raw_data={"content": [{"text": prompt}]}),
            WhamTurn(id="task_e_900~assttrn_2", role="assistant"),
        ]
        with patch.object(client, "get_task_turns", return_value=turns):
            assert client.reconcile_follow_up("task_e_900", turns[0].id, prompt) is True

    def test_reconcile_follow_up_does_not_accept_unrelated_user_advancement(self, client):
        turns = [
            WhamTurn(id="task_e_900~assttrn_1", role="assistant"),
            WhamTurn(id="task_e_900~usrtrn_2", role="user", raw_data={"content": [{"text": "different work"}]}),
            WhamTurn(id="task_e_900~assttrn_2", role="assistant"),
        ]
        with patch.object(client, "get_task_turns", return_value=turns):
            assert client.reconcile_follow_up("task_e_900", turns[0].id, "expected work") is None

    def test_send_follow_up_success(self, client):
        """Verify send_follow_up posts payload and returns True on HTTP 200/201."""
        mock_response = MagicMock(status_code=200)

        with patch("httpx.post", return_value=mock_response) as mock_post:
            success = client.send_follow_up(
                task_id="task_e_abc",
                turn_id="task_e_abc~assttrn_1",
                prompt="Continue working on PR",
                run_environment_in_qa_mode=False,
            )
            assert success.outcome is FollowUpDeliveryOutcome.DELIVERED
            mock_post.assert_called_once()
            call_url = mock_post.call_args[0][0]
            call_json = mock_post.call_args[1]["json"]
            assert call_url == "https://chatgpt.com/backend-api/wham/tasks"
            assert call_json["follow_up"]["task_id"] == "task_e_abc"
            assert call_json["follow_up"]["turn_id"] == "task_e_abc~assttrn_1"
            assert call_json["input_items"][0]["content"][0]["text"] == "Continue working on PR"

    def test_send_follow_up_auth_failure(self, client):
        """Verify send_follow_up returns False on HTTP 401/403 without retrying with API keys."""
        for code in (401, 403):
            mock_response = MagicMock(status_code=code)
            with patch("httpx.post", return_value=mock_response):
                success = client.send_follow_up("task_e_abc", "turn_1", "continue")
                assert success.outcome is FollowUpDeliveryOutcome.NOT_DELIVERED

    def test_send_follow_up_server_error(self, client):
        """Verify HTTP 500 has an indeterminate delivery result."""
        mock_response = MagicMock(status_code=500)
        with patch("httpx.post", return_value=mock_response):
            success = client.send_follow_up("task_e_abc", "turn_1", "continue")
            assert success.outcome is FollowUpDeliveryOutcome.INDETERMINATE

    def test_send_follow_up_rate_limit_is_indeterminate(self, client):
        """A 429 response does not prove that WHAM rejected the POST."""
        with patch("httpx.post", return_value=MagicMock(status_code=429)):
            result = client.send_follow_up("task_e_abc", "turn_1", "continue")

        assert result.outcome is FollowUpDeliveryOutcome.INDETERMINATE
        assert result.status_code == 429

    def test_send_follow_up_timeout_is_indeterminate(self, client):
        """A timeout may happen after WHAM accepted the request."""
        with patch("httpx.post", side_effect=httpx.ReadTimeout("late response")):
            result = client.send_follow_up("task_e_abc", "turn_1", "continue")

        assert result.outcome is FollowUpDeliveryOutcome.INDETERMINATE

    def test_send_follow_up_network_error(self, client):
        """Verify send_follow_up handles network/HTTP exceptions safely."""
        with patch("httpx.post", side_effect=httpx.ConnectTimeout("Timeout")):
            success = client.send_follow_up("task_e_abc", "turn_1", "continue")
            assert success.outcome is FollowUpDeliveryOutcome.INDETERMINATE
