"""Remote-management building blocks.

Importing this package intentionally does not initialize Firebase.
"""

from .identity import AdmissionResult, AdmissionStatus, FirebaseOperatorIdentity, IdentityConfig, ReasonCode, VerifiedPrincipal

__all__ = [
    "AdmissionResult",
    "AdmissionStatus",
    "FirebaseOperatorIdentity",
    "IdentityConfig",
    "ReasonCode",
    "VerifiedPrincipal",
]
