import sys

def modify_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()

    # The tests need a fixture fixed_now or we can just import datetime and use a mocked one
    import datetime

    replacements = [
        ("def test_codex_cloud_burst_strategy_bypasses_reserve_threshold(fixed_now):",
         "def test_codex_cloud_burst_strategy_bypasses_reserve_threshold():\n    fixed_now = datetime.now(timezone.utc)"),
        ("def test_codex_cloud_burst_strategy_rejects_zero_quota(fixed_now):",
         "def test_codex_cloud_burst_strategy_rejects_zero_quota():\n    fixed_now = datetime.now(timezone.utc)"),
        ("def test_codex_cloud_surplus_strategy_rejects_below_reserve(fixed_now):",
         "def test_codex_cloud_surplus_strategy_rejects_below_reserve():\n    fixed_now = datetime.now(timezone.utc)")
    ]

    for old, new in replacements:
        content = content.replace(old, new)

    with open(filepath, 'w') as f:
        f.write(content)

modify_file('tests/test_quota_selector.py')
