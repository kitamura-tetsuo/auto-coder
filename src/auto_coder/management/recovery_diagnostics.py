"""Bounded non-authorizing code locations for recovery refusal diagnostics."""

from __future__ import annotations

import re
from pathlib import Path

_SYMBOL = r"[A-Za-z_][A-Za-z0-9_]{0,95}"
_FILENAME = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\.py"
_FUNCTION = _SYMBOL + r"|<module>"


def refusal_causes(error: BaseException) -> list[dict[str, str | int]]:
    """Describe bounded code locations, never exception data or frame locals."""
    causes: list[dict[str, str | int]] = []
    visited: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in visited and len(causes) < 8:
        visited.add(id(current))
        name = type(current).__name__
        row: dict[str, str | int] = {"type": name if re.fullmatch(_SYMBOL, name) else "Exception"}
        if isinstance(current, OSError) and type(current.errno) is int:
            row["errno"] = current.errno
        trace = current.__traceback__
        if trace is not None:
            while trace.tb_next is not None:
                trace = trace.tb_next
            code = trace.tb_frame.f_code
            filename = Path(code.co_filename).name
            if re.fullmatch(_FILENAME, filename) and re.fullmatch(_FUNCTION, code.co_name):
                row.update(file=filename, function=code.co_name, line=trace.tb_lineno)
        causes.append(row)
        current = current.__cause__
    return causes


def safe_refusal_causes(value: object) -> list[dict[str, str | int]]:
    """Copy only the same finite metadata from a subprocess's JSON result."""
    if not isinstance(value, list) or len(value) > 8:
        return []
    causes: list[dict[str, str | int]] = []
    for row in value:
        if not isinstance(row, dict) or not set(row) <= {"type", "errno", "file", "function", "line"}:
            return []
        if not isinstance(row.get("type"), str) or re.fullmatch(_SYMBOL, row["type"]) is None:
            return []
        if "errno" in row and type(row["errno"]) is not int:
            return []
        location = {"file", "function", "line"} & row.keys()
        if location and (location != {"file", "function", "line"} or not isinstance(row["file"], str) or re.fullmatch(_FILENAME, row["file"]) is None or not isinstance(row["function"], str) or re.fullmatch(_FUNCTION, row["function"]) is None or type(row["line"]) is not int or row["line"] <= 0):
            return []
        causes.append(dict(row))
    return causes
