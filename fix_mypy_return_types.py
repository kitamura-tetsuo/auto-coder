import re
with open('src/auto_coder/review_audit.py', 'r') as f:
    content = f.read()

content = content.replace("def get_recent_history(self, repository: str, limit: int = 50, high_water_mark_seq: Optional[int] = None) -> List[ReviewAuditRecord]:", "def get_recent_history(self, repository: str, limit: int = 50, high_water_mark_seq: Optional[int] = None) -> AuditReadResult:")
content = content.replace("def get_related_evaluations(self, repository: str, target_type: str, target_number: str, review_kind: Optional[str] = None) -> List[ReviewAuditRecord]:", "def get_related_evaluations(self, repository: str, target_type: str, target_number: str, review_kind: Optional[str] = None) -> AuditReadResult:")
content = content.replace("                    return None\n\n                eval_record = self._row_to_evaluation(row)", "                    return AuditSingleReadResult(health=StorageHealth.AVAILABLE, record=None)\n\n                eval_record = self._row_to_evaluation(row)")

with open('src/auto_coder/review_audit.py', 'w') as f:
    f.write(content)
