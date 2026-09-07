from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, FrozenSet, Dict, Set, List
from src.auto_coder.parent_issue_reconciliation import parse_parent_declaration, ParentDeclarationStatus, ParentDeclaration

class BlockedByDeclarationStatus(Enum):
    ABSENT = "absent"
    SUPPORTED = "supported"
    INVALID = "invalid"

@dataclass(frozen=True)
class BlockedByDeclaration:
    status: BlockedByDeclarationStatus
    dependencies: Optional[FrozenSet[int]] = None
    reason: Optional[str] = None

class IssueState(Enum):
    OPEN = "open"
    CLOSED = "closed"
    UNAVAILABLE = "unavailable"

class IssueType(Enum):
    ISSUE = "issue"
    PULL_REQUEST = "pull_request"
    UNAVAILABLE = "unavailable"

@dataclass(frozen=True)
class IssueEvidence:
    number: int
    repository: str
    type: IssueType
    state: IssueState
    authoritative_parent: Optional[int]
    body: str
    observed_native_dependencies: Optional[FrozenSet[int]]

def parse_blocked_by_declaration(body: object) -> BlockedByDeclaration:
    pass

@dataclass(frozen=True)
class EvaluatedDependencyGraph:
    pass

def evaluate_family_graph(evidence: Dict[int, IssueEvidence], target_parent: int) -> EvaluatedDependencyGraph:
    pass
