"""Usage amount CLI command for Auto-Coder.

Displays usage amount, quota utilization, and rate limit status
for Claude (Anthropic OAuth) and Codex (ChatGPT OAuth).
"""

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from typing import List, Optional

import click

from .claude_usage_checker import check_claude_usage, resolve_claude_oauth_token
from .codex_usage_checker import get_codex_weekly_usage, load_codex_oauth_credentials
from .logger_config import setup_logger


@dataclass
class UsageWindowSummary:
    name: str = ""
    utilization_percent: Optional[float] = None
    remaining_percent: Optional[float] = None
    resets_at: Optional[str] = None


@dataclass
class ExtraUsageSummary:
    is_enabled: Optional[bool] = None
    monthly_limit: Optional[float] = None
    used_credits: Optional[float] = None
    utilization_percent: Optional[float] = None
    currency: Optional[str] = None
    disabled_reason: Optional[str] = None


@dataclass
class ClaudeUsageReport:
    available: bool = False
    status: str = "unknown"
    message: str = ""
    is_quota_insufficient: bool = False
    windows: List[UsageWindowSummary] = field(default_factory=list)
    extra_usage: ExtraUsageSummary = field(default_factory=ExtraUsageSummary)


@dataclass
class CodexUsageReport:
    available: bool = False
    status: str = "unknown"
    message: str = ""
    can_start_task: bool = False
    remaining_percent: Optional[float] = None
    used_percent: Optional[float] = None
    reset_at: Optional[str] = None
    days_until_reset: Optional[int] = None
    minimum_remaining_percent: Optional[float] = None


@dataclass
class CombinedUsageReport:
    claude: Optional[ClaudeUsageReport] = None
    codex: Optional[CodexUsageReport] = None


def _get_claude_usage_report(
    token: Optional[str] = None,
    use_cache: bool = True,
) -> ClaudeUsageReport:
    """Fetch and construct Claude usage report."""
    resolved_token = resolve_claude_oauth_token(token)
    if not resolved_token:
        return ClaudeUsageReport(
            available=False,
            status="missing_credentials",
            message="No Claude OAuth token found. Please login via Claude Code CLI or set CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_AUTH_TOKEN.",
        )

    quota = check_claude_usage(token=token, use_cache=use_cache)
    windows: List[UsageWindowSummary] = []

    if quota.five_hour.utilization is not None:
        windows.append(
            UsageWindowSummary(
                name="5-hour Window",
                utilization_percent=quota.five_hour.utilization,
                remaining_percent=quota.five_hour.remaining_percent,
                resets_at=quota.five_hour.resets_at,
            )
        )

    if quota.seven_day.utilization is not None:
        windows.append(
            UsageWindowSummary(
                name="7-day Window",
                utilization_percent=quota.seven_day.utilization,
                remaining_percent=quota.seven_day.remaining_percent,
                resets_at=quota.seven_day.resets_at,
            )
        )

    if quota.seven_day_sonnet.utilization is not None:
        windows.append(
            UsageWindowSummary(
                name="7-day Sonnet Window",
                utilization_percent=quota.seven_day_sonnet.utilization,
                remaining_percent=quota.seven_day_sonnet.remaining_percent,
                resets_at=quota.seven_day_sonnet.resets_at,
            )
        )

    if quota.seven_day_opus.utilization is not None:
        windows.append(
            UsageWindowSummary(
                name="7-day Opus Window",
                utilization_percent=quota.seven_day_opus.utilization,
                remaining_percent=quota.seven_day_opus.remaining_percent,
                resets_at=quota.seven_day_opus.resets_at,
            )
        )

    if quota.seven_day_oauth_apps.utilization is not None:
        windows.append(
            UsageWindowSummary(
                name="7-day OAuth Apps Window",
                utilization_percent=quota.seven_day_oauth_apps.utilization,
                remaining_percent=quota.seven_day_oauth_apps.remaining_percent,
                resets_at=quota.seven_day_oauth_apps.resets_at,
            )
        )

    extra = ExtraUsageSummary(
        is_enabled=quota.extra_usage.is_enabled,
        monthly_limit=quota.extra_usage.monthly_limit,
        used_credits=quota.extra_usage.used_credits,
        utilization_percent=quota.extra_usage.utilization,
        currency=quota.extra_usage.currency,
        disabled_reason=quota.extra_usage.disabled_reason,
    )

    if not windows and quota.extra_usage.is_enabled is None:
        return ClaudeUsageReport(
            available=False,
            status="fetch_failed",
            message="Failed to retrieve Claude usage data from Anthropic API.",
        )

    status_str = "quota_insufficient" if quota.is_quota_insufficient else "ok"
    msg = quota.reason if quota.is_quota_insufficient else "Quota is sufficient."

    return ClaudeUsageReport(
        available=True,
        status=status_str,
        message=msg,
        is_quota_insufficient=quota.is_quota_insufficient,
        windows=windows,
        extra_usage=extra,
    )


