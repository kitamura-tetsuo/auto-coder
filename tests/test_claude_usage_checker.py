"""
Unit tests for Claude OAuth usage checker.
"""

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.claude_usage_checker import (
    ClaudeUsageQuota,
    ClaudeUsageWindow,
    check_claude_usage,
    check_claude_usage_or_raise,
    clear_claude_usage_cache,
    fetch_claude_usage_data,
    refresh_claude_access_token,
    resolve_claude_oauth_token,
)
from auto_coder.exceptions import AutoCoderUsageLimitError


@pytest.fixture(autouse=True)
def reset_cache():
    """Clear Claude usage cache before and after every test."""
    clear_claude_usage_cache()
    yield
    clear_claude_usage_cache()


class TestClaudeUsageWindow:
    """Test ClaudeUsageWindow dataclass calculations."""

    def test_remaining_percent_calculation(self):
        """Test remaining_percent when utilization is present."""
        window = ClaudeUsageWindow(utilization=37.0, resets_at="2026-08-16T18:30:00Z")
        assert window.remaining_percent == 63.0

    def test_remaining_percent_none(self):
        """Test remaining_percent when utilization is None."""
        window = ClaudeUsageWindow(utilization=None)
        assert window.remaining_percent is None

    def test_remaining_percent_over_100(self):
        """Test remaining_percent when utilization exceeds 100%."""
        window = ClaudeUsageWindow(utilization=105.0)
        assert window.remaining_percent == 0.0


