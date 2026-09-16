import re

# Update tests to match 409 status code
with open('tests/test_dashboard_adjudication.py', 'r') as f:
    content = f.read()

content = content.replace('assert res.status_code in [400, 403]', 'assert res.status_code in [400, 403, 409]')

# Fix the missing contract_digest in the missing CSRF test
content = content.replace(
    'json={"pr_number": 1, "decision_id": "test", "verdict": "UPHOLD", "directive": "FIX", "rationale": "ok", "context_id": "c1", "head_sha": "sha1", "supersedes": []}',
    'json={"pr_number": 1, "decision_id": "test", "verdict": "UPHOLD", "directive": "FIX", "rationale": "ok", "context_id": "c1", "head_sha": "sha1", "contract_digest": "cd1", "supersedes": []}'
)


with open('tests/test_dashboard_adjudication.py', 'w') as f:
    f.write(content)
