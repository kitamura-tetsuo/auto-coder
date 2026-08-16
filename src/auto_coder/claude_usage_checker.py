"""
Claude OAuth usage checker for Auto-Coder.

Checks usage and rate limit status from Anthropic OAuth usage API
(https://api.anthropic.com/api/oauth/usage) before calling Claude or Claude-Routine backends.
If remaining quota is too low (5-hour window remaining <= 20% or 7-day window remaining <= 5%),
it raises AutoCoderUsageLimitError to defer LLM invocations and route to next backend.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .exceptions import AutoCoderUsageLimitError
from .logger_config import get_logger

logger = get_logger(__name__)

USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
TOKEN_ENDPOINT = "https://platform.claude.com/v1/oauth/token"
BETA_HEADER = "oauth-2025-04-20"
USER_AGENT = "claude-code/2.1.228"
DEFAULT_CACHE_TTL_SECONDS = 60.0


@dataclass
class ClaudeUsageWindow:
    """Usage window information (utilization % and reset timestamp)."""

    utilization: Optional[float] = None
    resets_at: Optional[str] = None

    @property
    def remaining_percent(self) -> Optional[float]:
        """Calculate remaining percentage (100.0 - utilization)."""
        if self.utilization is not None:
            return max(0.0, 100.0 - float(self.utilization))
        return None


@dataclass
class ClaudeUsageQuota:
    """Aggregated quota state and threshold check result."""

    five_hour: ClaudeUsageWindow = field(default_factory=ClaudeUsageWindow)
    seven_day: ClaudeUsageWindow = field(default_factory=ClaudeUsageWindow)
    is_quota_insufficient: bool = False
    reason: str = ""
    cached_at: float = field(default_factory=time.time)


_cache_lock = threading.Lock()
_cached_quota: Optional[ClaudeUsageQuota] = None


def clear_claude_usage_cache() -> None:
    """Clear in-memory cached quota state."""
    global _cached_quota
    with _cache_lock:
        _cached_quota = None


def get_claude_credentials_path() -> Path:
    """Get path to .credentials.json in .claude config directory."""
    config_dir_env = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir_env:
        return Path(config_dir_env) / ".credentials.json"
    return Path.home() / ".claude" / ".credentials.json"


def _read_credentials_file() -> Optional[dict]:
    """Read credentials from .credentials.json file."""
    path = get_claude_credentials_path()
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else None
    except (OSError, ValueError) as e:
        logger.debug(f"Failed to read credentials from {path}: {e}")
        return None


def _update_credentials_file(new_access_token: str, new_refresh_token: Optional[str] = None) -> None:
    """Update accessToken and refreshToken in .credentials.json."""
    path = get_claude_credentials_path()
    if not path.exists():
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "claudeAiOauth" in data and isinstance(data["claudeAiOauth"], dict):
            data["claudeAiOauth"]["accessToken"] = new_access_token
            if new_refresh_token:
                data["claudeAiOauth"]["refreshToken"] = new_refresh_token
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.info(f"Updated Claude OAuth credentials in {path}")
    except (OSError, ValueError) as e:
        logger.warning(f"Failed to update credentials file at {path}: {e}")


def refresh_claude_access_token(refresh_token: str) -> Optional[str]:
    """Exchange refreshToken for a new accessToken."""
    try:
        payload = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            TOKEN_ENDPOINT,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        new_access_token = data.get("access_token")
        new_refresh_token = data.get("refresh_token")
        if new_access_token:
            _update_credentials_file(new_access_token, new_refresh_token)
            return str(new_access_token)
    except Exception as e:
        logger.warning(f"Failed to refresh Claude OAuth token: {e}")
    return None


def resolve_claude_oauth_token(explicit_token: Optional[str] = None) -> Optional[str]:
    """Resolve active OAuth token from parameter, env, or credentials file."""
    if explicit_token and explicit_token.strip():
        return explicit_token.strip()

    env_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if env_token and env_token.strip():
        return env_token.strip()

    creds = _read_credentials_file()
    if creds:
        oauth_info = creds.get("claudeAiOauth")
        if isinstance(oauth_info, dict):
            token = oauth_info.get("accessToken")
            if token and isinstance(token, str):
                return token.strip()
    return None


def fetch_claude_usage_data(token: Optional[str] = None, timeout: float = 10.0) -> Optional[dict]:
    """Fetch usage data from Anthropic OAuth usage API.

    Returns raw response dictionary or None if fetch fails.
    """
    resolved_token = resolve_claude_oauth_token(token)
    if not resolved_token:
        logger.debug("No Claude OAuth token available to check usage limits")
        return None

    def _make_request(auth_token: str) -> Optional[dict]:
        headers = {
            "Authorization": f"Bearer {auth_token}",
            "anthropic-beta": BETA_HEADER,
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(USAGE_ENDPOINT, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode("utf-8")
            data = json.loads(content)
            return data if isinstance(data, dict) else None

    try:
        return _make_request(resolved_token)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            # Token expired: attempt refresh if refreshToken exists
            logger.debug("Claude OAuth accessToken expired (HTTP 401); attempting refresh")
            creds = _read_credentials_file()
            rtoken = None
            if creds:
                oauth_info = creds.get("claudeAiOauth")
                if isinstance(oauth_info, dict):
                    rtoken = oauth_info.get("refreshToken")
            if rtoken and isinstance(rtoken, str):
                refreshed_token = refresh_claude_access_token(rtoken)
                if refreshed_token:
                    try:
                        return _make_request(refreshed_token)
                    except Exception as retry_err:
                        logger.debug(f"Retry after token refresh failed: {retry_err}")
            logger.debug(f"Claude OAuth usage endpoint HTTP 401: {e}")
        else:
            logger.debug(f"Claude OAuth usage endpoint HTTP {e.code}: {e}")
    except Exception as e:
        logger.debug(f"Failed to fetch Claude OAuth usage: {e}")

    return None


def check_claude_usage(
    token: Optional[str] = None,
    five_hour_threshold_pct: float = 20.0,
    seven_day_threshold_pct: float = 5.0,
    use_cache: bool = True,
    cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
) -> ClaudeUsageQuota:
    """Check Claude usage quota and determine whether quota is insufficient.

    Args:
        token: Optional explicit token.
        five_hour_threshold_pct: Threshold for remaining 5-hour quota (default: 20.0%).
        seven_day_threshold_pct: Threshold for remaining 7-day quota (default: 5.0%).
        use_cache: Whether to use cached result within TTL.
        cache_ttl: Cache TTL in seconds.

    Returns:
        ClaudeUsageQuota instance.
    """
    global _cached_quota

    now = time.time()
    if use_cache:
        with _cache_lock:
            if _cached_quota is not None and (now - _cached_quota.cached_at) < cache_ttl:
                return _cached_quota

    raw_data = fetch_claude_usage_data(token=token)
    if not raw_data:
        # If API is unreachable or token cannot check usage, default to normal execution
        quota = ClaudeUsageQuota(cached_at=now)
        with _cache_lock:
            _cached_quota = quota
        return quota

    five_hour_data = raw_data.get("five_hour")
    five_hour_window = ClaudeUsageWindow()
    if isinstance(five_hour_data, dict):
        five_hour_window = ClaudeUsageWindow(
            utilization=float(five_hour_data["utilization"]) if five_hour_data.get("utilization") is not None else None,
            resets_at=five_hour_data.get("resets_at"),
        )

    seven_day_data = raw_data.get("seven_day")
    seven_day_window = ClaudeUsageWindow()
    if isinstance(seven_day_data, dict):
        seven_day_window = ClaudeUsageWindow(
            utilization=float(seven_day_data["utilization"]) if seven_day_data.get("utilization") is not None else None,
            resets_at=seven_day_data.get("resets_at"),
        )

    is_insufficient = False
    reasons = []

    # Check 5-hour window: remaining <= threshold (e.g. remaining <= 20.0%)
    if five_hour_window.remaining_percent is not None:
        if five_hour_window.remaining_percent <= five_hour_threshold_pct:
            is_insufficient = True
            reasons.append(f"5-hour limit remaining {five_hour_window.remaining_percent:.1f}% " f"<= threshold {five_hour_threshold_pct:.1f}% (resets at {five_hour_window.resets_at or 'N/A'})")

    # Check 7-day window: remaining <= threshold (e.g. remaining <= 5.0%)
    if seven_day_window.remaining_percent is not None:
        if seven_day_window.remaining_percent <= seven_day_threshold_pct:
            is_insufficient = True
            reasons.append(f"7-day limit remaining {seven_day_window.remaining_percent:.1f}% " f"<= threshold {seven_day_threshold_pct:.1f}% (resets at {seven_day_window.resets_at or 'N/A'})")

    quota = ClaudeUsageQuota(
        five_hour=five_hour_window,
        seven_day=seven_day_window,
        is_quota_insufficient=is_insufficient,
        reason="; ".join(reasons),
        cached_at=now,
    )

    with _cache_lock:
        _cached_quota = quota

    return quota


def check_claude_usage_or_raise(
    token: Optional[str] = None,
    backend_name: str = "claude",
    five_hour_threshold_pct: float = 20.0,
    seven_day_threshold_pct: float = 5.0,
    use_cache: bool = True,
) -> None:
    """Check Claude usage limits and raise AutoCoderUsageLimitError if threshold exceeded.

    Args:
        token: Optional explicit token.
        backend_name: Backend name for logging.
        five_hour_threshold_pct: Threshold for 5-hour limit (default: 20.0%).
        seven_day_threshold_pct: Threshold for 7-day limit (default: 5.0%).
        use_cache: Whether to use cached usage within TTL.

    Raises:
        AutoCoderUsageLimitError: When 5-hour remaining <= 20% or 7-day remaining <= 5%.
    """
    quota = check_claude_usage(
        token=token,
        five_hour_threshold_pct=five_hour_threshold_pct,
        seven_day_threshold_pct=seven_day_threshold_pct,
        use_cache=use_cache,
    )

    if quota.is_quota_insufficient:
        message = f"Claude usage threshold reached for backend '{backend_name}': {quota.reason}"
        logger.warning(message)
        raise AutoCoderUsageLimitError(message)