class TestClaudeUsageChecker:
    """Test suite for usage checker functions."""

    def test_check_claude_usage_sufficient_quota(self):
        """Test that usage within limits is marked sufficient."""
        mock_data = {
            "five_hour": {"utilization": 37.0, "resets_at": "2026-08-16T18:30:00Z"},
            "seven_day": {"utilization": 41.0, "resets_at": "2026-08-18T21:00:00Z"},
        }
        with patch("auto_coder.claude_usage_checker.fetch_claude_usage_data", return_value=mock_data):
            quota = check_claude_usage(token="test-token", use_cache=False)
            assert quota.is_quota_insufficient is False
            assert quota.five_hour.utilization == 37.0
            assert quota.five_hour.remaining_percent == 63.0
            assert quota.seven_day.utilization == 41.0
            assert quota.seven_day.remaining_percent == 59.0

    def test_check_claude_usage_five_hour_insufficient(self):
        """Test that 5-hour quota remaining <= 20% (utilization >= 80%) triggers insufficient flag."""
        mock_data = {
            "five_hour": {"utilization": 82.0, "resets_at": "2026-08-16T18:30:00Z"},
            "seven_day": {"utilization": 50.0, "resets_at": "2026-08-18T21:00:00Z"},
        }
        with patch("auto_coder.claude_usage_checker.fetch_claude_usage_data", return_value=mock_data):
            quota = check_claude_usage(token="test-token", use_cache=False)
            assert quota.is_quota_insufficient is True
            assert "5-hour limit remaining 18.0%" in quota.reason
            assert "2026-08-16T18:30:00Z" in quota.reason

    def test_check_claude_usage_seven_day_insufficient(self):
        """Test that 7-day quota remaining <= 5% (utilization >= 95%) triggers insufficient flag."""
        mock_data = {
            "five_hour": {"utilization": 50.0, "resets_at": "2026-08-16T18:30:00Z"},
            "seven_day": {"utilization": 96.0, "resets_at": "2026-08-18T21:00:00Z"},
        }
        with patch("auto_coder.claude_usage_checker.fetch_claude_usage_data", return_value=mock_data):
            quota = check_claude_usage(token="test-token", use_cache=False)
            assert quota.is_quota_insufficient is True
            assert "7-day limit remaining 4.0%" in quota.reason
            assert "2026-08-18T21:00:00Z" in quota.reason

    def test_check_claude_usage_or_raise_when_insufficient(self):
        """Test check_claude_usage_or_raise raises AutoCoderUsageLimitError."""
        mock_data = {
            "five_hour": {"utilization": 85.0, "resets_at": "2026-08-16T18:30:00Z"},
            "seven_day": {"utilization": 40.0, "resets_at": "2026-08-18T21:00:00Z"},
        }
        with patch("auto_coder.claude_usage_checker.fetch_claude_usage_data", return_value=mock_data):
            with pytest.raises(AutoCoderUsageLimitError) as exc_info:
                check_claude_usage_or_raise(token="test-token", backend_name="claude-opus")
            assert "Claude usage threshold reached for backend 'claude-opus'" in str(exc_info.value)
            assert "5-hour limit remaining 15.0%" in str(exc_info.value)

    def test_check_claude_usage_or_raise_when_sufficient(self):
        """Test check_claude_usage_or_raise passes silently when quota is normal."""
        mock_data = {
            "five_hour": {"utilization": 20.0, "resets_at": "2026-08-16T18:30:00Z"},
            "seven_day": {"utilization": 30.0, "resets_at": "2026-08-18T21:00:00Z"},
        }
        with patch("auto_coder.claude_usage_checker.fetch_claude_usage_data", return_value=mock_data):
            # Should not raise
            check_claude_usage_or_raise(token="test-token", backend_name="claude-opus")

    def test_check_claude_usage_or_raise_when_fetch_returns_none(self):
        """Test check_claude_usage_or_raise does not block when API fetch fails."""
        with patch("auto_coder.claude_usage_checker.fetch_claude_usage_data", return_value=None):
            # Should not raise (graceful fallback)
            check_claude_usage_or_raise(token="test-token", backend_name="claude-opus")

    def test_resolve_token_priority(self):
        """Test token resolution order: explicit token > env var > credentials file."""
        # 1. Explicit token
        assert resolve_claude_oauth_token("explicit-tok") == "explicit-tok"

        # 2. Env token
        with patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "env-tok"}):
            assert resolve_claude_oauth_token(None) == "env-tok"

        # 3. Credentials file
        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "auto_coder.claude_usage_checker._read_credentials_file",
                return_value={"claudeAiOauth": {"accessToken": "file-tok"}},
            ),
        ):
            assert resolve_claude_oauth_token(None) == "file-tok"

    def test_fetch_claude_usage_data_handles_401_and_refresh(self):
        """Test that HTTP 401 attempts token refresh and retries."""
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value.read.return_value = json.dumps({"five_hour": {"utilization": 10.0}}).encode("utf-8")

        # First call raises 401, second call succeeds with refreshed token
        http_401 = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)  # type: ignore

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise http_401
            return mock_resp

        with (
            patch("auto_coder.claude_usage_checker.resolve_claude_oauth_token", return_value="old-token"),
            patch(
                "auto_coder.claude_usage_checker._read_credentials_file",
                return_value={"claudeAiOauth": {"refreshToken": "refresh-tok"}},
            ),
            patch(
                "auto_coder.claude_usage_checker.refresh_claude_access_token",
                return_value="new-refreshed-token",
            ) as mock_refresh,
            patch(
                "urllib.request.urlopen",
                side_effect=side_effect,
            ),
        ):
            result = fetch_claude_usage_data("old-token")
            assert result == {"five_hour": {"utilization": 10.0}}
            mock_refresh.assert_called_once_with("refresh-tok", scopes=None)

    def test_refresh_claude_access_token_success(self):
        """Test refresh_claude_access_token parses response and updates credentials file."""
        mock_token_resp = MagicMock()
        mock_token_resp.__enter__.return_value.read.return_value = json.dumps(
            {
                "access_token": "new-acc-token",
                "refresh_token": "new-ref-token",
                "expires_in": 3600,
            }
        ).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=mock_token_resp) as mock_urlopen, patch("auto_coder.claude_usage_checker._update_credentials_file") as mock_update:
            token = refresh_claude_access_token("my-refresh-token", scopes=["user:profile", "user:inference"])
            assert token == "new-acc-token"
            mock_update.assert_called_once()
            req = mock_urlopen.call_args[0][0]
            assert req.headers["Content-type"] == "application/json"
            body = json.loads(req.data.decode("utf-8"))
            assert body["grant_type"] == "refresh_token"
            assert body["refresh_token"] == "my-refresh-token"
            assert body["client_id"] == "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
            assert body["scope"] == "user:profile user:inference"

    def test_refresh_claude_access_token_handles_429(self):
        """Test that refresh_claude_access_token handles HTTP 429 gracefully."""
        http_429 = urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)  # type: ignore
        with patch("urllib.request.urlopen", side_effect=http_429):
            token = refresh_claude_access_token("my-refresh-token")
            assert token is None

    def test_resolve_token_proactive_refresh_when_expired(self):
        """Test resolve_claude_oauth_token proactively refreshes when expiresAt is in past."""
        expired_ms = (1000.0) * 1000  # in the past
        creds = {
            "claudeAiOauth": {
                "accessToken": "expired-tok",
                "refreshToken": "refresh-tok",
                "expiresAt": expired_ms,
                "scopes": ["user:profile"],
            }
        }
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("auto_coder.claude_usage_checker._read_credentials_file", return_value=creds),
            patch("auto_coder.claude_usage_checker.refresh_claude_access_token", return_value="proactive-refreshed-tok") as mock_refresh,
        ):
            token = resolve_claude_oauth_token(None)
            assert token == "proactive-refreshed-tok"
            mock_refresh.assert_called_once_with("refresh-tok", scopes=["user:profile"])

    def test_fetch_claude_usage_data_handles_429(self):
        """Test fetch_claude_usage_data returns rate limit error structure on HTTP 429."""
        http_429 = urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)  # type: ignore
        with (
            patch("auto_coder.claude_usage_checker.resolve_claude_oauth_token", return_value="test-token"),
            patch("urllib.request.urlopen", side_effect=http_429),
        ):
            res = fetch_claude_usage_data("test-token")
            assert res is not None
            assert res.get("is_rate_limited") is True
            assert res.get("http_status") == 429
            assert res.get("error", {}).get("type") == "rate_limit_error"

    def test_check_claude_usage_http_429_insufficient(self):
        """Test check_claude_usage marks quota insufficient on HTTP 429 rate limit."""
        mock_data = {
            "is_rate_limited": True,
            "http_status": 429,
            "error": {"type": "rate_limit_error", "message": "Rate limit reached (HTTP 429)"},
        }
        with patch("auto_coder.claude_usage_checker.fetch_claude_usage_data", return_value=mock_data):
            quota = check_claude_usage(token="test-token", use_cache=False)
            assert quota.is_quota_insufficient is True
            assert "Claude rate limit error" in quota.reason

    def test_check_claude_usage_extra_usage_out_of_credits(self):
        """Test check_claude_usage detects extra_usage out_of_credits disabled_reason."""
        mock_data = {
            "five_hour": {"utilization": 10.0},
            "seven_day": {"utilization": 20.0},
            "extra_usage": {
                "is_enabled": False,
                "disabled_reason": "out_of_credits",
            },
        }
        with patch("auto_coder.claude_usage_checker.fetch_claude_usage_data", return_value=mock_data):
            quota = check_claude_usage(token="test-token", use_cache=False)
            assert quota.is_quota_insufficient is True
            assert "Extra usage disabled: out_of_credits" in quota.reason

    def test_check_claude_usage_sonnet_window_insufficient(self):
        """Test check_claude_usage detects 7-day sonnet limit exhaustion."""
        mock_data = {
            "five_hour": {"utilization": 10.0},
            "seven_day": {"utilization": 20.0},
            "seven_day_sonnet": {"utilization": 98.0, "resets_at": "2026-08-25T10:00:00Z"},
        }
        with patch("auto_coder.claude_usage_checker.fetch_claude_usage_data", return_value=mock_data):
            quota = check_claude_usage(token="test-token", use_cache=False)
            assert quota.is_quota_insufficient is True
            assert "7-day Sonnet limit remaining 2.0%" in quota.reason

    def test_check_claude_usage_weekly_model_limits_array(self):
        """Test check_claude_usage parses limits array with weekly_scoped models."""
        mock_data = {
            "five_hour": {"utilization": 10.0},
            "seven_day": {"utilization": 20.0},
            "limits": [
                {
                    "kind": "weekly_scoped",
                    "scope": {"model": {"display_name": "Claude 3.7 Sonnet"}},
                    "percent": 97.0,
                    "resets_at": "2026-08-26T00:00:00Z",
                }
            ],
        }
        with patch("auto_coder.claude_usage_checker.fetch_claude_usage_data", return_value=mock_data):
            quota = check_claude_usage(token="test-token", use_cache=False)
            assert quota.is_quota_insufficient is True
            assert "Weekly Claude 3.7 Sonnet limit remaining 3.0%" in quota.reason