def _get_codex_usage_report() -> CodexUsageReport:
    """Fetch and construct Codex usage report."""
    credentials = load_codex_oauth_credentials()
    if credentials is None:
        return CodexUsageReport(
            available=False,
            status="missing_credentials",
            message="Codex OAuth credentials are missing, expired, or invalid. Please login via Codex CLI.",
        )

    usage = get_codex_weekly_usage()
    if usage is None:
        return CodexUsageReport(
            available=False,
            status="fetch_failed",
            message="Failed to retrieve Codex weekly quota from ChatGPT API.",
        )

    status_str = "ok" if usage.can_start_task else "quota_insufficient"
    msg = "Quota is sufficient to start tasks." if usage.can_start_task else f"Remaining quota ({usage.remaining_percent:.1f}%) is below minimum required threshold ({usage.minimum_remaining_percent:.1f}%)."

    return CodexUsageReport(
        available=True,
        status=status_str,
        message=msg,
        can_start_task=usage.can_start_task,
        remaining_percent=usage.remaining_percent,
        used_percent=100.0 - usage.remaining_percent,
        reset_at=usage.reset_at.isoformat(),
        days_until_reset=usage.days_until_reset,
        minimum_remaining_percent=usage.minimum_remaining_percent,
    )


def _print_claude_report(report: ClaudeUsageReport, no_color: bool) -> None:
    """Print Claude usage report to console."""
    if no_color:
        click.echo("Claude Usage (Anthropic OAuth):")
    else:
        click.secho("🤖 Claude Usage (Anthropic OAuth):", bold=True, fg="blue")

    if not report.available:
        icon = "[ERR]" if no_color else "❌"
        msg = f"  {icon} {report.message}"
        if no_color:
            click.echo(msg)
        else:
            click.secho(msg, fg="red")
        return

    # Quota status banner
    if report.is_quota_insufficient:
        icon = "[WARN]" if no_color else "⚠️ "
        status_line = f"  {icon} Quota Status: Insufficient ({report.message})"
        if no_color:
            click.echo(status_line)
        else:
            click.secho(status_line, fg="yellow")
    else:
        icon = "[OK]" if no_color else "✅"
        status_line = f"  {icon} Quota Status: OK ({report.message})"
        if no_color:
            click.echo(status_line)
        else:
            click.secho(status_line, fg="green")

    # Windows
    for win in report.windows:
        util_str = f"{win.utilization_percent:.1f}%" if win.utilization_percent is not None else "N/A"
        rem_str = f"{win.remaining_percent:.1f}%" if win.remaining_percent is not None else "N/A"
        resets_str = f" (resets at {win.resets_at})" if win.resets_at else ""
        click.echo(f"    • {win.name}: {util_str} used, {rem_str} remaining{resets_str}")

    # Extra usage
    if report.extra_usage.is_enabled is not None:
        enabled_str = "Enabled" if report.extra_usage.is_enabled else "Disabled"
        details: List[str] = [f"Status: {enabled_str}"]
        if report.extra_usage.monthly_limit is not None:
            currency = report.extra_usage.currency or "$"
            details.append(f"Limit: {currency}{report.extra_usage.monthly_limit:.2f}")
        if report.extra_usage.used_credits is not None:
            currency = report.extra_usage.currency or "$"
            details.append(f"Used: {currency}{report.extra_usage.used_credits:.2f}")
        if report.extra_usage.utilization_percent is not None:
            details.append(f"Utilization: {report.extra_usage.utilization_percent:.1f}%")
        if report.extra_usage.disabled_reason:
            details.append(f"Disabled reason: {report.extra_usage.disabled_reason}")

        click.echo(f"    • Extra Usage: {', '.join(details)}")


