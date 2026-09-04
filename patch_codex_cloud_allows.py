import sys

def modify_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()

    # Replace codex_cloud_quota_allows_task function
    old_func = """def codex_cloud_quota_allows_task() -> bool:
    usage = get_codex_weekly_usage()
    if usage is None:
        return False
    decision = "start" if usage.can_start_task else "skip"
    credits = usage.reset_credits.available_count
    credits_display = str(credits) if credits is not None else f"unavailable ({usage.reset_credits.status})"
    logger.info(f"Codex weekly quota: remaining={usage.remaining_percent:.1f}%, " f"reset_at={usage.reset_at.isoformat()}, days_until_reset={usage.days_until_reset}, " f"required={usage.minimum_remaining_percent:.1f}%, reset_credits={credits_display}, decision={decision}")
    return usage.can_start_task"""

    new_func = """def codex_cloud_quota_allows_task(strategy: str = "surplus") -> bool:
    usage = get_codex_weekly_usage()
    if usage is None:
        return False

    can_start = usage.has_remaining_quota if strategy == "burst" else usage.meets_reserve_threshold
    decision = "start" if can_start else "skip"
    credits = usage.reset_credits.available_count
    credits_display = str(credits) if credits is not None else f"unavailable ({usage.reset_credits.status})"
    logger.info(f"Codex weekly quota (strategy={strategy}): remaining={usage.remaining_percent:.1f}%, " f"reset_at={usage.reset_at.isoformat()}, days_until_reset={usage.days_until_reset}, " f"required={usage.minimum_remaining_percent:.1f}%, reset_credits={credits_display}, decision={decision}")
    return can_start"""

    content = content.replace(old_func, new_func)

    with open(filepath, 'w') as f:
        f.write(content)

modify_file('src/auto_coder/codex_usage_checker.py')
