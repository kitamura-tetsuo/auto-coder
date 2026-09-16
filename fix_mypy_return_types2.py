import re
with open('src/auto_coder/review_audit.py', 'r') as f:
    content = f.read()

content = content.replace("            logger.error(\"Page size must be between 1 and 200\")\n            return []", "            logger.error(\"Page size must be between 1 and 200\")\n            return AuditReadResult(health=StorageHealth.AVAILABLE, records=[])")
content = content.replace("        conn = self._ensure_db(repository)\n        if not conn:\n            return []", "        conn, health = self._connect_readonly(repository)\n        if not conn:\n            return AuditReadResult(health=health, records=[])")

with open('src/auto_coder/review_audit.py', 'w') as f:
    f.write(content)
