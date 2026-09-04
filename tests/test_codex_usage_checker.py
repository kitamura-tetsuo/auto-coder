import base64
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from auto_coder.codex_usage_checker import (
    CODEX_USAGE_URL,
    get_codex_weekly_usage,
    load_codex_oauth_credentials,
    parse_codex_weekly_usage,
)

NOW = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)


def _payload(remaining: float, reset_delta: timedelta) -> dict[str, object]:
    return {
        "rate_limit": {
            "primary_window": {
                "used_percent": 1,
                "limit_window_seconds": 18_000,
                "reset_at": (NOW + timedelta(hours=2)).timestamp(),
            },
            "secondary_window": {
                "used_percent": 100 - remaining,
                "limit_window_seconds": 604_800,
                "reset_at": (NOW + reset_delta).timestamp(),
            },
        }
    }


@pytest.mark.parametrize(
    ("reset_delta", "expected_days", "expected_threshold"),
    [
        (timedelta(hours=23), 0, 5),
        (timedelta(days=1, hours=1), 1, 10),
        (timedelta(days=3, hours=12), 3, 20),
        (timedelta(days=6, hours=23), 6, 35),
    ],
)
def test_threshold_uses_whole_days_until_reset(reset_delta, expected_days, expected_threshold):
    usage = parse_codex_weekly_usage(_payload(50, reset_delta), now=NOW)
    assert usage.days_until_reset == expected_days
    assert usage.minimum_remaining_percent == expected_threshold


def test_quota_boundary_is_inclusive():
    allowed = parse_codex_weekly_usage(_payload(20, timedelta(days=3)), now=NOW)
    denied = parse_codex_weekly_usage(_payload(19.9, timedelta(days=3)), now=NOW)
    assert allowed.can_start_task is True
    assert denied.can_start_task is False


def test_primary_window_is_used_when_it_is_weekly():
    payload = _payload(80, timedelta(days=2))
    rate_limit = payload["rate_limit"]
    assert isinstance(rate_limit, dict)
    rate_limit["primary_window"], rate_limit["secondary_window"] = rate_limit["secondary_window"], None
    usage = parse_codex_weekly_usage(payload, now=NOW)
    assert usage.remaining_percent == 80


@pytest.mark.parametrize("count", [3, 0])
def test_reset_credit_count_is_parsed_without_an_extra_request(count):
    payload = _payload(80, timedelta(days=2))
    payload["rate_limit_reset_credits"] = {"available_count": count}
    usage = parse_codex_weekly_usage(payload, now=NOW)
    assert usage.reset_credits.available_count == count
    assert usage.reset_credits.status == "available"


def test_missing_reset_credit_data_is_unavailable_not_zero():
    usage = parse_codex_weekly_usage(_payload(80, timedelta(days=2)), now=NOW)
    assert usage.reset_credits.available_count is None
    assert usage.reset_credits.status == "missing"


@pytest.mark.parametrize("credits", [{"available_count": "1"}, {"available_count": -1}, "bad"])
def test_malformed_reset_credit_data_does_not_destroy_valid_quota(credits):
    payload = _payload(80, timedelta(days=2))
    payload["rate_limit_reset_credits"] = credits
    usage = parse_codex_weekly_usage(payload, now=NOW)
    assert usage.remaining_percent == 80
    assert usage.reset_credits.available_count is None
    assert usage.reset_credits.status == "malformed"


def test_missing_weekly_window_is_rejected():
    with pytest.raises(ValueError, match="weekly"):
        parse_codex_weekly_usage({"rate_limit": {"primary_window": {"used_percent": 10, "limit_window_seconds": 18_000, "reset_at": 1}}}, now=NOW)


def _jwt(expiry: datetime) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps({"exp": expiry.timestamp()}).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def test_credentials_are_loaded_from_codex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "must-not-be-used", "tokens": {"access_token": _jwt(NOW + timedelta(hours=1)), "account_id": "acct"}}))
    credentials = load_codex_oauth_credentials(now=NOW)
    assert credentials is not None
    assert credentials.account_id == "acct"
    assert credentials.access_token != "must-not-be-used"


def test_expired_credentials_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(json.dumps({"tokens": {"access_token": _jwt(NOW - timedelta(seconds=1)), "account_id": "acct"}}))
    assert load_codex_oauth_credentials(now=NOW) is None


@pytest.mark.parametrize("status_code", [401, 403])
def test_auth_rejection_does_not_retry_with_api_key(status_code, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "api-key-must-not-be-sent")
    response = MagicMock(status_code=status_code)
    with (
        patch("auto_coder.codex_usage_checker.load_codex_oauth_credentials") as load_credentials,
        patch("auto_coder.codex_usage_checker.httpx.get", return_value=response) as get,
    ):
        load_credentials.return_value.access_token = "oauth-token"
        load_credentials.return_value.account_id = "account-id"
        assert get_codex_weekly_usage(now=NOW) is None
        get.assert_called_once()
        headers = get.call_args.kwargs["headers"]
        assert get.call_args.args[0] == CODEX_USAGE_URL
        assert headers == {
            "Authorization": "Bearer oauth-token",
            "ChatGPT-Account-Id": "account-id",
            "Accept": "application/json",
            "User-Agent": "codex_cli_rs",
            "originator": "codex_cli_rs",
        }
        assert "api-key-must-not-be-sent" not in str(get.call_args)


def test_fetch_failure_fails_closed():
    with (
        patch("auto_coder.codex_usage_checker.load_codex_oauth_credentials") as load_credentials,
        patch("auto_coder.codex_usage_checker.httpx.get", side_effect=httpx.ConnectError("offline")),
    ):
        load_credentials.return_value.access_token = "oauth-token"
        load_credentials.return_value.account_id = "account-id"
        assert get_codex_weekly_usage(now=NOW) is None


def test_malformed_response_fails_closed():
    response = MagicMock(status_code=200)
    response.json.return_value = {"unexpected": True}
    with (
        patch("auto_coder.codex_usage_checker.load_codex_oauth_credentials") as load_credentials,
        patch("auto_coder.codex_usage_checker.httpx.get", return_value=response),
    ):
        load_credentials.return_value.access_token = "oauth-token"
        load_credentials.return_value.account_id = "account-id"
        assert get_codex_weekly_usage(now=NOW) is None
