import re
with open('src/auto_coder/review_audit.py', 'r') as f:
    content = f.read()

# Introduce HealthStatus enum and return type wrappers
header_add = """
class StorageHealth(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNINITIALIZED = "UNINITIALIZED"
    UNAVAILABLE = "UNAVAILABLE"

@dataclasses.dataclass
class AuditReadResult:
    health: StorageHealth
    records: List[ReviewAuditRecord]

@dataclasses.dataclass
class AuditSingleReadResult:
    health: StorageHealth
    record: Optional[ReviewAuditRecord]

"""

content = content.replace("class EvaluationLifecycle(str, Enum):", header_add + "class EvaluationLifecycle(str, Enum):")

# Fix _ensure_db and introduce _connect_readonly
conn_logic = """
    def _connect_readonly(self, repository: str) -> Tuple[Optional[sqlite3.Connection], StorageHealth]:
        db_path = self._get_db_path(repository)
        if not db_path.exists():
            return None, StorageHealth.UNINITIALIZED

        try:
            # open read-only URI
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            return conn, StorageHealth.AVAILABLE
        except sqlite3.Error as e:
            logger.error(f"Audit read failed for {repository}: {e}")
            return None, StorageHealth.UNAVAILABLE
        except Exception as e:
            logger.error(f"Audit read error for {repository}: {e}")
            return None, StorageHealth.UNAVAILABLE

"""

content = content.replace("    def _ensure_db(self, repository: str) -> Optional[sqlite3.Connection]:", conn_logic + "    def _ensure_db(self, repository: str) -> Optional[sqlite3.Connection]:")

# Update get_evaluation
get_eval_old = """    def get_evaluation(self, repository: str, review_id: str) -> Optional[ReviewAuditRecord]:
        \"\"\"Gets a single evaluation and its interactions/effects.\"\"\"
        conn = self._ensure_db(repository)
        if not conn:
            return None

        try:
            with conn:"""

get_eval_new = """    def get_evaluation(self, repository: str, review_id: str) -> AuditSingleReadResult:
        \"\"\"Gets a single evaluation and its interactions/effects.\"\"\"
        conn, health = self._connect_readonly(repository)
        if not conn:
            return AuditSingleReadResult(health=health, record=None)

        try:
            with conn:"""

content = content.replace(get_eval_old, get_eval_new)
content = content.replace("                    return None\n                    \n                eval_record = self._row_to_evaluation(row)", "                    return AuditSingleReadResult(health=StorageHealth.AVAILABLE, record=None)\n                    \n                eval_record = self._row_to_evaluation(row)")
content = content.replace("                return eval_record\n        except Exception as e:\n            logger.error(f\"Failed to get evaluation {review_id}: {e}\")\n            return None", "                return AuditSingleReadResult(health=StorageHealth.AVAILABLE, record=eval_record)\n        except Exception as e:\n            logger.error(f\"Failed to get evaluation {review_id}: {e}\")\n            return AuditSingleReadResult(health=StorageHealth.UNAVAILABLE, record=None)")


# Update get_recent_history
hist_old = """    def get_recent_history(self, repository: str, limit: int = 50, high_water_mark_seq: Optional[int] = None) -> List[ReviewAuditRecord]:
        \"\"\"Gets recent evaluations. Page sizes strictly 1-200.\"\"\"
        if not 1 <= limit <= 200:
            logger.error("Page size must be between 1 and 200")
            return []

        conn = self._ensure_db(repository)
        if not conn:
            return []

        try:
            with conn:"""

hist_new = """    def get_recent_history(self, repository: str, limit: int = 50, high_water_mark_seq: Optional[int] = None) -> AuditReadResult:
        \"\"\"Gets recent evaluations. Page sizes strictly 1-200.\"\"\"
        if not 1 <= limit <= 200:
            logger.error("Page size must be between 1 and 200")
            return AuditReadResult(health=StorageHealth.AVAILABLE, records=[])

        conn, health = self._connect_readonly(repository)
        if not conn:
            return AuditReadResult(health=health, records=[])

        try:
            with conn:"""

content = content.replace(hist_old, hist_new)
content = content.replace("                return records\n        except Exception as e:\n            logger.error(f\"Failed to get recent history: {e}\")\n            return []", "                return AuditReadResult(health=StorageHealth.AVAILABLE, records=records)\n        except Exception as e:\n            logger.error(f\"Failed to get recent history: {e}\")\n            return AuditReadResult(health=StorageHealth.UNAVAILABLE, records=[])")

# Update get_related_evaluations
rel_old = """    def get_related_evaluations(self, repository: str, target_type: str, target_number: str, review_kind: Optional[str] = None) -> List[ReviewAuditRecord]:
        \"\"\"Looks up decomposition or other related records.\"\"\"
        conn = self._ensure_db(repository)
        if not conn:
            return []

        try:
            with conn:"""

rel_new = """    def get_related_evaluations(self, repository: str, target_type: str, target_number: str, review_kind: Optional[str] = None) -> AuditReadResult:
        \"\"\"Looks up decomposition or other related records.\"\"\"
        conn, health = self._connect_readonly(repository)
        if not conn:
            return AuditReadResult(health=health, records=[])

        try:
            with conn:"""
content = content.replace(rel_old, rel_new)
content = content.replace("                return records\n        except Exception as e:\n            logger.error(f\"Failed to get related evaluations: {e}\")\n            return []", "                return AuditReadResult(health=StorageHealth.AVAILABLE, records=records)\n        except Exception as e:\n            logger.error(f\"Failed to get related evaluations: {e}\")\n            return AuditReadResult(health=StorageHealth.UNAVAILABLE, records=[])")


with open('src/auto_coder/review_audit.py', 'w') as f:
    f.write(content)
