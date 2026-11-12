"""Bind known local command inputs without re-auditing trusted distro programs.

This is deliberately a bounded supported syntax, not a shell/interpreter sandbox.
Separately reviewed exclusions retain their meaning only while the referenced
local source/configuration identities remain unchanged. Unsupported dynamic local
source dispatch refuses instead of inheriting an unrelated classification.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .containment_user_routes import UserControllerUnavailable, _protected_source

_TRUSTED_EXECUTABLE_ROOTS = tuple(Path(path) for path in ("/bin", "/sbin", "/lib", "/lib64", "/usr/bin", "/usr/sbin", "/usr/lib", "/usr/lib64", "/usr/libexec", "/usr/share"))
_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9_:/])(/[^\s\"';|&<>]+)")
_LITERAL_PATH = re.compile(r"/[A-Za-z0-9_.+@=:/-]+")
_MODULE = re.compile(r"(?:^|\s)-m\s+([A-Za-z_][A-Za-z0-9_.]*)")
_MAX_REFERENCES = 128
_MAX_SOURCE_BYTES = 8 * 1024 * 1024


class CommandSourceUnavailable(ValueError):
    """A current local execution/configuration dependency cannot be bound."""


@dataclass(frozen=True)
class CommandSourceFact:
    path: str
    resolved: str
    kind: str
    device: int
    inode: int
    ctime_ns: int
    mode: int
    uid: int
    sha256: str
    source_device: int = 0
    source_inode: int = 0
    source_ctime_ns: int = 0

    @classmethod
    def parse(cls, value: object) -> CommandSourceFact:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise CommandSourceUnavailable("local command-source schema differs")
        if any(not isinstance(value[key], str) or not Path(value[key]).is_absolute() or len(value[key]) > 4096 or ".." in Path(value[key]).parts for key in ("path", "resolved")):
            raise CommandSourceUnavailable("local command-source path differs")
        if value["kind"] not in ("missing", "directory", "file", "trusted-link") or any(type(value[key]) is not int or value[key] < 0 for key in ("device", "inode", "ctime_ns", "mode", "uid", "source_device", "source_inode", "source_ctime_ns")):
            raise CommandSourceUnavailable("local command-source identity differs")
        if not isinstance(value["sha256"], str) or (value["kind"] == "file" and not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])) or (value["kind"] != "file" and value["sha256"]):
            raise CommandSourceUnavailable("local command-source digest differs")
        return cls(**value)


def _trusted_program(path: Path) -> bool:
    """Preserve the trusted installed distro executable/library boundary."""
    return any(path == root or path.is_relative_to(root) for root in _TRUSTED_EXECUTABLE_ROOTS)


def _references(text: str) -> tuple[str, ...]:
    references: set[str] = set()
    for match in _ABSOLUTE_PATH.finditer(text):
        name = match.group(1)
        if not _LITERAL_PATH.fullmatch(name):
            if _trusted_program(Path(name)):
                continue
            raise CommandSourceUnavailable("nonliteral local command-source path is unsupported")
        # The bounded grammar never truncates a quoted path at its first space.
        # A shell string starting with a local command may be rejected as well;
        # an explicit direct command remains available for this narrow profile.
        if match.start() and text[match.start() - 1] in ("'", '"'):
            quote = text[match.start() - 1]
            end = text.find(quote, match.end())
            if end >= 0 and any(char.isspace() for char in text[match.end() : end]) and not _trusted_program(Path(name)):
                raise CommandSourceUnavailable("quoted local command-source whitespace is unsupported")
        references.add(name)
    return tuple(sorted(references))


def _dynamic_local_dispatch(text: str) -> bool:
    # Literal direct paths can be inspected. An unresolved executable/source
    # expression cannot be made safe by approving the wrapper's name alone.
    return bool(
        re.search(r"(?:^|[\n;|&])\s*(?:exec\s+)?[\"']?(?:\$|`)", text)
        or re.search(r"(?:^|[\s;])(?:source|\.)\s+(?!/)[^\s;]+", text)
        or re.search(r"(?:\bexec\s+|\b(?:sh|bash|dash|python[0-9.]*)\s+)(?:\$|`)", text)
        or re.search(r"\b(?:sh|bash|dash|python[0-9.]*|node|perl|ruby)\s+(?:\.\.?/|[A-Za-z0-9_-]+\.(?:sh|py|js|pl|rb)\b)", text)
        or re.search(r"\b(?:sh|bash|dash)\s+-c\s+[\"']?\s*(?:\$|`)", text)
        or re.search(r"\bpython[0-9.]*\s+(?:-[IB]\s+)*-c\s", text)
        or re.search(r"(?:^|[\s\"';|&])(?:exec\s+)?(?:\.\.?/|~/)", text)
        or re.search(r"(?:\$[A-Za-z_][A-Za-z0-9_]*|\$\{[^}]+\})/", text)
        or re.search(r"\b(?:exec|sh|bash|dash|python[0-9.]*|node|perl|ruby)\s+(?!/|-)['\"]?[A-Za-z0-9_.-]+/", text)
    )


def observe_command_sources(
    text: str,
    *,
    uid: int,
    cues: tuple[str, ...],
    known_modules: tuple[tuple[str, str], ...] = (),
) -> tuple[tuple[CommandSourceFact, ...], bool]:
    """Read known local inputs; selectors never decide whether a source is safe."""
    if len(text.encode()) > _MAX_SOURCE_BYTES:
        raise CommandSourceUnavailable("local command source text exceeds bound")
    selected_modules = dict(known_modules)

    def inputs(source: str) -> list[str]:
        if _dynamic_local_dispatch(source):
            raise CommandSourceUnavailable("opaque dynamic or relative local script dispatch is unsupported")
        references = list(_references(source))
        modules = _MODULE.findall(source) if re.search(r"\bpython[0-9.]*\b", source) else []
        for module in modules:
            if module not in selected_modules or not re.search(r"(?:^|\s)-I(?:\s|$)", source) or not re.search(r"(?:^|\s)-B(?:\s|$)", source):
                raise CommandSourceUnavailable("local Python module dispatch lacks a reviewed isolated source binding")
            references.append(selected_modules[module])
        return references

    pending = inputs(text)
    facts: dict[str, CommandSourceFact] = {}
    candidate = any(cue in text for cue in cues)
    total_bytes = 0
    owners = frozenset((0, uid))
    while pending:
        name = pending.pop()
        if name in facts:
            continue
        path = Path(name)
        if not path.is_absolute() or ".." in path.parts or len(name) > 4096:
            raise CommandSourceUnavailable("local command-source path is unsupported")
        if any(char in name for char in ("%", "~", "*", "?", "\\")):
            if _trusted_program(path):
                continue
            raise CommandSourceUnavailable("dynamic local command-source path is unsupported")
        if len(facts) >= _MAX_REFERENCES:
            raise CommandSourceUnavailable("local command-source dependency inventory exceeds bound")
        try:
            original = path.lstat()
            resolved = path.resolve(strict=True)
            target = resolved.stat()
        except FileNotFoundError:
            if not _trusted_program(path):
                facts[name] = CommandSourceFact(name, name, "missing", 0, 0, 0, 0, 0, "")
            continue
        if _trusted_program(path) and _trusted_program(resolved):
            # Trusted distro programs are not recursively interpreted. Root's
            # unrelated-service review remains a deliberate trust boundary.
            continue
        if stat.S_ISSOCK(target.st_mode) or stat.S_ISCHR(target.st_mode):
            continue
        if original.st_uid not in owners or target.st_uid not in owners or target.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise CommandSourceUnavailable("referenced local command source is mutable outside its approved owner")
        common = (name, str(resolved), "", original.st_dev, original.st_ino, original.st_ctime_ns, original.st_mode, original.st_uid, "", target.st_dev, target.st_ino, target.st_ctime_ns)
        if _trusted_program(resolved):
            facts[name] = CommandSourceFact(common[0], common[1], "trusted-link", *common[3:])
        elif stat.S_ISDIR(target.st_mode):
            facts[name] = CommandSourceFact(common[0], common[1], "directory", *common[3:])
        elif stat.S_ISREG(target.st_mode):
            try:
                data, after = _protected_source(resolved, owners=owners)
            except (OSError, UserControllerUnavailable) as exc:
                raise CommandSourceUnavailable("referenced local script/configuration cannot be independently read") from exc
            total_bytes += len(data)
            if total_bytes > _MAX_SOURCE_BYTES:
                raise CommandSourceUnavailable("local command-source bytes exceed bound")
            facts[name] = CommandSourceFact(name, str(resolved), "file", original.st_dev, original.st_ino, original.st_ctime_ns, original.st_mode, original.st_uid, hashlib.sha256(data).hexdigest(), after.st_dev, after.st_ino, after.st_ctime_ns)
            candidate = candidate or any(cue.encode() in data for cue in cues)
            # Follow explicit local paths in scripts, but do not parse an
            # arbitrary binary or treat data-file strings as commands.
            if data.startswith(b"#!") or path.suffix in (".sh", ".py", ".pl", ".rb", ".js"):
                try:
                    script = data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise CommandSourceUnavailable("local script source encoding is unsupported") from exc
                pending.extend(inputs(script))
        else:
            # Socket and device paths are external endpoints, not executable
            # source. Their actual deployment use is checked by its profile.
            if not (stat.S_ISSOCK(target.st_mode) or stat.S_ISCHR(target.st_mode)):
                raise CommandSourceUnavailable("local command source is an unsupported filesystem object")
        if path.resolve(strict=True) != resolved or (path.lstat().st_dev, path.lstat().st_ino, path.lstat().st_ctime_ns) != (original.st_dev, original.st_ino, original.st_ctime_ns):
            raise CommandSourceUnavailable("referenced local command source changed during observation")
    return tuple(facts[name] for name in sorted(facts)), candidate
