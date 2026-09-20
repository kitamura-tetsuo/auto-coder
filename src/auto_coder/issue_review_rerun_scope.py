"""Authoritative, read-only scope resolution for Issue-review reruns."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .issue_review_rerun import ReviewSubject
from .parent_issue_reconciliation import ParentDeclarationStatus, parse_parent_declaration
from .util.gh_cache import resolve_authoritative_item_type


@dataclass(frozen=True)
class RerunScope:
    subjects: tuple[ReviewSubject, ...]
    exclusions: tuple[str, ...] = ()
    snapshots: tuple[tuple[int, dict[str, object]], ...] = ()
    readiness_authorities: tuple[tuple[str, int], ...] = ()


class IssueReviewRerunScopeResolver:
    """Resolve selectors without mutating GitHub or accepting partial scope."""

    def __init__(self, github: object, repository: str) -> None:
        self.github = github
        self.repository = repository.strip().lower()
        self._snapshots: dict[int, dict[str, object]] = {}
        self._parents: dict[int, Optional[int]] = {}
        self._direct_children: dict[int, list[dict[str, object]]] = {}
        if len(self.repository.split("/")) != 2 or not all(self.repository.split("/")):
            raise ValueError("repository must be in OWNER/REPO form")

    def issue(self, number: int) -> RerunScope:
        snapshot = self._issue(number, require_open=True)
        parent = self._parent(snapshot)
        children = self._children(number)
        if parent is not None or children:
            raise ValueError(f"Issue #{number} belongs to a family; use --family")
        return self._scope((ReviewSubject(self.repository, "individual", number),), (), (snapshot,))

    def family(self, number: int) -> RerunScope:
        parent = self._issue(number, require_open=True)
        if self._parent(parent) is not None:
            raise ValueError(f"Issue #{number} is a child; use --family with its parent")
        children = self._children(number)
        if not children:
            raise ValueError(f"Issue #{number} is not an open parent with direct children")
        snapshots = [parent]
        subjects = [ReviewSubject(self.repository, "decomposition", number)]
        for child_summary in children:
            child_number = self._number(child_summary)
            child = self._issue(child_number, require_open=False)
            if self._parent(child) != number:
                raise ValueError(f"native membership for child #{child_number} is unstable or conflicting")
            snapshots.append(child)
            subjects.append(ReviewSubject(self.repository, "individual", child_number))
        return self._scope(subjects, (), snapshots)

    def all(self) -> RerunScope:
        entities = self.github.get_open_entities_strict(self.repository)  # type: ignore[attr-defined]
        open_numbers = sorted({self._entity_number(item) for item in entities.issues})
        snapshots = {number: self._issue(number, require_open=True) for number in open_numbers}
        subjects: set[ReviewSubject] = set()
        exclusions: list[str] = []
        resolved_families: set[int] = set()
        for number in open_numbers:
            snapshot = snapshots[number]
            parent_number = self._parent(snapshot)
            children = self._children(number)
            if parent_number is not None:
                parent = self._issue(parent_number, require_open=False)
                if str(parent.get("state", "")).lower() != "open":
                    exclusions.append(f"Issue #{number}: native parent #{parent_number} is closed")
                    continue
                if parent_number not in resolved_families:
                    family = self.family(parent_number)
                    subjects.update(family.subjects)
                    resolved_families.add(parent_number)
                continue
            if children:
                if number not in resolved_families:
                    family = self.family(number)
                    subjects.update(family.subjects)
                    resolved_families.add(number)
            else:
                subjects.add(ReviewSubject(self.repository, "individual", number))
        return self._scope(subjects, exclusions, snapshots.values())

    def _issue(self, number: int, *, require_open: bool) -> dict[str, object]:
        snapshot = self._snapshots.get(number)
        if snapshot is None:
            if resolve_authoritative_item_type(self.github, self.repository, number) != "issue":
                raise ValueError(f"#{number} is a pull request, not an Issue")
            snapshot = self.github.get_issue_dispatch_snapshot_strict(self.repository, number)  # type: ignore[attr-defined]
            if not isinstance(snapshot, dict) or self._number(snapshot) != number or "pull_request" in snapshot:
                raise ValueError(f"authoritative Issue #{number} snapshot is unavailable")
            self._snapshots[number] = snapshot
        state = snapshot.get("state")
        if not isinstance(state, str):
            raise ValueError(f"authoritative Issue #{number} state is malformed")
        if require_open and state.lower() != "open":
            raise ValueError(f"Issue #{number} is closed")
        return snapshot

    def _parent(self, snapshot: dict[str, object]) -> Optional[int]:
        number = self._number(snapshot)
        if number in self._parents:
            return self._parents[number]
        declaration = parse_parent_declaration(snapshot.get("body"))
        if declaration.status is ParentDeclarationStatus.INVALID:
            raise ValueError(f"Issue #{number}: {declaration.reason}")
        native = self.github.get_parent_issue_details_strict(self.repository, number)  # type: ignore[attr-defined]
        native_number = None if native is None else self._number(native)
        if declaration.status is ParentDeclarationStatus.SUPPORTED:
            if native_number is None:
                raise ValueError(f"Issue #{number}: declared parent is not natively materialized")
            if declaration.parent_number != native_number:
                raise ValueError(f"Issue #{number}: Parent-Issue declaration conflicts with native parent")
        self._parents[number] = native_number
        return native_number

    def _children(self, number: int) -> list[dict[str, object]]:
        if number in self._direct_children:
            return self._direct_children[number]
        children = self.github.get_direct_sub_issues_strict(self.repository, number)  # type: ignore[attr-defined]
        if not isinstance(children, list):
            raise ValueError(f"direct-child membership for Issue #{number} is unavailable")
        result: list[dict[str, object]] = []
        seen: set[int] = set()
        for child in children:
            if not isinstance(child, dict):
                raise ValueError(f"direct-child membership for Issue #{number} is malformed")
            child_number = self._number(child)
            if child_number == number or child_number in seen:
                raise ValueError(f"direct-child membership for Issue #{number} is malformed")
            seen.add(child_number)
            result.append(child)
        self._direct_children[number] = result
        return result

    @staticmethod
    def _number(value: dict[str, object]) -> int:
        number = value.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise ValueError("GitHub returned a malformed Issue number")
        return number

    @staticmethod
    def _entity_number(value: object) -> int:
        number = getattr(value, "number", None)
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise ValueError("GitHub open-Issue enumeration was malformed")
        return number

    def _scope(
        self,
        subjects: Iterable[ReviewSubject],
        exclusions: Iterable[str],
        snapshots: Iterable[dict[str, object]],
    ) -> RerunScope:
        normalized = tuple(sorted(set(subjects), key=lambda subject: subject.key))
        captured = tuple(sorted(((self._number(item), item) for item in snapshots), key=lambda pair: pair[0]))
        authorities = tuple((subject.key, self._parents.get(subject.issue_number) or subject.issue_number) for subject in normalized)
        return RerunScope(normalized, tuple(sorted(set(exclusions))), captured, authorities)
