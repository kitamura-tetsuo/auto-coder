"""Remote-management building blocks.

Importing this package intentionally does not initialize Firebase or open the
management-operation journal.
"""

from .identity import AdmissionResult, AdmissionStatus, FirebaseOperatorIdentity, IdentityConfig, ReasonCode, VerifiedPrincipal
from .operation_store import ManagementOperationStore

__all__ = [
    "AdmissionResult",
    "AdmissionStatus",
    "FirebaseOperatorIdentity",
    "IdentityConfig",
    "ManagementOperationStore",
    "ReasonCode",
    "VerifiedPrincipal",
]
