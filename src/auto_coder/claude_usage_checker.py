"""
Claude OAuth usage checker for Auto-Coder.

Checks usage and rate limit status from Anthropic OAuth usage API
(https://api.anthropic.com/api/oauth/usage) before calling Claude or Claude-Routine backends.
If remaining quota is too low (5-hour window remaining <= 20% or 7-day window remaining <= 5%),
or if rate limit errors (HTTP 429) or extra usage restrictions occur,
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
from typing import Any, List, Optional, Union

from .exceptions import AutoCoderUsageLimitError
from .logger_config import get_logger

logger = get_logger(__name__)

USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
TOKEN_ENDPOINT = "https://platform.claude.com/v1/oauth/token"
BETA_HEADER = "oauth-2025-04-20"
USER_AGENT = "claude-code/2.1.235"
DEFAULT_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
DEFAULT_CACHE_TTL_SECONDS = 60.0
DEFAULT_SCOPES = ["user:profile", "user:inference", "user:sessions:claude_code", "user:mcp_servers"]


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
class ClaudeExtraUsage:
    """Extra usage / overage credit status."""

    is_enabled: Optional[bool] = None
    monthly_limit: Optional[float] = None
    used_credits: Optional[float] = None
    utilization: Optional[float] = None
    currency: Optional[str] = None
    disabled_reason: Optional[str] = None


@dataclass
class ClaudeUsageQuota:
    """Aggregated quota state and threshold check result."""

    five_hour: ClaudeUsageWindow = field(default_factory=ClaudeUsageWindow)
    seven_day: ClaudeUsageWindow = field(default_factory=ClaudeUsageWindow)
    seven_day_sonnet: ClaudeUsageWindow = field(default_factory=ClaudeUsageWindow)
    seven_day_opus: ClaudeUsageWindow = field(default_factory=ClaudeUsageWindow)
    seven_day_oauth_apps: ClaudeUsageWindow = field(default_factory=ClaudeUsageWindow)
    extra_usage: ClaudeExtraUsage = field(default_factory=ClaudeExtraUsage)
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
        path = Path(config_dir_env) / ".credentials.json"
        if path.exists():
            return path
        path_without_dot = Path(config_dir_env) / "credentials.json"
        if path_without_dot.exists():
            return path_without_dot
        return path
    default_path = Path.home() / ".claude" / ".credentials.json"
    if default_path.exists():
        return default_path
    home_dot_path = Path.home() / ".credentials.json"
    if home_dot_path.exists():
        return home_dot_path
    return default_path


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


def _update_credentials_file(
    new_access_token: str,
    new_refresh_token: Optional[str] = None,
    new_expires_at: Optional[int] = None,
) -> None:
    """Update accessToken, refreshToken, and expiresAt in .credentials.json."""
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
            if new_expires_at:
                data["claudeAiOauth"]["expiresAt"] = new_expires_at
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.info(f"Updated Claude OAuth credentials in {path}")
    except (OSError, ValueError) as e:
        logger.warning(f"Failed to update credentials file at {path}: {e}")


def refresh_claude_access_token(
    refresh_token: str,
    scopes: Optional[Union[List[str], str]] = None,
    client_id: Optional[str] = None,
) -> Optional[str]:
    """Exchange refreshToken for a new accessToken using JSON payload with client_id."""
    resolved_client_id = client_id or os.environ.get("CLAUDE_CODE_OAUTH_CLIENT_ID") or DEFAULT_CLIENT_ID

    scope_str = " ".join(DEFAULT_SCOPES)
    if scopes:
        if isinstance(scopes, list):
            scope_str = " ".join(scopes)
        elif isinstance(scopes, str) and scopes.strip():
            scope_str = scopes.strip()

    payload_dict = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": resolved_client_id,
        "scope": scope_str,
    }
    payload = json.dumps(payload_dict).encode("utf-8")

    req = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        new_access_token = data.get("access_token")
        new_refresh_token = data.get("refresh_token")
        expires_in = data.get("expires_in")
        new_expires_at = int(time.time() * 1000 + int(expires_in) * 1000) if expires_in else None
        if new_access_token:
            _update_credentials_file(str(new_access_token), str(new_refresh_token) if new_refresh_token else None, new_expires_at)
            return str(new_access_token)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            logger.warning("Claude OAuth token refresh failed with HTTP 429: Rate limit exceeded")
            return None
        logger.warning(f"Failed to refresh Claude OAuth token (HTTP {e.code}): {e}")
    except Exception as e:
        logger.warning(f"Failed to refresh Claude OAuth token: {e}")
    return None


def resolve_claude_oauth_token(explicit_token: Optional[str] = None) -> Optional[str]:
    """Resolve active OAuth token from parameter, env, or credentials file, proactively refreshing if expired."""
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
            expires_at = oauth_info.get("expiresAt")
            refresh_token = oauth_info.get("refreshToken")
            scopes = oauth_info.get("scopes")

            # Check if token is expired or expiring within 60 seconds (expiresAt is in milliseconds)
            if expires_at and isinstance(expires_at, (int, float)):
                now_ms = time.time() * 1000
                if now_ms >= (expires_at - 60000) and refresh_token:
                    logger.debug("Claude OAuth accessToken is expired or expiring soon; proactively refreshing")
                    refreshed = refresh_claude_access_token(refresh_token, scopes=scopes)
                    if refreshed:
                        return refreshed

            if token and isinstance(token, str):
                return token.strip()
    return None


def fetch_claude_usage_data(token: Optional[str] = None, timeout: float = 10.0) -> Optional[dict]:
    """Fetch usage data from Anthropic OAuth usage API.

    Returns raw response dictionary, a rate-limited dictionary on HTTP 429, or None if fetch fails.
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
        if e.code == 429:
            logger.warning("Claude OAuth usage API returned HTTP 429: Rate limit exceeded")
            return {
                "is_rate_limited": True,
                "http_status": 429,
                "error": {
                    "type": "rate_limit_error",
                    "message": f"Rate limit reached (HTTP 429): {e.reason}",
                },
            }
        elif e.code == 401:
            # Token expired: attempt refresh if refreshToken exists
            logger.debug("Claude OAuth accessToken expired (HTTP 401); attempting refresh")
            creds = _read_credentials_file()
            rtoken = None
            scopes = None
            if creds:
                oauth_info = creds.get("claudeAiOauth")
                if isinstance(oauth_info, dict):
                    rtoken = oauth_info.get("refreshToken")
                    scopes = oauth_info.get("scopes")
            if rtoken and isinstance(rtoken, str):
                refreshed_token = refresh_claude_access_token(rtoken, scopes=scopes)
                if refreshed_token:
                    try:
                        return _make_request(refreshed_token)
                    except urllib.error.HTTPError as retry_http_err:
                        if retry_http_err.code == 429:
                            return {
                                "is_rate_limited": True,
                                "http_status": 429,
                                "error": {
                                    "type": "rate_limit_error",
                                    "message": f"Rate limit reached on refresh retry (HTTP 429): {retry_http_err.reason}",
                                },
                            }
                        logger.debug(f"Retry after token refresh failed with HTTP {retry_http_err.code}: {retry_http_err}")
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

    is_insufficient = False
    reasons: List[str] = []

    # Check for direct rate limit error indicators in response
    if raw_data.get("is_rate_limited") or raw_data.get("http_status") == 429:
        is_insufficient = True
        err_msg = raw_data.get("error", {}).get("message", "Rate limit exceeded (HTTP 429)")
        reasons.append(f"Claude rate limit error: {err_msg}")

    err_obj = raw_data.get("error")
    if isinstance(err_obj, dict) and err_obj.get("type") == "rate_limit_error":
        is_insufficient = True
        reasons.append(f"Claude rate limit error: {err_obj.get('message', 'Rate limit exceeded')}")

    # Check rate_limits container if present
    rate_limits = raw_data.get("rate_limits") if isinstance(raw_data.get("rate_limits"), dict) else raw_data
    rate_limits_dict = rate_limits if isinstance(rate_limits, dict) else {}

    # Helper to parse window
    def _parse_window(data: Any) -> ClaudeUsageWindow:
        if isinstance(data, dict):
            raw_util = data.get("utilization")
            if raw_util is None:
                raw_util = data.get("percent")
            return ClaudeUsageWindow(
                utilization=float(raw_util) if raw_util is not None else None,
                resets_at=data.get("resets_at"),
            )
        return ClaudeUsageWindow()

    five_hour_window = _parse_window(rate_limits_dict.get("five_hour"))
    seven_day_window = _parse_window(rate_limits_dict.get("seven_day"))
    seven_day_sonnet_window = _parse_window(rate_limits_dict.get("seven_day_sonnet"))
    seven_day_opus_window = _parse_window(rate_limits_dict.get("seven_day_opus"))
    seven_day_oauth_apps_window = _parse_window(rate_limits_dict.get("seven_day_oauth_apps"))

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

    # Check 7-day sonnet window
    if seven_day_sonnet_window.remaining_percent is not None:
        if seven_day_sonnet_window.remaining_percent <= seven_day_threshold_pct:
            is_insufficient = True
            reasons.append(f"7-day Sonnet limit remaining {seven_day_sonnet_window.remaining_percent:.1f}% " f"<= threshold {seven_day_threshold_pct:.1f}% (resets at {seven_day_sonnet_window.resets_at or 'N/A'})")

    # Check 7-day opus window
    if seven_day_opus_window.remaining_percent is not None:
        if seven_day_opus_window.remaining_percent <= seven_day_threshold_pct:
            is_insufficient = True
            reasons.append(f"7-day Opus limit remaining {seven_day_opus_window.remaining_percent:.1f}% " f"<= threshold {seven_day_threshold_pct:.1f}% (resets at {seven_day_opus_window.resets_at or 'N/A'})")

    # Check 7-day oauth apps window
    if seven_day_oauth_apps_window.remaining_percent is not None:
        if seven_day_oauth_apps_window.remaining_percent <= seven_day_threshold_pct:
            is_insufficient = True
            reasons.append(f"7-day OAuth apps limit remaining {seven_day_oauth_apps_window.remaining_percent:.1f}% " f"<= threshold {seven_day_threshold_pct:.1f}% (resets at {seven_day_oauth_apps_window.resets_at or 'N/A'})")

    # Check per-model weekly limits array if present
    limits_list = raw_data.get("limits")
    if isinstance(limits_list, list):
        for item in limits_list:
            if isinstance(item, dict):
                percent = item.get("percent")
                if percent is not None:
                    rem = max(0.0, 100.0 - float(percent))
                    if rem <= seven_day_threshold_pct:
                        scope = item.get("scope")
                        scope_dict = scope if isinstance(scope, dict) else {}
                        model = scope_dict.get("model")
                        model_dict = model if isinstance(model, dict) else {}
                        model_name = model_dict.get("display_name") or item.get("kind", "weekly")
                        is_insufficient = True
                        reasons.append(f"Weekly {model_name} limit remaining {rem:.1f}% " f"<= threshold {seven_day_threshold_pct:.1f}% (resets at {item.get('resets_at') or 'N/A'})")

    # Check extra_usage
    extra_usage_raw = raw_data.get("extra_usage") or (rate_limits.get("extra_usage") if isinstance(rate_limits, dict) else None)
    extra_usage = ClaudeExtraUsage()
    if isinstance(extra_usage_raw, dict):
        disabled_reason = extra_usage_raw.get("disabled_reason")
        extra_utilization = float(extra_usage_raw["utilization"]) if extra_usage_raw.get("utilization") is not None else None
        extra_usage = ClaudeExtraUsage(
            is_enabled=extra_usage_raw.get("is_enabled"),
            monthly_limit=float(extra_usage_raw["monthly_limit"]) if extra_usage_raw.get("monthly_limit") is not None else None,
            used_credits=float(extra_usage_raw["used_credits"]) if extra_usage_raw.get("used_credits") is not None else None,
            utilization=extra_utilization,
            currency=extra_usage_raw.get("currency"),
            disabled_reason=disabled_reason,
        )
        if disabled_reason in ("out_of_credits", "org_spend_cap_reached", "org_level_disabled_until", "org_level_disabled"):
            is_insufficient = True
            reasons.append(f"Extra usage disabled: {disabled_reason}")
        elif extra_utilization is not None and (100.0 - extra_utilization) <= five_hour_threshold_pct:
            is_insufficient = True
            reasons.append(f"Extra usage utilization {extra_utilization:.1f}% reached limit")

    quota = ClaudeUsageQuota(
        five_hour=five_hour_window,
        seven_day=seven_day_window,
        seven_day_sonnet=seven_day_sonnet_window,
        seven_day_opus=seven_day_opus_window,
        seven_day_oauth_apps=seven_day_oauth_apps_window,
        extra_usage=extra_usage,
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
        AutoCoderUsageLimitError: When quota is insufficient or rate limits are reached.
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
