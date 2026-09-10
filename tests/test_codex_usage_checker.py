import base64
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from auto_coder.codex_usage_checker import (
    codex_cloud_quota_allows_task,
    get_codex_weekly_usage,
    load_codex_oauth_credentials,
    parse_codex_weekly_usage,
)

NOW = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)


def _payload(remaining: float, reset_delta: timedelta) -> dict[str, object]:
    return {
        "rateLimits": {
            "primary": {
                "usedPercent": 1,
                "windowDurationMins": 300,
                "resetsAt": (NOW + timedelta(hours=2)).timestamp(),
            },
            "secondary": {
                "usedPercent": 100 - remaining,
                "windowDurationMins": 10_080,
                "resetsAt": (NOW + reset_delta).timestamp(),
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


def test_quota_facts_separate_remaining_quota_from_reserve_threshold():
    usage = parse_codex_weekly_usage(_payload(12, timedelta(days=2)), now=NOW)
    assert usage.has_remaining_quota is True
    assert usage.meets_reserve_threshold is False
    assert usage.allows_task("burst") is True
    assert usage.allows_task("surplus") is False


def test_burst_rejects_confirmed_exhaustion_without_using_reset_credit():
    payload = _payload(0, timedelta(days=2))
    payload["rateLimitResetCredits"] = {"availableCount": 1}
    usage = parse_codex_weekly_usage(payload, now=NOW)
    assert usage.has_remaining_quota is False
    assert usage.allows_task("burst") is False
    assert usage.reset_credits.available_count == 1


@pytest.mark.parametrize(("strategy", "expected"), [("burst", True), ("surplus", False)])
def test_cloud_quota_guard_uses_strategy(strategy, expected):
    usage = parse_codex_weekly_usage(_payload(12, timedelta(days=2)), now=NOW)
    with patch("auto_coder.codex_usage_checker.get_codex_weekly_usage", return_value=usage):
        assert codex_cloud_quota_allows_task(strategy) is expected


def test_primary_window_is_used_when_it_is_weekly():
    payload = _payload(80, timedelta(days=2))
    rate_limit = payload["rateLimits"]
    assert isinstance(rate_limit, dict)
    rate_limit["primary"], rate_limit["secondary"] = rate_limit["secondary"], None
    usage = parse_codex_weekly_usage(payload, now=NOW)
    assert usage.remaining_percent == 80


@pytest.mark.parametrize("count", [3, 0])
def test_reset_credit_count_is_parsed_without_an_extra_request(count):
    payload = _payload(80, timedelta(days=2))
    payload["rateLimitResetCredits"] = {"availableCount": count}
    usage = parse_codex_weekly_usage(payload, now=NOW)
    assert usage.reset_credits.available_count == count
    assert usage.reset_credits.status == "available"


def test_missing_reset_credit_data_is_unavailable_not_zero():
    usage = parse_codex_weekly_usage(_payload(80, timedelta(days=2)), now=NOW)
    assert usage.reset_credits.available_count is None
    assert usage.reset_credits.status == "missing"


@pytest.mark.parametrize("credits", [{"availableCount": "1"}, {"availableCount": -1}, "bad"])
def test_malformed_reset_credit_data_does_not_destroy_valid_quota(credits):
    payload = _payload(80, timedelta(days=2))
    payload["rateLimitResetCredits"] = credits
    usage = parse_codex_weekly_usage(payload, now=NOW)
    assert usage.remaining_percent == 80
    assert usage.reset_credits.available_count is None
    assert usage.reset_credits.status == "malformed"


def test_missing_weekly_window_is_rejected():
    with pytest.raises(ValueError, match="weekly"):
        parse_codex_weekly_usage({"rateLimits": {"primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": 1}}}, now=NOW)


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


@pytest.mark.parametrize("error", [FileNotFoundError(), TimeoutError(), EOFError(), ValueError("secret"), OSError()])
def test_fetch_failure_fails_closed(error):
    with patch("auto_coder.codex_usage_checker.read_account_data", side_effect=error):
        assert get_codex_weekly_usage(now=NOW) is None


def test_successful_usage_retrieval_preserves_reset_credits_in_one_request():
    payload = _payload(75, timedelta(days=2))
    payload["rateLimitResetCredits"] = {"availableCount": 2}
    with patch("auto_coder.codex_usage_checker.read_account_data", return_value=payload) as read:
        usage = get_codex_weekly_usage(now=NOW)
    assert usage is not None
    assert usage.remaining_percent == 75
    assert usage.reset_credits.available_count == 2
    read.assert_called_once_with("account/rateLimits/read")


def test_malformed_response_fails_closed():
    with patch("auto_coder.codex_usage_checker.read_account_data", return_value={"unexpected": True}):
        assert get_codex_weekly_usage(now=NOW) is None


def test_codex_bucket_is_selected_over_other_model_and_legacy_view():
    payload = _payload(10, timedelta(days=2))
    payload["rateLimitsByLimitId"] = {"codex_other": payload["rateLimits"], "codex": _payload(80, timedelta(days=2))["rateLimits"]}
    assert parse_codex_weekly_usage(payload, now=NOW).remaining_percent == 80


def test_missing_codex_bucket_does_not_use_another_models_quota():
    payload = _payload(80, timedelta(days=2))
    payload["rateLimitsByLimitId"] = {"codex_other": payload["rateLimits"]}
    with pytest.raises(ValueError, match="missing Codex"):
        parse_codex_weekly_usage(payload, now=NOW)


@pytest.mark.parametrize("field", ["usedPercent", "windowDurationMins", "resetsAt"])
@pytest.mark.parametrize("value", [None, True, "10", float("nan"), float("inf")])
def test_invalid_weekly_values_are_unavailable(field, value):
    payload = _payload(80, timedelta(days=2))
    payload["rateLimits"]["secondary"][field] = value
    with patch("auto_coder.codex_usage_checker.read_account_data", return_value=payload):
        assert get_codex_weekly_usage(now=NOW) is None


def test_app_server_usage_does_not_read_auth_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    with patch("auto_coder.codex_usage_checker.read_account_data", return_value=_payload(80, timedelta(days=2))):
        assert get_codex_weekly_usage(now=NOW).remaining_percent == 80
    assert not (tmp_path / "auth.json").exists()


def test_private_server_errors_are_not_logged():
    with (
        patch("auto_coder.codex_usage_checker.read_account_data", side_effect=ValueError("private token")),
        patch("auto_coder.codex_usage_checker.logger.warning") as warning,
    ):
        assert get_codex_weekly_usage() is None
    warning.assert_called_once_with("Codex app-server weekly usage check failed: ValueError")


@pytest.mark.parametrize("buckets", [[], "invalid", 1])
def test_malformed_bucket_map_is_not_treated_as_absent(buckets):
    payload = _payload(80, timedelta(days=2))
    payload["rateLimitsByLimitId"] = buckets
    with pytest.raises(ValueError, match="invalid Codex quota buckets"):
        parse_codex_weekly_usage(payload, now=NOW)
