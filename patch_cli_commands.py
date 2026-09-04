import sys

def modify_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()

    # In _get_codex_usage_report, replace can_start_task with meets_reserve_threshold for usage objects
    # Note: the report itself still has a can_start_task property, but it's based on meets_reserve_threshold by default
    # If the user runs `auto-coder usage`, it typically means standard threshold logic, or we can just leave it as meets_reserve_threshold.

    old_block = """    status_str = "ok" if usage.can_start_task else "quota_insufficient"
    msg = "Quota is sufficient to start tasks." if usage.can_start_task else f"Remaining quota ({usage.remaining_percent:.1f}%) is below minimum required threshold ({usage.minimum_remaining_percent:.1f}%)."

    return CodexUsageReport(
        available=True,
        status=status_str,
        message=msg,
        can_start_task=usage.can_start_task,"""

    new_block = """    status_str = "ok" if usage.meets_reserve_threshold else "quota_insufficient"
    msg = "Quota is sufficient to start tasks." if usage.meets_reserve_threshold else f"Remaining quota ({usage.remaining_percent:.1f}%) is below minimum required threshold ({usage.minimum_remaining_percent:.1f}%)."

    return CodexUsageReport(
        available=True,
        status=status_str,
        message=msg,
        can_start_task=usage.meets_reserve_threshold,"""

    content = content.replace(old_block, new_block)

    with open(filepath, 'w') as f:
        f.write(content)

modify_file('src/auto_coder/cli_commands_usage.py')
