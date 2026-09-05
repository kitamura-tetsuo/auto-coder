"""Deterministic parsing for explicit Issue requirement contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

REQUIREMENT_CONTRACT_PARSER_VERSION = "v1"

_MARKDOWN_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_EXPLICIT_REQUIREMENT_PREFIX = re.compile(r"^(?:(?:[-*+]\s+(?:\[[ xX]\]\s+)?)|(?:\d+[.)]\s+))?")
_EXPLICIT_REQUIREMENT = re.compile(r"^(?:`(REQ-\d{3})(?::)?`(?::)?|(REQ-\d{3}):)\s*(\S.*)$")


@dataclass
class RequirementContractResult:
    """Structural result for one Issue's explicit Requirements section."""

    explicit: bool = False
    entries: List[tuple[str, str]] = field(default_factory=list)
    error: Optional[str] = None
    invalid_reason: Optional[str] = None


@dataclass(frozen=True)
class NormativeRequirement:
    """One trusted, ordered entry in an explicit Issue contract."""

    requirement_id: str
    text: str


@dataclass(frozen=True)
class NormativeIssueManifest:
    """Provider-independent normative view of one Issue snapshot.

    ``requirements`` is deliberately empty unless the explicit contract is
    structurally valid. The Issue body outside that contract is not interpreted.
    """

    issue_number: int
    title: str
    explicit_contract_present: bool
    explicit_contract_valid: bool
    requirements: tuple[NormativeRequirement, ...] = ()
    error: Optional[str] = None
    invalid_reason: Optional[str] = None


def build_normative_issue_manifest(issue_number: int, title: str, body: str) -> NormativeIssueManifest:
    """Adapt the shared line-oriented contract result into a reusable manifest."""
    result = parse_requirement_contract(issue_number, body)
    valid = result.explicit and result.error is None
    requirements = tuple(NormativeRequirement(requirement_id, text) for requirement_id, text in result.entries) if valid else ()
    return NormativeIssueManifest(
        issue_number=issue_number,
        title=title,
        explicit_contract_present=result.explicit,
        explicit_contract_valid=valid,
        requirements=requirements,
        error=result.error,
        invalid_reason=result.invalid_reason,
    )


def parse_requirement_contract(issue_number: int, body: str) -> RequirementContractResult:
    """Parse an exact Markdown Requirements section without interpreting prose.

    Issues without such a section deliberately return ``explicit=False`` so callers
    can retain legacy extraction. This function is the shared structural oracle for
    both intake and adversarial validation.
    """
    section_lines: List[str] = []
    in_section = False
    found = False
    for line in body.splitlines():
        heading = _MARKDOWN_HEADING.match(line)
        if heading:
            if in_section:
                break
            if heading.group(2) == "Requirements":
                found = True
                in_section = True
            continue
        if in_section:
            section_lines.append(line)

    if not found:
        return RequirementContractResult()

    entries: List[tuple[str, str]] = []
    malformed: List[str] = []
    seen: set[str] = set()
    duplicates: set[str] = set()
    for raw_line in section_lines:
        line = raw_line.strip()
        if not line or line.startswith("<!--") or line.endswith("-->"):
            continue
        content = _EXPLICIT_REQUIREMENT_PREFIX.sub("", line, count=1)
        match = _EXPLICIT_REQUIREMENT.fullmatch(content)
        if not match:
            malformed.append(line)
            continue
        backticked_id, plain_id, text = match.groups()
        requirement_id = backticked_id or plain_id
        if requirement_id in seen:
            duplicates.add(requirement_id)
        seen.add(requirement_id)
        entries.append((requirement_id, text.strip()))

    prefix = f"Issue #{issue_number} Requirements section"
    if duplicates:
        invalid_reason = "duplicate_ids"
        error = f"{prefix} contains duplicate IDs: {', '.join(sorted(duplicates))}"
    elif malformed:
        invalid_reason = "malformed_entry"
        error = f"{prefix} contains malformed entries: {malformed[0]}"
    elif not entries:
        invalid_reason = "no_entries"
        error = f"{prefix} contains no valid REQ-NNN entries"
    else:
        invalid_reason = None
        error = None
    return RequirementContractResult(explicit=True, entries=[] if error else entries, error=error, invalid_reason=invalid_reason)
