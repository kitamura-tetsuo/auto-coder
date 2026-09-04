import sys

def patch_file(filepath, replacements):
    with open(filepath, 'r') as f:
        content = f.read()

    for old, new in replacements:
        content = content.replace(old, new)

    with open(filepath, 'w') as f:
        f.write(content)

patch_file('tests/test_codex_usage_checker.py', [
    ('assert allowed.can_start_task is True', 'assert allowed.meets_reserve_threshold is True\n    assert allowed.has_remaining_quota is True'),
    ('assert denied.can_start_task is False', 'assert denied.meets_reserve_threshold is False\n    assert denied.has_remaining_quota is True'),
])

patch_file('tests/test_quota_selector.py', [
    ('assert codex_usage.can_start_task is False', 'assert codex_usage.meets_reserve_threshold is False'),
])
