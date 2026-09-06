"""Validation and result types for native Parent-Issue reconciliation."""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ParentDeclarationStatus(Enum):
    ABSENT = "absent"
    SUPPORTED = "supported"
    INVALID = "invalid"


@dataclass(frozen=True)
class ParentDeclaration:
    status: ParentDeclarationStatus
    parent_number: Optional[int] = None
    reason: Optional[str] = None


class ParentReconciliationError(RuntimeError):
    """Base class for a graph state from which processing must not continue."""


class ParentSpecificationError(ParentReconciliationError):
    """The declaration is definitively malformed or contradicts GitHub."""


class ParentOperationalError(ParentReconciliationError):
    """GitHub did not authoritatively establish the relationship state."""


_CANDIDATE = re.compile(r"^(?:parent-issue|parent_issue|parent issue):(.*)$", re.IGNORECASE)
_SUPPORTED_VALUE = re.compile(r"\s*#?([1-9][0-9]*)\s*")


def parse_parent_declaration(body: object) -> ParentDeclaration:
    """Examine every declaration line and return its unambiguous meaning."""
    if not isinstance(body, str):
        return ParentDeclaration(ParentDeclarationStatus.ABSENT)

    declared: set[int] = set()
    found_candidate = False
    for line in body.splitlines():
        candidate = _CANDIDATE.fullmatch(line.strip())
        if candidate is None:
            continue
        found_candidate = True
        value = _SUPPORTED_VALUE.fullmatch(candidate.group(1))
        if value is None:
            return ParentDeclaration(ParentDeclarationStatus.INVALID, reason="malformed Parent-Issue declaration")
        declared.add(int(value.group(1)))

    if not found_candidate:
        return ParentDeclaration(ParentDeclarationStatus.ABSENT)
    if len(declared) != 1:
        return ParentDeclaration(ParentDeclarationStatus.INVALID, reason="multiple distinct Parent-Issue declarations")
    return ParentDeclaration(ParentDeclarationStatus.SUPPORTED, parent_number=next(iter(declared)))
