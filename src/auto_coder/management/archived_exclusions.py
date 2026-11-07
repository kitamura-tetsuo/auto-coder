"""Immutable ancestor bindings for owner and idempotency exclusion, not online history."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .fresh_initialization import acquire_prior_owner_fence


@dataclass(frozen=True)
class ArchivedExclusion:
    instance_id: str
    host_journal_root: str
    journal_root: str
    database_device: int
    database_inode: int
    host_fence: str
    fence: str
    fence_device: int
    fence_inode: int

    def validate(self) -> None:
        if not isinstance(self.instance_id, str) or not 0 < len(self.instance_id) <= 128 or "\x00" in self.instance_id:
            raise ValueError("invalid excluded instance")
        for value in (self.host_journal_root, self.journal_root, self.host_fence, self.fence):
            if not isinstance(value, str) or not Path(value).is_absolute() or ".." in Path(value).parts or "\x00" in value:
                raise ValueError("invalid exclusion path")
        for device in (self.database_device, self.fence_device):
            if type(device) is not int or device < 0:
                raise ValueError("invalid exclusion device")
        for inode in (self.database_inode, self.fence_inode):
            if type(inode) is not int or inode <= 0:
                raise ValueError("invalid exclusion inode")

    def identity(self) -> tuple[str, str, int, int, str, int, int]:
        return self.instance_id, self.host_journal_root, self.database_device, self.database_inode, self.host_fence, self.fence_device, self.fence_inode


def parse_exclusions(raw: object) -> tuple[ArchivedExclusion, ...]:
    if not isinstance(raw, list) or len(raw) > 32:
        raise ValueError("exclusions must be a complete bounded list")
    result = []
    for index, value in enumerate(raw):
        if not isinstance(value, dict) or set(value) != set(ArchivedExclusion.__dataclass_fields__):
            raise ValueError("exclusion fields differ")
        binding = ArchivedExclusion(**value)
        binding.validate()
        if binding.journal_root != f"/management-excluded/{index}/journal" or binding.fence != f"/management-excluded/{index}/owner.lock":
            raise ValueError("exclusion mount names differ")
        result.append(binding)
    for attribute in ("instance_id", "host_journal_root", "host_fence"):
        if len({getattr(item, attribute) for item in result}) != len(result):
            raise ValueError("duplicate excluded generation")
    if len({(item.database_device, item.database_inode) for item in result}) != len(result) or len({(item.fence_device, item.fence_inode) for item in result}) != len(result):
        raise ValueError("duplicate excluded storage identity")
    for index, item in enumerate(result):
        root = Path(item.host_journal_root)
        for other in result[index + 1 :]:
            other_root = Path(other.host_journal_root)
            if root in other_root.parents or other_root in root.parents or root in Path(other.host_fence).parents or other_root in Path(item.host_fence).parents:
                raise ValueError("overlapping excluded storage paths")
    return tuple(result)


def acquire_exclusion_fences(bindings: tuple[ArchivedExclusion, ...], *, host: bool = False) -> tuple[int, ...]:
    held = []
    try:
        for binding in bindings:
            held.append(acquire_prior_owner_fence(Path(binding.host_fence if host else binding.fence), expected_device=binding.fence_device, expected_inode=binding.fence_inode))
        return tuple(held)
    except BaseException:
        for descriptor in held:
            os.close(descriptor)
        raise


def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate exclusion provenance key")
        result[key] = value
    return result
