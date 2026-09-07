import re
from dataclasses import dataclass
from enum import Enum
from typing import Dict, FrozenSet, Optional, Set, Union

from .parent_issue_reconciliation import ParentDeclarationStatus, parse_parent_declaration


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


class UnavailableType:
    def __repr__(self):
        return "UNAVAILABLE"


UNAVAILABLE = UnavailableType()


@dataclass(frozen=True)
class IssueEvidence:
    number: int
    repository: Union[str, UnavailableType]
    type: Union[IssueType, UnavailableType]
    state: IssueState
    authoritative_parent: Union[int, None, UnavailableType]
    body: Union[str, UnavailableType]
    observed_native_dependencies: Union[FrozenSet[int], UnavailableType]
    is_authoritatively_nonexistent: bool = False


class DependencySatisfaction(Enum):
    WAITING = "waiting"
    SATISFIED = "satisfied"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class GraphValidity(Enum):
    VALID = "valid"
    INVALID = "invalid"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class SiblingDependencyResult:
    declaration_status: BlockedByDeclarationStatus
    desired_dependencies: Optional[FrozenSet[int]]
    is_synchronized: Union[bool, UnavailableType]
    is_valid_graph: GraphValidity
    satisfaction: DependencySatisfaction
    reason: Optional[str] = None


@dataclass(frozen=True)
class EvaluatedDependencyGraph:
    results: Dict[int, SiblingDependencyResult]


_BLOCKED_BY_CANDIDATE = re.compile(r"^blocked-by:(.*)$", re.IGNORECASE)
_BLOCKED_BY_DEPENDENCY = re.compile(r"^#([1-9][0-9]*)$")


def parse_blocked_by_declaration(body: object, parent_status: ParentDeclarationStatus) -> BlockedByDeclaration:
    if not isinstance(body, str):
        return BlockedByDeclaration(BlockedByDeclarationStatus.ABSENT)

    declarations: list[frozenset[int]] = []
    found_candidate = False

    for line in body.splitlines():
        candidate = _BLOCKED_BY_CANDIDATE.fullmatch(line.strip())
        if candidate is None:
            continue
        found_candidate = True
        value_str = candidate.group(1).strip()

        if not value_str:
            declarations.append(frozenset())
            continue

        parts = value_str.split(",")
        deps = set()
        malformed = False
        for part in parts:
            part = part.strip()
            if not part:
                malformed = True
                break
            dep_match = _BLOCKED_BY_DEPENDENCY.fullmatch(part)
            if not dep_match:
                malformed = True
                break
            deps.add(int(dep_match.group(1)))

        if malformed:
            return BlockedByDeclaration(BlockedByDeclarationStatus.INVALID, reason="malformed Blocked-By declaration")

        declarations.append(frozenset(deps))

    if not found_candidate:
        return BlockedByDeclaration(BlockedByDeclarationStatus.ABSENT)

    first_decl = declarations[0]
    for decl in declarations[1:]:
        if decl != first_decl:
            return BlockedByDeclaration(BlockedByDeclarationStatus.INVALID, reason="conflicting Blocked-By declarations")

    if parent_status != ParentDeclarationStatus.SUPPORTED:
        return BlockedByDeclaration(BlockedByDeclarationStatus.INVALID, reason="Blocked-By declaration requires a supported Parent-Issue declaration")

    return BlockedByDeclaration(BlockedByDeclarationStatus.SUPPORTED, dependencies=first_decl)


