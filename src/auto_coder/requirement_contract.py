"""Intake adapter for the shared Issue specification parser."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .issue_specification import (
    ISSUE_SPECIFICATION_PARSER_VERSION,
    format_requirement_contract_error,
    parse_issue_specification,
)

REQUIREMENT_CONTRACT_PARSER_VERSION = ISSUE_SPECIFICATION_PARSER_VERSION


@dataclass
class RequirementContractResult:
    """Structural result for one Issue's explicit Requirements section."""

    explicit: bool = False
    entries: List[tuple[str, str]] = field(default_factory=list)
    error: Optional[str] = None


def parse_requirement_contract(issue_number: int, body: str) -> RequirementContractResult:
    """Parse an exact Markdown Requirements section without interpreting prose.

    Issues without such a section deliberately return ``explicit=False`` so callers
    can retain legacy extraction. This function is the shared structural oracle for
    both intake and adversarial validation.
    """
    manifest = parse_issue_specification("", body)
    if not manifest.has_requirements_section:
        return RequirementContractResult()
    if manifest.requirement_diagnostics:
        error = format_requirement_contract_error(issue_number, manifest.requirement_diagnostics[0])
        return RequirementContractResult(explicit=True, error=error)
    entries = [(item.requirement_id, item.text) for item in manifest.requirements]
    return RequirementContractResult(explicit=True, entries=entries)
