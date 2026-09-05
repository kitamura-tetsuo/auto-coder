"""Provider-independent parsing of an Issue's normative specification."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

ISSUE_SPECIFICATION_PARSER_VERSION = "v1"

_MARKDOWN_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_ENTRY_PREFIX = re.compile(r"^(?:(?:[-*+]\s+(?:\[[ xX]\]\s+)?)|(?:\d+[.)]\s+))?")
_REQUIREMENT = re.compile(r"^(?:`(REQ-\d{3})(?::)?`(?::)?|(REQ-\d{3}):)\s*(.*)$")
_SCENARIO = re.compile(r"^(AC-\d{3})(?:\s*(?:—|-)\s*(.*))?$")
_COVERS = re.compile(r"^Covers:\s*(.*)$", re.IGNORECASE)
_REQ_REFERENCE = re.compile(r"\bREQ-\d{3}\b")


@dataclass(frozen=True)
class NormativeRequirement:
    """One exact, stable requirement declared in ``Requirements``."""

    requirement_id: str = ""
    text: str = ""


@dataclass(frozen=True)
class AcceptanceScenario:
    """Non-normative verification metadata from an Acceptance Scenarios section."""

    scenario_id: str = ""
    title: str = ""
    requirement_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SpecificationDiagnostic:
    """A deterministic structural problem in an Issue specification."""

    code: str = ""
    message: str = ""
    line: int = 0


@dataclass
class IssueSpecificationManifest:
    """Normative requirements plus separate, non-normative verification metadata."""

    title: str = ""
    has_requirements_section: bool = False
    requirements: List[NormativeRequirement] = field(default_factory=list)
    acceptance_scenarios: List[AcceptanceScenario] = field(default_factory=list)
    diagnostics: List[SpecificationDiagnostic] = field(default_factory=list)

    @property
    def is_structurally_valid(self) -> bool:
        return self.has_requirements_section and not self.diagnostics

    @property
    def requirement_diagnostics(self) -> List[SpecificationDiagnostic]:
        """Return diagnostics that make the normative manifest untrustworthy."""
        return [item for item in self.diagnostics if item.code != "unknown-requirement-reference"]


def format_requirement_contract_error(issue_number: int, diagnostic: SpecificationDiagnostic) -> str:
    """Render the stable compatibility diagnostic used at production gates."""
    labels = {
        "duplicate-requirement-id": "duplicate IDs",
        "malformed-requirement": "malformed entries",
        "empty-requirement-text": "empty requirement text",
        "empty-requirements-section": "no valid REQ-NNN entries",
    }
    detail = labels.get(diagnostic.code, diagnostic.message)
    return f"Issue #{issue_number} Requirements section contains {detail}"


def _sections(body: str) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    requirements: list[tuple[int, str]] = []
    scenarios: list[tuple[int, str]] = []
    destination: list[tuple[int, str]] | None = None
    section_level = 0
    for line_number, line in enumerate(body.splitlines(), 1):
        heading = _MARKDOWN_HEADING.match(line)
        if heading:
            name = heading.group(2)
            level = len(heading.group(1))
            if name == "Requirements":
                destination, section_level = requirements, level
            elif name == "Acceptance Scenarios":
                destination, section_level = scenarios, level
            elif destination is scenarios and level > section_level:
                scenarios.append((line_number, line))
            else:
                destination = None
            continue
        if destination is not None:
            destination.append((line_number, line))
    return requirements, scenarios


def parse_issue_specification(title: str, body: str) -> IssueSpecificationManifest:
    """Build a deterministic manifest without consulting any external state."""
    requirement_lines, scenario_lines = _sections(body)
    has_requirements = bool(re.search(r"^\s{0,3}#{1,6}\s+Requirements\s*#*\s*$", body, re.MULTILINE))
    manifest = IssueSpecificationManifest(title=title, has_requirements_section=has_requirements)
    if not has_requirements:
        manifest.diagnostics.append(SpecificationDiagnostic("missing-requirements-section", "No exact Requirements section was found"))
    else:
        seen: set[str] = set()
        for line_number, raw_line in requirement_lines:
            line = raw_line.strip()
            if not line or line.startswith("<!--") or line.endswith("-->"):
                continue
            content = _ENTRY_PREFIX.sub("", line, count=1)
            match = _REQUIREMENT.fullmatch(content)
            if not match:
                manifest.diagnostics.append(SpecificationDiagnostic("malformed-requirement", f"Malformed requirement entry: {line}", line_number))
                continue
            requirement_id = match.group(1) or match.group(2)
            text = match.group(3).strip()
            if not text:
                manifest.diagnostics.append(SpecificationDiagnostic("empty-requirement-text", f"Requirement {requirement_id} has empty text", line_number))
                continue
            if requirement_id in seen:
                manifest.diagnostics.append(SpecificationDiagnostic("duplicate-requirement-id", f"Duplicate requirement ID: {requirement_id}", line_number))
                continue
            seen.add(requirement_id)
            manifest.requirements.append(NormativeRequirement(requirement_id, text))
        if not manifest.requirements and not manifest.diagnostics:
            manifest.diagnostics.append(SpecificationDiagnostic("empty-requirements-section", "Requirements section contains no valid REQ-NNN entries"))

    current_id = ""
    current_title = ""
    current_refs: list[str] = []

    def append_scenario() -> None:
        if current_id:
            manifest.acceptance_scenarios.append(AcceptanceScenario(current_id, current_title, tuple(current_refs)))

    for line_number, raw_line in scenario_lines:
        heading = _MARKDOWN_HEADING.match(raw_line)
        if heading:
            match = _SCENARIO.fullmatch(heading.group(2))
            if match:
                append_scenario()
                current_id, current_title = match.group(1), (match.group(2) or "").strip()
                current_refs = []
            continue
        covers = _COVERS.match(raw_line.strip())
        if current_id and covers:
            current_refs = _REQ_REFERENCE.findall(covers.group(1))
            known = {item.requirement_id for item in manifest.requirements}
            for reference in current_refs:
                if reference not in known:
                    manifest.diagnostics.append(
                        SpecificationDiagnostic(
                            "unknown-requirement-reference",
                            f"Acceptance Scenario {current_id} references nonexistent requirement {reference}",
                            line_number,
                        )
                    )
    append_scenario()
    return manifest
