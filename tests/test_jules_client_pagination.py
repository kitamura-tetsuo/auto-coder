from unittest.mock import Mock, patch

import pytest

from src.auto_coder.jules_client import JulesClient


class TestJulesClientPagination:
    """Test cases for JulesClient pagination and filtering."""

    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("requests.Session.get")
    def test_list_sessions_pagination_and_filtering(self, mock_get, mock_get_config):
        """Test that list_sessions handles pagination and adds filter."""
        # Mock config
        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = []
        mock_backend_config.options_for_noedit = []
        mock_backend_config.api_key = None
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        # Mock responses
        # Page 1
        mock_response_page1 = Mock()
        mock_response_page1.status_code = 200
        mock_response_page1.json.return_value = {"sessions": [{"name": "session1", "state": "ACTIVE"}, {"name": "session_archived", "state": "ARCHIVED"}], "nextPageToken": "token_page_2"}

        # Page 2
        mock_response_page2 = Mock()
        mock_response_page2.status_code = 200
        mock_response_page2.json.return_value = {
            "sessions": [{"name": "session2", "state": "COMPLETED"}],
            # No nextPageToken
        }

        mock_get.side_effect = [mock_response_page1, mock_response_page2]

        client = JulesClient()
        sessions = client.list_sessions(page_size=10)

        # Verify results - should filter out ARCHIVED session
        assert len(sessions) == 2
        assert sessions[0]["name"] == "session1"
        assert sessions[1]["name"] == "session2"

        # Ensure archived session is NOT in the list
        names = [s["name"] for s in sessions]
        assert "session_archived" not in names

        # Verify API calls
        assert mock_get.call_count == 2

        # Check first call args
        call_args1 = mock_get.call_args_list[0]
        assert call_args1[0][0] == "https://jules.googleapis.com/v1alpha/sessions"
        assert call_args1[1]["params"]["pageSize"] == 10
        # Ensure filter param is NOT present
        assert "filter" not in call_args1[1]["params"]
        assert "pageToken" not in call_args1[1]["params"]

        # Check second call args
        call_args2 = mock_get.call_args_list[1]
        assert call_args2[0][0] == "https://jules.googleapis.com/v1alpha/sessions"
        assert call_args2[1]["params"]["pageSize"] == 10
        assert "filter" not in call_args2[1]["params"]
        assert call_args2[1]["params"]["pageToken"] == "token_page_2"
