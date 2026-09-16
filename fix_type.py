import re
with open('src/auto_coder/review_audit.py', 'r') as f:
    content = f.read()

# Wait, let's fix get_evaluation which somehow returned None?
content = content.replace(
"""        conn, health = self._connect_readonly(repository)
        if not conn:
            return AuditSingleReadResult(health=health, record=None)""",
"""        conn, health = self._connect_readonly(repository)
        if not conn:
            return AuditSingleReadResult(health=health, record=None)""")

with open('src/auto_coder/review_audit.py', 'w') as f:
    f.write(content)
