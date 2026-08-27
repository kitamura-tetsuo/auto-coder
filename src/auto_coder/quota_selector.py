"""Quota surplus-based backend selection for high-score tasks.

Selects the backend based on how far its current remaining weekly quota is above
the planned remaining quota at the current point in its quota cycle (quota surplus).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional, Sequence

from dateutil import parser

from .logger_config import get_logger

logger = get_logger(__name__)

WEEK_SECONDS: float = 7.0 * 24.0 * 3600.0  # 604,800 seconds


@dataclass
class BackendQuotaEvaluation:
    """Quota evaluation result for a candidate backend."""

    backend_name: str = ""
    is_eligible: bool = True
    actual_remaining_ratio: Optional[float] = None
    time_until_reset_seconds: Optional[float] = None
    planned_remaining_ratio: Optional[float] = None
    quota_surplus: Optional[float] = None
    reset_at: Optional[datetime] = None
    usage_retrieval_failed: bool = False
    reason: str = ""


def linear_planned_remaining_ratio(
    time_until_reset_seconds: float,
    quota_period_seconds: float = WEEK_SECONDS,
) -> float:
    """Calculate planned remaining quota ratio using a linear consumption curve.

    Args:
        time_until_reset_seconds: Seconds remaining until the quota resets.
        quota_period_seconds: Total duration of the quota cycle (default: 1 week).

    Returns:
        Planned remaining ratio between 0.0 and 1.0.
    """
    if quota_period_seconds <= 0:
        return 0.0
    clamped_seconds = max(0.0, min(quota_period_seconds, time_until_reset_seconds))
    return clamped_seconds / quota_period_seconds


def calculate_quota_surplus(
    actual_remaining_ratio: float,
    planned_remaining_ratio: float,
) -> float:
    """Calculate quota surplus (actual remaining ratio minus planned remaining ratio).

    Positive surplus indicates the backend is being consumed slower than planned.
    Negative surplus indicates the backend is consumed faster than planned.
    """
    return actual_remaining_ratio - planned_remaining_ratio


def evaluate_backend_quota(
    backend_name: str,
    config: Optional[object] = None,
    now: Optional[datetime] = None,
    consumption_curve: Optional[Callable[[float, float], float]] = None,
    quota_period_seconds: float = WEEK_SECONDS,
) -> BackendQuotaEvaluation:
    """Evaluate eligibility and quota surplus for a single backend candidate.

    Args:
        backend_name: Name of the backend to evaluate.
        config: Optional LLMBackendConfiguration instance.
        now: Optional current datetime (defaults to datetime.now(timezone.utc)).
        consumption_curve: Optional callable(time_until_reset_seconds, quota_period_seconds) -> float.
        quota_period_seconds: Quota period in seconds (default: 1 week).

    Returns:
        BackendQuotaEvaluation instance.
    """
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)

    curve_fn = consumption_curve or linear_planned_remaining_ratio

    backend_type = backend_name
    b_cfg = None
    if config is not None and hasattr(config, "get_backend_config"):
        b_cfg = config.get_backend_config(backend_name)
        if b_cfg:
            if getattr(b_cfg, "enabled", True) is False:
                return BackendQuotaEvaluation(
                    backend_name=backend_name,
                    is_eligible=False,
                    reason="Backend is disabled in configuration",
                )
            backend_type = getattr(b_cfg, "backend_type", None) or backend_name

    # Codex Cloud evaluation
    if backend_type == "codex-cloud" or (backend_type == "codex" and backend_name == "codex-cloud"):
        from .codex_usage_checker import get_codex_weekly_usage

        usage = get_codex_weekly_usage(now=current_time)
        if usage is None:
            return BackendQuotaEvaluation(
                backend_name=backend_name,
                is_eligible=True,
                usage_retrieval_failed=True,
                reason="Codex Cloud credentials or usage data unavailable",
            )
        if not usage.can_start_task:
            return BackendQuotaEvaluation(
                backend_name=backend_name,
                is_eligible=False,
                actual_remaining_ratio=usage.remaining_percent / 100.0,
                reset_at=usage.reset_at,
                reason=f"Codex Cloud quota insufficient ({usage.remaining_percent:.1f}% < {usage.minimum_remaining_percent:.1f}%)",
            )

        actual_ratio = usage.remaining_percent / 100.0
        reset_at = usage.reset_at if usage.reset_at.tzinfo else usage.reset_at.replace(tzinfo=timezone.utc)
        time_until_reset = max(0.0, (reset_at - current_time).total_seconds())
        planned_ratio = curve_fn(time_until_reset, quota_period_seconds)
        surplus = calculate_quota_surplus(actual_ratio, planned_ratio)

        return BackendQuotaEvaluation(
            backend_name=backend_name,
            is_eligible=True,
            actual_remaining_ratio=actual_ratio,
            time_until_reset_seconds=time_until_reset,
            planned_remaining_ratio=planned_ratio,
            quota_surplus=surplus,
            reset_at=reset_at,
            reason="Eligible",
        )

    # Standard Codex backend check - check if OAuth weekly quota is available
    if backend_type == "codex":
        from .codex_usage_checker import get_codex_weekly_usage, load_codex_oauth_credentials

        creds = load_codex_oauth_credentials(now=current_time)
        if creds is not None:
            usage = get_codex_weekly_usage(now=current_time)
            if usage is not None:
                if not usage.can_start_task:
                    return BackendQuotaEvaluation(
                        backend_name=backend_name,
                        is_eligible=False,
                        actual_remaining_ratio=usage.remaining_percent / 100.0,
                        reset_at=usage.reset_at,
                        reason=f"Codex quota insufficient ({usage.remaining_percent:.1f}% < {usage.minimum_remaining_percent:.1f}%)",
                    )
                actual_ratio = usage.remaining_percent / 100.0
                reset_at = usage.reset_at if usage.reset_at.tzinfo else usage.reset_at.replace(tzinfo=timezone.utc)
                time_until_reset = max(0.0, (reset_at - current_time).total_seconds())
                planned_ratio = curve_fn(time_until_reset, quota_period_seconds)
                surplus = calculate_quota_surplus(actual_ratio, planned_ratio)
                return BackendQuotaEvaluation(
                    backend_name=backend_name,
                    is_eligible=True,
                    actual_remaining_ratio=actual_ratio,
                    time_until_reset_seconds=time_until_reset,
                    planned_remaining_ratio=planned_ratio,
                    quota_surplus=surplus,
                    reset_at=reset_at,
                    reason="Eligible",
                )
            else:
                return BackendQuotaEvaluation(
                    backend_name=backend_name,
                    is_eligible=True,
                    usage_retrieval_failed=True,
                    reason="Codex OAuth usage data could not be retrieved",
                )

        # Unmetered / API key Codex
        return BackendQuotaEvaluation(
            backend_name=backend_name,
            is_eligible=True,
            quota_surplus=None,
            reason="Eligible (unmetered)",
        )

    # Claude / Claude Routine evaluation
    if backend_type in ("claude-routine", "claude") or backend_name in ("claude-routine", "claude"):
        from .claude_usage_checker import check_claude_usage, resolve_claude_oauth_token

        token = None
        if b_cfg:
            token = getattr(b_cfg, "claude_code_routine_token", None) or getattr(b_cfg, "api_key", None)
        resolved_token = resolve_claude_oauth_token(token)

        if resolved_token:
            quota = check_claude_usage(token=resolved_token, use_cache=True)
            if quota.is_quota_insufficient:
                if quota.reason == "Claude usage data could not be retrieved" or "could not be retrieved" in quota.reason:
                    return BackendQuotaEvaluation(
                        backend_name=backend_name,
                        is_eligible=True,
                        usage_retrieval_failed=True,
                        quota_surplus=None,
                        reason="Eligible with lowered priority (Claude usage data could not be retrieved)",
                    )
                return BackendQuotaEvaluation(
                    backend_name=backend_name,
                    is_eligible=False,
                    reason=f"Claude quota insufficient: {quota.reason}",
                )

            # Select applicable 7-day window
            window = quota.seven_day
            if "opus" in backend_name.lower() and quota.seven_day_opus.utilization is not None:
                window = quota.seven_day_opus
            elif "sonnet" in backend_name.lower() and quota.seven_day_sonnet.utilization is not None:
                window = quota.seven_day_sonnet
            elif window.utilization is None:
                for candidate_win in (quota.seven_day_opus, quota.seven_day_sonnet, quota.seven_day_oauth_apps):
                    if candidate_win.utilization is not None:
                        window = candidate_win
                        break

            if window.remaining_percent is not None and window.resets_at:
                try:
                    reset_dt = parser.parse(str(window.resets_at))
                    if reset_dt.tzinfo is None:
                        reset_dt = reset_dt.replace(tzinfo=timezone.utc)
                    else:
                        reset_dt = reset_dt.astimezone(timezone.utc)

                    actual_ratio = window.remaining_percent / 100.0
                    time_until_reset = max(0.0, (reset_dt - current_time).total_seconds())
                    planned_ratio = curve_fn(time_until_reset, quota_period_seconds)
                    surplus = calculate_quota_surplus(actual_ratio, planned_ratio)

                    return BackendQuotaEvaluation(
                        backend_name=backend_name,
                        is_eligible=True,
                        actual_remaining_ratio=actual_ratio,
                        time_until_reset_seconds=time_until_reset,
                        planned_remaining_ratio=planned_ratio,
                        quota_surplus=surplus,
                        reset_at=reset_dt,
                        reason="Eligible",
                    )
                except Exception as e:
                    logger.debug(f"Failed to parse Claude resets_at '{window.resets_at}': {e}")

        # Claude without resolved OAuth token or unmetered API key
        return BackendQuotaEvaluation(
            backend_name=backend_name,
            is_eligible=True,
            quota_surplus=None,
            reason="Eligible (unmetered or without weekly quota)",
        )

    # Other backends (e.g. antigravity, qwen, auggie, aider, jules)
    return BackendQuotaEvaluation(
        backend_name=backend_name,
        is_eligible=True,
        quota_surplus=None,
        reason="Eligible (unmetered)",
    )


def rank_high_score_backends_by_quota(
    candidate_backends: Sequence[str],
    config: Optional[object] = None,
    now: Optional[datetime] = None,
    consumption_curve: Optional[Callable[[float, float], float]] = None,
    quota_period_seconds: float = WEEK_SECONDS,
) -> List[str]:
    """Rank candidate high-score backends based on quota surplus.

    Evaluates each candidate's weekly quota surplus:
        planned_remaining_ratio = time_until_reset / quota_period
        quota_surplus = actual_remaining_ratio - planned_remaining_ratio

    Candidates are filtered by eligibility and ranked in order:
    1. Backends with measured quota metrics, ordered by quota_surplus descending.
    2. Healthy unmetered backends, retaining stable original order.
    3. Backends whose usage data could not be retrieved, retaining stable original order at lowered priority.

    Args:
        candidate_backends: List of candidate backend names to rank.
        config: Optional LLMBackendConfiguration instance.
        now: Optional current datetime.
        consumption_curve: Optional consumption curve function.
        quota_period_seconds: Quota period in seconds (default: 1 week).

    Returns:
        List of ranked eligible backend names.
    """
    if not candidate_backends:
        return []

    evaluations = [
        evaluate_backend_quota(
            backend_name=b,
            config=config,
            now=now,
            consumption_curve=consumption_curve,
            quota_period_seconds=quota_period_seconds,
        )
        for b in candidate_backends
    ]

    eligible_evals = [e for e in evaluations if e.is_eligible]
    if not eligible_evals:
        logger.warning("No candidate high-score backends were eligible under quota checks; falling back to configured order")
        return list(candidate_backends)

    # Sort key:
    # 1. Backends with quota_surplus (and not usage_retrieval_failed) come first (tier: 0),
    #    sorted by quota_surplus descending (-surplus)
    # 2. Healthy unmetered backends come next (tier: 1), maintaining stable original order
    # 3. Backends whose usage could not be retrieved come last (tier: 2), maintaining stable original order
    def _sort_key(eval_item: BackendQuotaEvaluation) -> tuple:
        if eval_item.usage_retrieval_failed:
            return (2, 0.0)
        if eval_item.quota_surplus is not None:
            return (0, -eval_item.quota_surplus)
        return (1, 0.0)

    ranked_evals = sorted(eligible_evals, key=_sort_key)
    ranked_names = [e.backend_name for e in ranked_evals]

    log_summaries = []
    for e in ranked_evals:
        if e.usage_retrieval_failed:
            log_summaries.append(f"{e.backend_name} (usage unretrieved)")
        elif e.quota_surplus is not None and e.actual_remaining_ratio is not None and e.planned_remaining_ratio is not None:
            log_summaries.append(f"{e.backend_name} (surplus={e.quota_surplus * 100:+.1f}%, " f"actual={e.actual_remaining_ratio * 100:.1f}%, " f"planned={e.planned_remaining_ratio * 100:.1f}%)")
        else:
            log_summaries.append(f"{e.backend_name} (unmetered)")

    logger.info(f"High-score backend quota ranking: {', '.join(log_summaries)} -> Primary: '{ranked_names[0]}'")

    return ranked_names
