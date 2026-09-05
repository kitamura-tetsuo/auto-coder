"""Provider-independent parsing of an Issue's normative specification."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

ISSUE_SPECIFICATION_PARSER_VERSION = "v1"

_MARKDOWN_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_POTENTIAL_LIST_PREFIX = re.compile(r"^( {0,3})([-*+]|\d+[.)])([ \t]+)(?:\[[ xX]\]([ \t]+))?")
_REQUIREMENT = re.compile(r"^(?:`(REQ-\d{3})(?::)?`(?::)?|(REQ-\d{3}):)\s*(.*)$")
_SCENARIO = re.compile(r"^(AC-\d{3})(?:\s*(?:—|-)\s*(.*))?$")
_COVERS = re.compile(r"^Covers:\s*(.*)$", re.IGNORECASE)
_REQ_REFERENCE = re.compile(r"\bREQ-\d{3}\b")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_LIST_ITEM = re.compile(r"^ {0,3}(?:[-*+] |\d+[.)] )")


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


def _indentation_columns(line: str) -> int:
    columns = 0
    for character in line:
        if character == " ":
            columns += 1
        elif character == "\t":
            columns += 4 - (columns % 4)
        else:
            break
    return columns


def _column_width(text: str) -> int:
    columns = 0
    for character in text:
        columns += 4 - (columns % 4) if character == "\t" else 1
    return columns


def _parse_entry_line(raw_line: str) -> tuple[str, int, bool]:
    """Return entry content, CommonMark content column, and prefix validity."""
    prefix = _POTENTIAL_LIST_PREFIX.match(raw_line)
    if not prefix:
        return raw_line.strip(), 0, False
    leading, marker, padding, checkbox_padding = prefix.groups()
    marker_end = f"{leading}{marker}"
    padding_width = _column_width(f"{marker_end}{padding}") - _column_width(marker_end)
    valid_marker = not marker[0].isdigit() or len(marker[:-1]) <= 9
    if not valid_marker or not 1 <= padding_width <= 4:
        return raw_line.strip(), 0, True
    content_column = _column_width(f"{marker_end}{padding}")
    prefix_end = prefix.end() if checkbox_padding is not None else len(marker_end + padding)
    return raw_line[prefix_end:].strip(), content_column, False


def visible_markdown_lines(body: str) -> list[tuple[int, str]]:
    """Remove Markdown regions that GitHub does not render as Issue prose."""
    visible: list[tuple[int, str]] = []
    in_comment = False
    fence_character = ""
    fence_length = 0
    paragraph_open = False
    list_item_open = False
    for line_number, raw_line in enumerate(body.splitlines(), 1):
        if in_comment:
            closing = raw_line.find("-->")
            if closing < 0:
                visible.append((line_number, ""))
                paragraph_open = False
                continue
            # HTML closing delimiters are recognized regardless of indentation.
            # Only the rendered suffix participates in Markdown block parsing.
            raw_line = raw_line[closing + 3 :]
            in_comment = False

        fence = _FENCE.match(raw_line)
        if not in_comment and fence:
            marker = fence.group(1)
            trailing = fence.group(2)
            if fence_character:
                # CommonMark closing fences contain only an equal-or-longer
                # marker and optional spaces. Fence-looking code remains code.
                if marker[0] == fence_character and len(marker) >= fence_length and not trailing.strip():
                    fence_character, fence_length = "", 0
                continue
            # Backtick info strings cannot themselves contain a backtick. Such
            # a line is ordinary visible prose rather than a fence opener.
            if marker[0] == "`" and "`" in trailing:
                pass
            else:
                fence_character, fence_length = marker[0], len(marker)
                paragraph_open = False
                continue
        if fence_character:
            continue

        # CommonMark expands tabs to four-column stops. Any non-blank line
        # reaching column four before its first visible character is an
        # indented code line, not a heading, fence, or requirement entry.
        indentation = _indentation_columns(raw_line)
        if raw_line.strip() and indentation >= 4 and not paragraph_open and not list_item_open:
            continue

        line = raw_line
        rendered_parts: list[str] = []
        while line:
            opening = line.find("<!--")
            if opening < 0:
                rendered_parts.append(line)
                break
            rendered_parts.append(line[:opening])
            line = line[opening + 4 :]
            in_comment = True
        rendered = "".join(rendered_parts)
        visible.append((line_number, rendered))
        stripped = rendered.strip()
        if not stripped or _MARKDOWN_HEADING.match(rendered):
            paragraph_open = False
            if stripped:
                list_item_open = False
        else:
            paragraph_open = True
            if indentation < 4:
                list_item_open = bool(_LIST_ITEM.match(rendered))
    return visible


def _sections(body: str) -> tuple[bool, list[tuple[int, str]], list[tuple[int, str]]]:
    requirements: list[tuple[int, str]] = []
    scenarios: list[tuple[int, str]] = []
    destination: list[tuple[int, str]] | None = None
    section_level = 0
    found_requirements = False
    for line_number, line in visible_markdown_lines(body):
        heading = _MARKDOWN_HEADING.match(line)
        if heading:
            name = heading.group(2)
            level = len(heading.group(1))
            if name == "Requirements" and not found_requirements:
                destination, section_level = requirements, level
                found_requirements = True
            elif name == "Acceptance Scenarios":
                destination, section_level = scenarios, level
            elif destination is scenarios and level > section_level:
                scenarios.append((line_number, line))
            else:
                destination = None
            continue
        if destination is not None:
            destination.append((line_number, line))
    return found_requirements, requirements, scenarios


def parse_issue_specification(title: str, body: str) -> IssueSpecificationManifest:
    """Build a deterministic manifest without consulting any external state."""
    has_requirements, requirement_lines, scenario_lines = _sections(body)
    manifest = IssueSpecificationManifest(title=title, has_requirements_section=has_requirements)
    if not has_requirements:
        manifest.diagnostics.append(SpecificationDiagnostic("missing-requirements-section", "No exact Requirements section was found"))
    else:
        seen: set[str] = set()
        list_content_indentation = 0
        for line_number, raw_line in requirement_lines:
            line = raw_line.strip()
            if not line or line.startswith("<!--") or line.endswith("-->"):
                continue
            indentation = _indentation_columns(raw_line)
            if indentation >= 4 or (list_content_indentation and indentation >= list_content_indentation):
                manifest.diagnostics.append(SpecificationDiagnostic("malformed-requirement", f"Malformed requirement continuation: {line}", line_number))
                continue
            content, list_content_indentation, invalid_list_prefix = _parse_entry_line(raw_line)
            if invalid_list_prefix:
                manifest.diagnostics.append(SpecificationDiagnostic("malformed-requirement", f"Malformed list prefix: {line}", line_number))
                continue
            match = _REQUIREMENT.fullmatch(content)
            if not match:
                manifest.diagnostics.append(SpecificationDiagnostic("malformed-requirement", f"Malformed requirement entry: {line}", line_number))
                continue
            requirement_id = match.group(1) or match.group(2)
            text = match.group(3).strip()
            duplicate = requirement_id in seen
            if duplicate:
                manifest.diagnostics.append(SpecificationDiagnostic("duplicate-requirement-id", f"Duplicate requirement ID: {requirement_id}", line_number))
            else:
                seen.add(requirement_id)
            if not text:
                manifest.diagnostics.append(SpecificationDiagnostic("empty-requirement-text", f"Requirement {requirement_id} has empty text", line_number))
                continue
            if duplicate:
                continue
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
