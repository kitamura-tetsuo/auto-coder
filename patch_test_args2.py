import sys

def modify_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()

    # The tests need a fixture fixed_now or we can just import datetime and use a mocked one
    import datetime

    replacements = [
        ("config = LLMBackendConfiguration(\n        quota_selection={\"strategy\": \"burst\"},\n        backends={\"codex-cloud\": BackendConfig(name=\"codex-cloud\", backend_type=\"codex-cloud\")}\n    )",
         "config = LLMBackendConfiguration(backends={\"codex-cloud\": BackendConfig(name=\"codex-cloud\", backend_type=\"codex-cloud\")})\n    config.quota_selection_strategy = \"burst\""),
        ("config = LLMBackendConfiguration(\n        quota_selection={\"strategy\": \"surplus\"},\n        backends={\"codex-cloud\": BackendConfig(name=\"codex-cloud\", backend_type=\"codex-cloud\")}\n    )",
         "config = LLMBackendConfiguration(backends={\"codex-cloud\": BackendConfig(name=\"codex-cloud\", backend_type=\"codex-cloud\")})\n    config.quota_selection_strategy = \"surplus\""),
    ]

    for old, new in replacements:
        content = content.replace(old, new)

    with open(filepath, 'w') as f:
        f.write(content)

modify_file('tests/test_quota_selector.py')
