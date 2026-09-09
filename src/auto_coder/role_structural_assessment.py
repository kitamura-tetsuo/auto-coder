"""Deterministic role-aware structural assessment for a supplied Issue snapshot.

This boundary classifies exactly the text/role structure described by the
tracking-parent and implementation-child roles. It performs no LLM call, no
GitHub request or mutation, and no durable-state write; the supplied role is
never inferred from the Issue's content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple

from .requirement_contract import NormativeIssueManifest

ROLE_STRUCTURAL_ASSESSMENT_VERSION = "v1"

ROLE_TRACKING_PARENT = "tracking_parent"
ROLE_IMPLEMENTATION_CHILD = "implementation_child"
_KNOWN_ROLES = frozenset({ROLE_TRACKING_PARENT, ROLE_IMPLEMENTATION_CHILD})

REASON_PARENT_REQUIREMENTS_FORBIDDEN = "parent_requirements_forbidden"
REASON_PARENT_OBJECTIVE_REQUIRED = "parent_objective_required"
REASON_CHILD_REQUIREMENTS_INVALID = "child_requirements_invalid"

_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_BLOCKQUOTE = re.compile(r"^ {0,3}>")
_INDENTED_CODE = re.compile(r"^ {4,}\S")
_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_DECLARATION_PREFIX = re.compile(r"^(?:(?:[-*+][ \t]+(?:\[[ xX]\][ \t]+)?)|(?:\d+[.)][ \t]+))?")
_DECLARATION = re.compile(r"^(?:`(REQ-\d{3})(?::)?`(?::)?|(REQ-\d{3}):)[ \t]*(.*)$")


@dataclass(frozen=True)
class StructuralLocation:
    """One authored source line implicated in a structural defect."""

    line_number: int
    text: str


@dataclass(frozen=True)
class StructuralDefect:
    """One independently detected, deterministic structural defect."""

    reason: str
    detail: str
    locations: Tuple[StructuralLocation, ...] = ()


@dataclass(frozen=True)
class RoleStructuralAssessment:
    """Side-effect-free verdict for one supplied Issue snapshot and role."""

    status: str
    issue_number: Optional[int] = None
    role: Optional[str] = None
    defects: Tuple[StructuralDefect, ...] = ()
    error: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        return self.status == "VALID"


def _error(message: str) -> RoleStructuralAssessment:
    return RoleStructuralAssessment(status="ERROR", error=message)


def _authored_lines(body: str) -> Iterator[Tuple[int, str]]:
    """Yield (1-based line number, raw line) pairs outside excluded regions.

    Fenced code blocks, indented code blocks, blockquote lines, and HTML
    comments are non-authored examples: they are skipped entirely rather than
    being inspected for headings or declarations, and text after they end is
    inspected normally again.
    """
    lines = body.replace("\r\n", "\n").split("\n")
    fence_char: Optional[str] = None
    fence_len = 0
    in_comment = False
    for index, line in enumerate(lines, start=1):
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        comment_start = line.find("<!--")
        if comment_start != -1:
            if "-->" not in line[comment_start:]:
                in_comment = True
            continue
        fence_match = _FENCE_OPEN.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence_char is None:
                fence_char, fence_len = marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                fence_char = None
            continue
        if fence_char is not None:
            continue
        if _BLOCKQUOTE.match(line):
            continue
        if _INDENTED_CODE.match(line):
            continue
        yield index, line


def _find_requirements_defect(body: str) -> Optional[StructuralDefect]:
    locations: List[StructuralLocation] = []
    for index, line in _authored_lines(body):
        heading_match = _HEADING.match(line)
        if heading_match:
            if heading_match.group(2).strip().casefold() == "requirements":
                locations.append(StructuralLocation(index, line.strip()))
            continue
        stripped = line.strip()
        if not stripped:
            continue
        content = _DECLARATION_PREFIX.sub("", stripped, count=1)
        if _DECLARATION.match(content):
            locations.append(StructuralLocation(index, stripped))
    if not locations:
        return None
    return StructuralDefect(
        REASON_PARENT_REQUIREMENTS_FORBIDDEN,
        "A tracking parent must not contain a Requirements heading or a REQ-NNN declaration anywhere in its body.",
        tuple(locations),
    )


def _find_objective_defect(body: str) -> Optional[StructuralDefect]:
    sections: List[Tuple[int, str, str]] = []
    current: Optional[List[str]] = None
    current_level: Optional[int] = None
    current_start: Optional[int] = None
    current_heading_text = ""

    def close_section() -> None:
        nonlocal current, current_level, current_start
        assert current is not None and current_start is not None
        sections.append((current_start, current_heading_text, "\n".join(current).strip()))
        current, current_level, current_start = None, None, None

    for index, line in _authored_lines(body):
        heading_match = _HEADING.match(line)
        if heading_match:
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            is_target = title.casefold() == "objective"
            if current is not None and (is_target or level <= current_level):
                close_section()
            if is_target:
                current, current_level, current_start = [], level, index
                current_heading_text = line.strip()
            continue
        if current is not None:
            current.append(line)
    if current is not None:
        close_section()

    if not sections:
        return StructuralDefect(REASON_PARENT_OBJECTIVE_REQUIRED, "A tracking parent must have exactly one nonempty authored Objective section, but none was found.")
    if len(sections) > 1:
        locations = tuple(StructuralLocation(start, heading_text) for start, heading_text, _text in sections)
        return StructuralDefect(REASON_PARENT_OBJECTIVE_REQUIRED, "A tracking parent must have exactly one Objective section, but multiple were found.", locations)
    start, heading_text, text = sections[0]
    if not text:
        return StructuralDefect(REASON_PARENT_OBJECTIVE_REQUIRED, "A tracking parent's Objective section must not be empty.", (StructuralLocation(start, heading_text),))
    return None


def assess_role_structure(manifest: NormativeIssueManifest, body: str, role: Optional[str]) -> RoleStructuralAssessment:
    """Assess the supplied Issue snapshot against its explicitly supplied role.

    ``manifest`` and ``body`` must describe the same Issue snapshot; this
    function does not recompute or reconcile that pairing. ``role`` must be
    one of :data:`ROLE_TRACKING_PARENT` or :data:`ROLE_IMPLEMENTATION_CHILD`;
    any other value, including ``None``, is an assessment error rather than a
    guessed role.
    """
    if not isinstance(manifest, NormativeIssueManifest):
        return _error("Role structural assessment requires a normative Issue manifest")
    if not isinstance(body, str):
        return _error(f"Issue #{manifest.issue_number} role structural assessment requires the assessed Issue body text")
    if role not in _KNOWN_ROLES:
        return _error(f"Issue #{manifest.issue_number} role is unavailable or contradictory for structural assessment")

    if role == ROLE_TRACKING_PARENT:
        defects = [defect for defect in (_find_requirements_defect(body), _find_objective_defect(body)) if defect is not None]
        return RoleStructuralAssessment("BLOCKED" if defects else "VALID", manifest.issue_number, role, tuple(defects))

    if not manifest.explicit_contract_present or not manifest.explicit_contract_valid:
        detail = manifest.error or f"Issue #{manifest.issue_number} requires an explicit, nonempty Requirements contract with unique REQ-NNN identifiers."
        return RoleStructuralAssessment("BLOCKED", manifest.issue_number, role, (StructuralDefect(REASON_CHILD_REQUIREMENTS_INVALID, detail),))
    return RoleStructuralAssessment("VALID", manifest.issue_number, role)
