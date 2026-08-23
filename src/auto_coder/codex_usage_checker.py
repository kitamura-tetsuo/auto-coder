"""Fail-safe Codex weekly quota checks for Codex Cloud submissions."""

import base64
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

import httpx

from .logger_config import get_logger

logger = get_logger(__name__)

CODEX_USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
WEEK_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class CodexOAuthCredentials:
    access_token: str = ""
    account_id: str = ""


@dataclass(frozen=True)
class CodexWeeklyUsage:
    remaining_percent: float = 0.0
    reset_at: datetime = datetime.min.replace(tzinfo=timezone.utc)
    days_until_reset: int = 0
    minimum_remaining_percent: float = 5.0

    @property
    def can_start_task(self) -> bool:
        return self.remaining_percent >= self.minimum_remaining_percent


def _codex_auth_path() -> Path:
    """Return the Codex-managed auth file, respecting an isolated CODEX_HOME."""
    codex_home = os.environ.get("CODEX_HOME")
    return Path(codex_home).expanduser() / "auth.json" if codex_home else Path.home() / ".codex" / "auth.json"


def _token_expiry(access_token: str) -> Optional[datetime]:
    """Read a JWT expiry without validating or exposing the token."""
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        expiry = decoded.get("exp")
        if isinstance(expiry, (int, float)):
            return datetime.fromtimestamp(expiry, tz=timezone.utc)
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return None


def load_codex_oauth_credentials(now: Optional[datetime] = None) -> Optional[CodexOAuthCredentials]:
    """Load ChatGPT credentials managed by Codex, never the OpenAI API key."""
    try:
        raw = json.loads(_codex_auth_path().read_text(encoding="utf-8"))
        tokens = raw.get("tokens")
        if not isinstance(tokens, dict):
            return None
        access_token = tokens.get("access_token")
        account_id = tokens.get("account_id")
        if not isinstance(access_token, str) or not access_token or not isinstance(account_id, str) or not account_id:
            return None
        expiry = _token_expiry(access_token)
        current_time = now or datetime.now(timezone.utc)
        if expiry is not None and expiry <= current_time:
            logger.warning("Codex ChatGPT OAuth credentials are expired; weekly usage check is unavailable")
            return None
        return CodexOAuthCredentials(access_token=access_token, account_id=account_id)
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _mapping(value: object) -> Optional[Mapping[str, object]]:
    return value if isinstance(value, dict) else None


def parse_codex_weekly_usage(payload: Mapping[str, object], now: Optional[datetime] = None) -> CodexWeeklyUsage:
    """Parse the longest rate-limit window as the weekly Codex quota window."""
    rate_limit = _mapping(payload.get("rate_limit"))
    if rate_limit is None:
        raise ValueError("missing rate_limit")

    windows = []
    for name in ("primary_window", "secondary_window"):
        window = _mapping(rate_limit.get(name))
        if window is not None:
            duration = window.get("limit_window_seconds")
            if isinstance(duration, (int, float)):
                windows.append((float(duration), window))
    if not windows:
        raise ValueError("missing rate-limit windows")

    duration, weekly = max(windows, key=lambda item: item[0])
    if duration < WEEK_SECONDS:
        raise ValueError("weekly rate-limit window is unavailable")
    used_percent = weekly.get("used_percent")
    reset_timestamp = weekly.get("reset_at")
    if not isinstance(used_percent, (int, float)) or not isinstance(reset_timestamp, (int, float)):
        raise ValueError("invalid weekly usage values")
    if not 0 <= float(used_percent) <= 100:
        raise ValueError("weekly used_percent is out of range")

    reset_at = datetime.fromtimestamp(float(reset_timestamp), tz=timezone.utc)
    current_time = now or datetime.now(timezone.utc)
    days_until_reset = max(0, int((reset_at - current_time).total_seconds() // 86400))
    threshold = float((days_until_reset + 1) * 5)
    return CodexWeeklyUsage(
        remaining_percent=100.0 - float(used_percent),
        reset_at=reset_at,
        days_until_reset=days_until_reset,
        minimum_remaining_percent=threshold,
    )


def get_codex_weekly_usage(now: Optional[datetime] = None) -> Optional[CodexWeeklyUsage]:
    """Fetch weekly usage. Any auth, HTTP, or parsing failure denies task creation."""
    credentials = load_codex_oauth_credentials(now=now)
    if credentials is None:
        logger.warning("Codex ChatGPT OAuth credentials are unavailable; skipping Codex Cloud")
        return None
    headers = {
        "Authorization": f"Bearer {credentials.access_token}",
        "ChatGPT-Account-Id": credentials.account_id,
        "Accept": "application/json",
        "User-Agent": "codex_cli_rs",
        "originator": "codex_cli_rs",
    }
    try:
        response = httpx.get(CODEX_USAGE_URL, headers=headers, timeout=15.0)
        if response.status_code in (401, 403):
            logger.warning("Codex usage authentication was rejected; skipping Codex Cloud")
            return None
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            return None
        return parse_codex_weekly_usage(data, now=now)
    except (httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError) as error:
        logger.warning(f"Codex weekly usage check failed; skipping Codex Cloud: {type(error).__name__}")
        return None


def codex_cloud_quota_allows_task() -> bool:
    usage = get_codex_weekly_usage()
    if usage is None:
        return False
    decision = "start" if usage.can_start_task else "skip"
    logger.info(f"Codex weekly quota: remaining={usage.remaining_percent:.1f}%, " f"reset_at={usage.reset_at.isoformat()}, days_until_reset={usage.days_until_reset}, " f"required={usage.minimum_remaining_percent:.1f}%, decision={decision}")
    return usage.can_start_task
