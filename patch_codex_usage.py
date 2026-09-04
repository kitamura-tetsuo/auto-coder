import sys

def modify_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()

    # Replace can_start_task with has_remaining_quota and meets_reserve_threshold
    content = content.replace(
        "    @property\n    def can_start_task(self) -> bool:\n        return self.remaining_percent >= self.minimum_remaining_percent",
        "    @property\n    def has_remaining_quota(self) -> bool:\n        return self.remaining_percent > 0.0\n\n    @property\n    def meets_reserve_threshold(self) -> bool:\n        return self.remaining_percent >= self.minimum_remaining_percent"
    )

    with open(filepath, 'w') as f:
        f.write(content)

modify_file('src/auto_coder/codex_usage_checker.py')