def _print_codex_report(report: CodexUsageReport, no_color: bool) -> None:
    """Print Codex usage report to console."""
    if no_color:
        click.echo("Codex Usage (ChatGPT OAuth):")
    else:
        click.secho("🤖 Codex Usage (ChatGPT OAuth):", bold=True, fg="blue")

    if not report.available:
        icon = "[ERR]" if no_color else "❌"
        msg = f"  {icon} {report.message}"
        if no_color:
            click.echo(msg)
        else:
            click.secho(msg, fg="red")
        return

    # Quota status banner
    if not report.can_start_task:
        icon = "[WARN]" if no_color else "⚠️ "
        status_line = f"  {icon} Quota Status: Insufficient ({report.message})"
        if no_color:
            click.echo(status_line)
        else:
            click.secho(status_line, fg="yellow")
    else:
        icon = "[OK]" if no_color else "✅"
        status_line = f"  {icon} Quota Status: OK ({report.message})"
        if no_color:
            click.echo(status_line)
        else:
            click.secho(status_line, fg="green")

    # Weekly quota details
    rem_str = f"{report.remaining_percent:.1f}%" if report.remaining_percent is not None else "N/A"
    used_str = f"{report.used_percent:.1f}%" if report.used_percent is not None else "N/A"
    resets_str = f" (resets at {report.reset_at})" if report.reset_at else ""
    click.echo(f"    • Weekly Window: {used_str} used, {rem_str} remaining{resets_str}")

    days_str = f"{report.days_until_reset} day(s)" if report.days_until_reset is not None else "N/A"
    min_req_str = f"{report.minimum_remaining_percent:.1f}%" if report.minimum_remaining_percent is not None else "N/A"
    allow_str = "Yes" if report.can_start_task else "No"
    click.echo(f"    • Reset Countdown: {days_str} until reset (required minimum: {min_req_str})")
    click.echo(f"    • Task Start Allowed: {allow_str}")


@click.command(name="usage-amount")
@click.argument(
    "target",
    type=click.Choice(["all", "claude", "codex"], case_sensitive=False),
    required=False,
    default="all",
)
@click.option(
    "--backend",
    "-b",
    "backend_option",
    type=click.Choice(["all", "claude", "codex"], case_sensitive=False),
    default=None,
    help="Target backend to check usage for (all|claude|codex).",
)
@click.option(
    "--token",
    type=str,
    default=None,
    help="Explicit Claude OAuth token to use for verification.",
)
@click.option(
    "--no-cache",
    is_flag=True,
    default=False,
    help="Bypass in-memory cache and fetch fresh usage data.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output results in JSON format.",
)
def usage_amount(
    target: str,
    backend_option: Optional[str],
    token: Optional[str],
    no_cache: bool,
    as_json: bool,
) -> None:
    """Check AI backend quota and usage amounts (Claude and Codex).

    Examples:
        # Check usage for all backends
        auto-coder usage-amount

        # Check Claude usage only
        auto-coder usage-amount claude
        auto-coder usage-amount --backend claude

        # Check Codex usage only
        auto-coder usage-amount codex
        auto-coder usage-amount --backend codex

        # Output JSON
        auto-coder usage-amount --json
    """
    setup_logger(stream=sys.stderr)

    selected = (backend_option or target or "all").lower()

    include_claude = selected in ("all", "claude")
    include_codex = selected in ("all", "codex")

    claude_report: Optional[ClaudeUsageReport] = None
    codex_report: Optional[CodexUsageReport] = None

    if include_claude:
        claude_report = _get_claude_usage_report(token=token, use_cache=not no_cache)

    if include_codex:
        codex_report = _get_codex_usage_report()

    if as_json:
        combined = CombinedUsageReport(claude=claude_report, codex=codex_report)
        payload = asdict(combined)
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    no_color = "NO_COLOR" in os.environ

    if include_claude and claude_report is not None:
        _print_claude_report(claude_report, no_color)

    if include_claude and include_codex:
        click.echo()

    if include_codex and codex_report is not None:
        _print_codex_report(codex_report, no_color)
