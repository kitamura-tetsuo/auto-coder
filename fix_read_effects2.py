import re
with open('src/auto_coder/review_audit.py', 'r') as f:
    content = f.read()

# Update get_evaluation again because it failed previously
get_eval_old = """    def get_evaluation(self, repository: str, review_id: str) -> Optional[ReviewAuditRecord]:
        \"\"\"Gets a single evaluation and its interactions/effects.\"\"\"
        conn = self._ensure_db(repository)
        if not conn:
            return None"""

get_eval_new = """    def get_evaluation(self, repository: str, review_id: str) -> AuditSingleReadResult:
        \"\"\"Gets a single evaluation and its interactions/effects.\"\"\"
        conn, health = self._connect_readonly(repository)
        if not conn:
            return AuditSingleReadResult(health=health, record=None)"""

content = content.replace(get_eval_old, get_eval_new)
content = content.replace("                    return None\n                    \n                eval_record = self._row_to_evaluation(row)", "                    return AuditSingleReadResult(health=StorageHealth.AVAILABLE, record=None)\n                    \n                eval_record = self._row_to_evaluation(row)")
content = content.replace("                return eval_record\n        except Exception as e:\n            logger.error(f\"Failed to get evaluation {review_id}: {e}\")\n            return None", "                return AuditSingleReadResult(health=StorageHealth.AVAILABLE, record=eval_record)\n        except Exception as e:\n            logger.error(f\"Failed to get evaluation {review_id}: {e}\")\n            return AuditSingleReadResult(health=StorageHealth.UNAVAILABLE, record=None)")


with open('src/auto_coder/review_audit.py', 'w') as f:
    f.write(content)
