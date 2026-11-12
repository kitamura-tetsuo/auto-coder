"""Canonicalize only Docker's unordered effective Mounts collection."""

from __future__ import annotations

import json


def canonical_mounts(value: object) -> list[dict[str, object]]:
    """Keep every complete mount record, including duplicates and extra fields."""
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("Docker mount inspection is malformed")
    return sorted(value, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))


def canonical_inspection(value: dict[str, object]) -> dict[str, object]:
    """Preserve all other fields and array ordering in the full inspection."""
    return {**value, "Mounts": canonical_mounts(value.get("Mounts"))}


def canonical_legacy_inspection(value: list[object]) -> list[object]:
    """Normalize the mount field in the existing 19-field host readback."""
    if len(value) != 19:
        raise ValueError("legacy Docker inspection fields are incomplete")
    return [*value[:11], canonical_mounts(value[11]), *value[12:]]
