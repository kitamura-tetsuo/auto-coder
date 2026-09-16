from typing import Optional

from ..review_audit import ReviewAuditStore

_global_audit_store: Optional[ReviewAuditStore] = None


def get_review_audit_store() -> ReviewAuditStore:
    global _global_audit_store
    if _global_audit_store is None:
        _global_audit_store = ReviewAuditStore()
    return _global_audit_store
