"""Tests for JulesClient.get_session_activities pagination and payload shape (Issue #2147, item 4).

Covers:
- complete pagination across nextPageToken pages;
- a partial first page followed by a failing subsequent page must not be
  silently truncated into "complete history" (raises instead);
- a genuinely absent endpoint (404 on the first page) returns an empty list,
  distinct from an incomplete read.
"""

from unittest.mock import Mock, patch

import pytest

from src.auto_coder.jules_client import JulesClient


def _make_client() -> JulesClient:
    with patch("src.auto_coder.jules_client.get_llm_config") as mock_get_config:
        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = []
        mock_backend_config.options_for_noedit = []
        mock_backend_config.api_key = None
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config
        return JulesClient()


class TestJulesClientActivitiesPagination:
    @patch("requests.Session.get")
    def test_complete_pagination_across_pages(self, mock_get):
        """A partial first page must not be treated as complete history: all
        pages must be followed via nextPageToken."""
        page1 = Mock()
        page1.status_code = 200
        page1.json.return_value = {
            "activities": [{"name": "activities/1", "userMessage": {"prompt": "ok"}, "createTime": "2026-01-01T09:00:00Z"}],
            "nextPageToken": "token-2",
        }
        page2 = Mock()
        page2.status_code = 200
        page2.json.return_value = {
            "activities": [{"name": "activities/2", "sessionCompleted": {}, "createTime": "2026-01-01T10:00:00Z"}],
        }
        mock_get.side_effect = [page1, page2]

        client = _make_client()
        activities = client.get_session_activities("sess-1")
        assert len(activities) == 2
        assert activities[0]["name"] == "activities/1"
        assert activities[1]["name"] == "activities/2"
        assert mock_get.call_count == 2

    @patch("requests.Session.get")
    def test_incomplete_pagination_raises_instead_of_truncating(self, mock_get):
        """A first page promising a nextPageToken whose subsequent fetch fails
        must raise, never return the partial first page as complete."""
        page1 = Mock()
        page1.status_code = 200
        page1.json.return_value = {
            "activities": [{"name": "activities/1", "userMessage": {}, "createTime": "2026-01-01T09:00:00Z"}],
            "nextPageToken": "token-2",
        }
        page2_failure = Mock()
        page2_failure.status_code = 500
        mock_get.side_effect = [page1, page2_failure]

        client = _make_client()
        with pytest.raises(RuntimeError):
            client.get_session_activities("sess-1")

    @patch("requests.Session.get")
    def test_absent_endpoint_returns_empty_list(self, mock_get):
        """A 404 on the FIRST page means no activities support for this
        session at all — distinct from an incomplete/failed read."""
        response = Mock()
        response.status_code = 404
        mock_get.return_value = response

        client = _make_client()
        assert client.get_session_activities("sess-1") == []

    @patch("requests.Session.get")
    def test_bare_list_payload_supported(self, mock_get):
        """Some responses may return a bare JSON list rather than a wrapper object."""
        response = Mock()
        response.status_code = 200
        response.json.return_value = [
            {"name": "activities/1", "planApproval": {}, "createTime": "2026-01-01T09:00:00Z"},
        ]
        mock_get.return_value = response

        client = _make_client()
        activities = client.get_session_activities("sess-1")
        assert len(activities) == 1
