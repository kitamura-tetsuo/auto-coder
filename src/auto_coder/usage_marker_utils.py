import json
import re
from typing import Any, Iterable, List, Tuple

# Matches "429" only when it appears in a context that unambiguously identifies
# it as the HTTP 429 (Too Many Requests) status code, e.g. "HTTP 429",
# "Error 429:", "status: 429", "429 Too Many Requests". A bare "429" (e.g. a
# line number, a port, a test count) must not match, since it is a common
# regression source for false-positive usage-limit detection.
_HTTP_429_PATTERN = re.compile(
    r"http[\s/][\w.]{0,10}\s*429\b" r"|\b(?:error|status(?:\s*code)?|code)\b[^0-9]{0,10}429\b" r"|\b429\b[^a-z0-9]{0,10}too\s+many\s+requests",
    re.IGNORECASE,
)


def has_http_429_marker(output: str) -> bool:
    """Check whether output contains an unambiguous HTTP 429 status reference.

    This intentionally requires context (an "http"/"error"/"status"/"code"
    label preceding 429, or "too many requests" following it) rather than a
    bare "429" substring, to avoid misdetecting unrelated numbers such as
    line numbers, ports, or counts as a usage-limit signal.
    """
    if not output:
        return False
    return bool(_HTTP_429_PATTERN.search(output))


def _json_values_match(container: Any, needle: Any) -> bool:
    """Compare two JSON values allowing partial string matches."""
    if isinstance(needle, str) and isinstance(container, str):
        return needle.lower() in container.lower()
    return container == needle


def _json_subset(container: Any, needle: Any) -> bool:
    """Check whether needle JSON structure is contained within container."""
    if isinstance(needle, dict):
        if not isinstance(container, dict):
            return False
        return all(key in container and _json_subset(container[key], value) for key, value in needle.items())

    if isinstance(needle, list):
        if not isinstance(container, list):
            return False
        # Every element in the needle list must match at least one element in the container list
        return all(any(_json_subset(item, needle_item) for item in container) for needle_item in needle)

    return _json_values_match(container, needle)


def _json_tree_contains(container: Any, needle: Any) -> bool:
    """Recursively search the entire JSON tree for a subset match."""
    if _json_subset(container, needle):
        return True

    if isinstance(container, dict):
        return any(_json_tree_contains(value, needle) for value in container.values())

    if isinstance(container, list):
        return any(_json_tree_contains(item, needle) for item in container)

    return False


def _try_parse_json_fragment(text: str) -> Tuple[bool, Any]:
    """Attempt to parse a JSON object/array from the given text."""
    try:
        return True, json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try parsing from the first JSON-looking character to handle prefixed logs
    for start_char in ("{", "["):
        idx = text.find(start_char)
        if idx != -1:
            fragment = text[idx:]
            try:
                return True, json.loads(fragment)
            except json.JSONDecodeError:
                continue

    return False, None


def _extract_json_structures(output: str) -> List[Any]:
    """Extract JSON structures from the full CLI output."""
    structures: List[Any] = []
    if not output:
        return structures

    # Try parsing the full output first
    parsed, obj = _try_parse_json_fragment(output.strip())
    if parsed:
        structures.append(obj)

    # Then try line by line to capture JSON logs embedded in output
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        parsed, obj = _try_parse_json_fragment(line)
        if parsed:
            structures.append(obj)

    return structures


def _normalize_marker(marker: Any) -> Tuple[Any, bool]:
    """Normalize marker into (value, is_json_marker) form."""
    if isinstance(marker, (dict, list)):
        return marker, True

    if isinstance(marker, str):
        stripped = marker.strip()
        if not stripped:
            return "", False
        try:
            return json.loads(stripped), True
        except json.JSONDecodeError:
            return stripped.lower(), False

    return marker, False


def has_usage_marker_match(output: str, usage_markers: Iterable[Any]) -> bool:
    """Check whether the output contains any configured usage marker.

    Supports JSON-based partial matching: if a marker is a JSON object/array (or a JSON
    string that can be parsed), it is treated as a subset that can match anywhere within
    any JSON structure found in the output. String markers fall back to a
    case-insensitive substring check to preserve existing behavior.
    """
    markers = list(usage_markers or [])
    if not markers or not output:
        return False

    json_structures = _extract_json_structures(output)
    lowered_output = output.lower()

    for marker in markers:
        normalized_marker, is_json_marker = _normalize_marker(marker)

        if is_json_marker:
            if json_structures and any(_json_tree_contains(obj, normalized_marker) for obj in json_structures):
                return True
            # If no JSON could be parsed, fall back to string comparison for safety
            marker_str = json.dumps(normalized_marker) if not isinstance(normalized_marker, str) else normalized_marker
            if marker_str.lower() in lowered_output:
                return True
        else:
            if isinstance(normalized_marker, str):
                if normalized_marker and normalized_marker in lowered_output:
                    return True
            else:
                if str(normalized_marker).lower() in lowered_output:
                    return True

    return False