def evaluate_family_graph(evidence_map: Dict[int, IssueEvidence], target_parent: int, target_repository: str) -> EvaluatedDependencyGraph:
    results: Dict[int, SiblingDependencyResult] = {}

    direct_children = {num: ev for num, ev in evidence_map.items() if ev.authoritative_parent == target_parent and ev.repository == target_repository and not ev.is_authoritatively_nonexistent}

    node_validity: Dict[int, GraphValidity] = {}
    node_reasons: Dict[int, Optional[str]] = {}
    node_desired_deps: Dict[int, Optional[FrozenSet[int]]] = {}
    node_declarations: Dict[int, BlockedByDeclaration] = {}

    for num, child_ev in direct_children.items():
        if child_ev.body is UNAVAILABLE:
            node_validity[num] = GraphValidity.UNRESOLVED
            node_reasons[num] = "child body is unavailable"
            node_declarations[num] = BlockedByDeclaration(BlockedByDeclarationStatus.ABSENT)
            node_desired_deps[num] = None
            continue

        parent_decl = parse_parent_declaration(child_ev.body)
        blocked_by_decl = parse_blocked_by_declaration(child_ev.body, parent_decl.status)
        node_declarations[num] = blocked_by_decl

        validity = GraphValidity.VALID
        reason = None
        desired_deps: Optional[FrozenSet[int]] = None

        if blocked_by_decl.status == BlockedByDeclarationStatus.INVALID:
            validity = GraphValidity.INVALID
            reason = blocked_by_decl.reason
        elif blocked_by_decl.status == BlockedByDeclarationStatus.SUPPORTED:
            desired_deps = blocked_by_decl.dependencies
        else:
            if child_ev.observed_native_dependencies is UNAVAILABLE:
                validity = GraphValidity.UNRESOLVED
                reason = "native dependencies unavailable"
            else:
                desired_deps = child_ev.observed_native_dependencies

        if validity == GraphValidity.VALID and desired_deps is not None:
            for dep in desired_deps:
                if dep == num:
                    validity = GraphValidity.INVALID
                    reason = "self-dependency is invalid"
                    break

                dep_ev = evidence_map.get(dep)
                if dep_ev is None:
                    validity = GraphValidity.UNRESOLVED
                    reason = f"target #{dep} evidence is unavailable"
                    break

                if dep_ev.is_authoritatively_nonexistent:
                    validity = GraphValidity.INVALID
                    reason = f"target #{dep} is authoritatively nonexistent"
                    break

                if dep_ev.repository is UNAVAILABLE:
                    validity = GraphValidity.UNRESOLVED
                    reason = f"target #{dep} repository is unavailable"
                    break
                elif dep_ev.repository != target_repository:
                    validity = GraphValidity.INVALID
                    reason = f"target #{dep} is in a different repository"
                    break

                if dep_ev.type is UNAVAILABLE:
                    validity = GraphValidity.UNRESOLVED
                    reason = f"target #{dep} type is unavailable"
                    break
                elif dep_ev.type != IssueType.ISSUE:
                    validity = GraphValidity.INVALID
                    reason = f"target #{dep} is not an issue"
                    break

                if dep_ev.authoritative_parent is UNAVAILABLE:
                    validity = GraphValidity.UNRESOLVED
                    reason = f"target #{dep} parent is unavailable"
                    break
                elif dep_ev.authoritative_parent != target_parent:
                    validity = GraphValidity.INVALID
                    reason = f"target #{dep} is not a sibling"
                    break

        node_validity[num] = validity
        node_reasons[num] = reason
        node_desired_deps[num] = desired_deps

    in_cycle: Set[int] = set()
    desired_graph: Dict[int, Set[int]] = {}

    for num, deps in node_desired_deps.items():
        desired_graph[num] = set(deps) if deps is not None else set()

    for start_node in desired_graph:
        visited = set()
        path = []

        def dfs(node):
            if node in path:
                cycle_start = path.index(node)
                cycle_nodes = path[cycle_start:]
                in_cycle.update(cycle_nodes)
                return True
            if node in visited:
                return False

            visited.add(node)
            path.append(node)

            for neighbor in desired_graph.get(node, []):
                if dfs(neighbor):
                    return True

            path.pop()
            return False

        dfs(start_node)

    for node in in_cycle:
        node_validity[node] = GraphValidity.INVALID
        node_reasons[node] = "directed cycle detected"

    for num, child_ev in direct_children.items():
        decl = node_declarations[num]
        validity = node_validity[num]
        reason = node_reasons[num]
        desired_deps = node_desired_deps[num]

        is_sync: Union[bool, UnavailableType] = UNAVAILABLE
        if child_ev.observed_native_dependencies is UNAVAILABLE:
            is_sync = UNAVAILABLE
        elif desired_deps is not None:
            is_sync = set(desired_deps) == set(child_ev.observed_native_dependencies or frozenset())
        elif validity == GraphValidity.INVALID:
            is_sync = UNAVAILABLE

        satisfaction = DependencySatisfaction.INVALID
        if validity == GraphValidity.INVALID:
            satisfaction = DependencySatisfaction.INVALID
        elif validity == GraphValidity.UNRESOLVED:
            satisfaction = DependencySatisfaction.UNAVAILABLE
        else:
            visited = set()
            queue = list(desired_deps or set())

            has_unavailable = False
            has_open = False

            while queue:
                curr = queue.pop(0)
                if curr in visited:
                    continue
                visited.add(curr)

                curr_ev = evidence_map.get(curr)
                if curr_ev is None or curr_ev.state == IssueState.UNAVAILABLE:
                    has_unavailable = True
                    continue

                if curr_ev.state == IssueState.OPEN:
                    has_open = True

                if node_validity.get(curr) == GraphValidity.INVALID:
                    has_unavailable = True
                else:
                    queue.extend(node_desired_deps.get(curr) or set())

            if has_open:
                satisfaction = DependencySatisfaction.WAITING
            elif has_unavailable:
                satisfaction = DependencySatisfaction.UNAVAILABLE
            else:
                satisfaction = DependencySatisfaction.SATISFIED

        results[num] = SiblingDependencyResult(declaration_status=decl.status, desired_dependencies=desired_deps, is_synchronized=is_sync, is_valid_graph=validity, satisfaction=satisfaction, reason=reason)

    return EvaluatedDependencyGraph(results=results)
