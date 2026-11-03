"""Lossless, self-contained interning for individual review transport only."""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
from typing import Callable

VERSION = 1
_JSON_STYLES = ("compact", "compact-sorted", "pretty", "pretty-sorted")


def _json(value: object, style: str = "compact") -> str:
    if style not in _JSON_STYLES:
        raise ValueError("Unsupported evidence JSON serialization")
    pretty = style.startswith("pretty")
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2 if pretty else None, separators=None if pretty else (",", ":"), sort_keys=style.endswith("-sorted"))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate evidence object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite evidence scalar")


def _load(value: str) -> object:
    return json.loads(value, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def _structure(value: object, string: Callable[[str], object]) -> list[object]:
    """Keep mapping order, tuple/list and bool/int distinctions explicit."""
    if isinstance(value, str):
        return ["r", string(value)]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Evidence mapping keys must be strings")
        return ["m", [[string(key), _structure(item, string)] for key, item in value.items()]]
    if isinstance(value, (tuple, list)):
        return ["t" if isinstance(value, tuple) else "l", [_structure(item, string) for item in value]]
    if value is None or type(value) in (bool, int) or (type(value) is float and math.isfinite(value)):
        return ["v", value]
    raise ValueError("Unsupported required evidence value")


def _identity(value: object) -> str:
    return hashlib.sha256(_json(_structure(value, lambda text: text)).encode("utf-8")).hexdigest()


def encode_individual_evidence(inputs: dict[str, object], requirement_texts: tuple[str, ...]) -> str:
    """Intern exact strings and Requirement occurrences without changing roles.

    Canonical JSON strings may be represented by their exact reversible
    serialization style. No Markdown is parsed to manufacture a manifest.
    """
    identity = _identity(inputs)
    prefix = f"ie{VERSION}:{identity}:"
    definitions: list[object] = []
    references: dict[str, str] = {}
    normative_atoms = frozenset(text for text in requirement_texts if text)
    captured: set[str] = set()
    json_forms: dict[str, tuple[str, object]] = {}

    def collect(value: object) -> None:
        if isinstance(value, str):
            if value in captured:
                return
            captured.add(value)
            if value.startswith(("{", "[")):
                try:
                    parsed = _load(value)
                    if isinstance(parsed, (dict, list)):
                        for style in _JSON_STYLES:
                            if _json(parsed, style) == value:
                                json_forms[value] = (style, parsed)
                                collect(parsed)
                                break
                except (ValueError, TypeError, RecursionError):
                    pass
        elif isinstance(value, dict):
            for key, item in value.items():
                collect(key)
                collect(item)
        elif isinstance(value, (tuple, list)):
            for item in value:
                collect(item)

    collect(inputs)
    # Generic captured strings are byte candidates, not parsed authority.
    # This also interns a related member's manifest text inside its body while
    # preserving both occurrences' original non-normative locations.
    atoms = normative_atoms | {text for text in captured if len(text) >= 32}

    def intern(text: str) -> str:
        if text in references:
            return references[text]
        index = len(definitions)
        reference = prefix + str(index)
        references[text] = reference
        definitions.append(None)
        definition: object = ["s", text]
        if text not in normative_atoms:
            form = json_forms.get(text)
            if form is not None:
                definition = ["j", form[0], _structure(form[1], intern)]
            else:
                matches: list[tuple[int, int, str]] = []
                for atom in atoms:
                    if len(atom) >= len(text):
                        continue
                    start = text.find(atom)
                    if start >= 0:
                        heapq.heappush(matches, (start, -len(atom), atom))
                parts = []
                offset = 0
                while matches:
                    start, size, atom = heapq.heappop(matches)
                    if start >= offset:
                        if start > offset:
                            parts.append(intern(text[offset:start]))
                        parts.append(intern(atom))
                        offset = start + len(atom)
                    following = text.find(atom, offset)
                    if following >= 0:
                        heapq.heappush(matches, (following, size, atom))
                if parts:
                    if offset < len(text):
                        parts.append(intern(text[offset:]))
                    definition = ["c", parts]
        definitions[index] = definition
        return reference

    root = _structure(inputs, intern)
    return _json({"version": VERSION, "identity": identity, "definitions": definitions, "root": root})


def decode_individual_evidence(payload: str) -> dict[str, object]:
    """Validate the entire envelope before returning any reconstructed input."""
    envelope = _load(payload)
    if not isinstance(envelope, dict) or set(envelope) != {"version", "identity", "definitions", "root"}:
        raise ValueError("Invalid evidence envelope")
    if type(envelope["version"]) is not int or envelope["version"] != VERSION:
        raise ValueError("Unsupported evidence version")
    identity = envelope["identity"]
    definitions = envelope["definitions"]
    if not isinstance(identity, str) or re.fullmatch(r"[0-9a-f]{64}", identity) is None or not isinstance(definitions, list):
        raise ValueError("Invalid evidence identity or definitions")
    prefix = f"ie{VERSION}:{identity}:"
    resolved: dict[int, str] = {}
    active: set[int] = set()
    content_owners: dict[str, int] = {}

    def reference(value: object) -> str:
        if not isinstance(value, str) or not value.startswith(prefix):
            raise ValueError("Cross-envelope or malformed evidence reference")
        suffix = value[len(prefix) :]
        if not suffix.isascii() or not suffix.isdecimal() or str(int(suffix)) != suffix:
            raise ValueError("Malformed evidence reference index")
        index = int(suffix)
        if index >= len(definitions) or index in active:
            raise ValueError("Dangling or cyclic evidence reference")
        if index in resolved:
            return resolved[index]
        active.add(index)
        definition = definitions[index]
        if not isinstance(definition, list) or not definition:
            raise ValueError("Invalid evidence definition")
        tag = definition[0]
        if tag == "s" and len(definition) == 2 and isinstance(definition[1], str):
            text = definition[1]
        elif tag == "c" and len(definition) == 2 and isinstance(definition[1], list):
            text = "".join(reference(part) for part in definition[1])
        elif tag == "j" and len(definition) == 3 and isinstance(definition[1], str) and definition[1] in _JSON_STYLES:
            text = _json(node(definition[2]), definition[1])
        else:
            raise ValueError("Unsupported evidence definition")
        if text in content_owners:
            raise ValueError("Conflicting duplicate evidence definitions")
        content_owners[text] = index
        resolved[index] = text
        active.remove(index)
        return text

    def node(value: object) -> object:
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError("Malformed evidence node")
        tag, content = value
        if tag == "r":
            return reference(content)
        if tag == "v" and (content is None or type(content) in (bool, int) or (type(content) is float and math.isfinite(content))):
            return content
        if tag in ("l", "t") and isinstance(content, list):
            sequence = [node(item) for item in content]
            return tuple(sequence) if tag == "t" else sequence
        if tag == "m" and isinstance(content, list):
            pairs = []
            for pair in content:
                if not isinstance(pair, list) or len(pair) != 2:
                    raise ValueError("Malformed evidence mapping occurrence")
                pairs.append((reference(pair[0]), node(pair[1])))
            return _unique_object(pairs)
        raise ValueError("Unsupported evidence node")

    result = node(envelope["root"])
    if not isinstance(result, dict) or len(resolved) != len(definitions) or _identity(result) != identity:
        raise ValueError("Incomplete or corrupted required evidence")
    return result
