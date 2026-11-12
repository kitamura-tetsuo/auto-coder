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
_FSCK_EXECUTABLE = Path("/usr/lib/systemd/systemd-fsck")
_FSCK_FRAGMENT = Path("/usr/lib/systemd/system/systemd-fsck@.service")
_FSCK_FRAGMENT_SHA256 = "43be4420263da4e79a7fe33f6b628449cc56cf99ca30cdc4dedc7f53aff7ea7e"
_FSCK_EXEC_ROW = re.compile(
    r"\{ path=/usr/lib/systemd/systemd-fsck ; argv\[\]=/usr/lib/systemd/systemd-fsck "
    r"(?P<operand>/dev/[A-Za-z0-9_.+@=:/-]+) ; ignore_errors=no ; "
    r"start_time=\[[^\]\r\n]{1,128}\] ; stop_time=\[[^\]\r\n]{1,128}\] ; "
    r"pid=[0-9]{1,20} ; code=(?:\(null\)|[a-z-]{1,32}) ; status=[0-9]{1,10}/(?:[0-9]{1,10}|[A-Z0-9]{1,32}) \}"
)


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


@dataclass(frozen=True)
class SourceNodeIdentity:
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    rdev: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class FsckBlockOperandFact:
    path: str
    resolved: str
    entry: SourceNodeIdentity
    target: SourceNodeIdentity
    executable: SourceNodeIdentity


def _source_node_identity(info: os.stat_result) -> SourceNodeIdentity:
    return SourceNodeIdentity(info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_rdev, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def fsck_block_operand(command: str) -> tuple[str, str] | None:
    """Identify one direct fsck operand while preserving the remaining row."""
    match = _FSCK_EXEC_ROW.fullmatch(command)
    if match is None:
        return None
    operand = match.group("operand")
    if len(operand) > 4096 or ".." in Path(operand).parts:
        return None
    start, end = match.span("operand")
    return operand, command[:start] + command[end:]


def observe_reviewed_fsck_fragment(unit_source: str) -> CommandSourceFact:
    """Require the exact native vendor source, without a hidden extra fragment."""
    data, info = _protected_source(_FSCK_FRAGMENT, owners=frozenset((0,)))
    digest = hashlib.sha256(data).hexdigest()
    expected = "# " + str(_FSCK_FRAGMENT) + "\n" + data.decode("utf-8")
    if _FSCK_FRAGMENT.resolve(strict=True) != _FSCK_FRAGMENT or digest != _FSCK_FRAGMENT_SHA256 or unit_source.rstrip("\n") != expected.rstrip("\n"):
        raise CommandSourceUnavailable("fsck block operand lacks the exact reviewed vendor fragment")
    return CommandSourceFact(str(_FSCK_FRAGMENT), str(_FSCK_FRAGMENT), "file", info.st_dev, info.st_ino, info.st_ctime_ns, info.st_mode, info.st_uid, digest, info.st_dev, info.st_ino, info.st_ctime_ns)


def observe_fsck_block_operand(name: str) -> FsckBlockOperandFact:
    """Stat only the certified data operand; never open or read a block device."""
    path = Path(name)
    if not _LITERAL_PATH.fullmatch(name) or len(name) > 4096 or not path.is_relative_to("/dev") or ".." in path.parts:
        raise CommandSourceUnavailable("fsck block operand path is unsupported")

    def snapshot() -> FsckBlockOperandFact:
        entry = path.lstat()
        resolved = path.resolve(strict=True)
        target = resolved.lstat()
        executable = _FSCK_EXECUTABLE.lstat()
        if not (stat.S_ISLNK(entry.st_mode) or stat.S_ISBLK(entry.st_mode)) or entry.st_uid != 0 or not resolved.is_relative_to("/dev") or not stat.S_ISBLK(target.st_mode) or target.st_uid != 0:
            raise CommandSourceUnavailable("fsck operand is not a root-owned block data endpoint")
        if _FSCK_EXECUTABLE.resolve(strict=True) != _FSCK_EXECUTABLE or not stat.S_ISREG(executable.st_mode) or executable.st_uid != 0 or executable.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or not executable.st_mode & stat.S_IXUSR:
            raise CommandSourceUnavailable("fsck executable is not the canonical protected installed program")
        return FsckBlockOperandFact(name, str(resolved), _source_node_identity(entry), _source_node_identity(target), _source_node_identity(executable))

    first = snapshot()
    if snapshot() != first:
        raise CommandSourceUnavailable("fsck block data endpoint or executable changed during readback")
    return first


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


def _shell_script_references(text: str) -> tuple[str, ...]:
    """Retain shell-source context for literal interpreter operands."""
    references: set[str] = set()
    for match in re.finditer(r"(?<![A-Za-z0-9_./-])(?:/(?:usr/)?bin/)?(?:sh|bash|dash)[ \t]+([^\n;]+)", text):
        references.update(_references(match.group(1)))
    return tuple(sorted(references))


def _environment_file_references(text: str) -> tuple[str, ...]:
    """Recognize only the literal EnvironmentFiles readback row grammar."""
    return tuple(re.findall(r"(?m)^[ \t]*(?:EnvironmentFiles=)?(/[A-Za-z0-9_.+@=:/-]+)[ \t]+\(ignore_errors=(?:yes|no)\)[ \t]*$", text))


def _bash_startup_environment(text: str) -> bool:
    return bool(re.search(r"\bBASH_ENV[ \t]*=", text))


def _execution_command_views(text: str) -> tuple[tuple[str, str], ...]:
    """Separate actual argv from systemctl's trailing observation metadata."""
    result = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("{ path="):
            result.append(("", line))
            continue
        executable, first, remaining = line[7:].partition(" ; argv[]=")
        arguments, last, metadata = remaining.rpartition(" ; ignore_errors=")
        if not first or not last or not _LITERAL_PATH.fullmatch(executable) or not re.match(r"(?:yes|no) ;", metadata) or not metadata.endswith(" }"):
            raise CommandSourceUnavailable("effective command structure or executable is unsupported")
        dispatchers = r"(?:sh|dash|bash|nice|nohup|setsid|timeout|flock|stdbuf|ionice|chrt|taskset|env|python[0-9.]*|node|nodejs|perl|ruby)"
        if re.fullmatch(dispatchers, executable.rsplit("/", 1)[-1]) and (not arguments.split() or arguments.split()[0] != executable):
            raise CommandSourceUnavailable("overridden dispatcher argv[0] is unsupported")
        result.append((executable, arguments))
    return tuple(result)


def _unresolved_wrapper_dispatch(text: str) -> bool:
    """Keep a finite literal-target profile for common command launchers."""
    pattern = r"(?<![A-Za-z0-9_./-])(?:/(?:usr/)?bin/)?(nice|nohup|setsid|timeout|flock|stdbuf|ionice|chrt|taskset)[\"']?[ \t]+"
    for _, command in _execution_command_views(text):
        if _unresolved_wrapper_command(command, pattern):
            return True
    return False


def _unresolved_wrapper_command(command: str, pattern: str) -> bool:
    for match in re.finditer(pattern, command):
        launcher, arguments = match.group(1), command[match.end() :]
        words = arguments.split()
        if not words:
            continue
        if launcher == "nice":
            while words and words[0].startswith("-"):
                option = words.pop(0)
                if option == "--":
                    break
                if option in ("-n", "--adjustment"):
                    if not words or not re.fullmatch(r"[+-]?[0-9]{1,20}", words.pop(0)):
                        return True
                elif not re.fullmatch(r"(?:-[0-9]{1,20}|-n[+-]?[0-9]{1,20}|--adjustment=[+-]?[0-9]{1,20})", option):
                    return True
        elif words[0] == "--":
            words.pop(0)
        elif words[0].startswith("-"):
            return True
        if launcher in ("timeout", "flock", "chrt", "taskset"):
            if not words:
                return True
            parameter = words.pop(0)
            grammar = {"timeout": r"[0-9]{1,20}(?:\.[0-9]{1,20})?[smhd]?", "flock": r"(?:/[A-Za-z0-9_.+@=:/-]+|[0-9]{1,20})", "chrt": r"[0-9]{1,3}", "taskset": r"(?:0x)?[0-9a-fA-F]{1,64}"}[launcher]
            if not re.fullmatch(grammar, parameter):
                return True
        if words and not _LITERAL_PATH.fullmatch(words[0]):
            return True
    return False


def _unsupported_shell_invocation(text: str) -> bool:
    """Inline sh/dash supports only one literal absolute command without args."""
    pattern = r"(?<![A-Za-z0-9_./-])(?P<shell>(?:/(?:usr/)?bin/)?(?:sh|bash|dash))[\"']?(?=[ \t]|$)"
    for executable, command in _execution_command_views(text):
        if executable.rsplit("/", 1)[-1] in ("sh", "bash", "dash") and (not command.split() or command.split()[0] != executable):
            # systemd's @ executable prefix may override argv[0], including
            # -sh, which starts a login shell before a literal script runs.
            return True
        for match in re.finditer(pattern, command):
            shell, arguments = match.group("shell"), command[match.end() :].strip()
            # Bash startup sources may be inherited from the manager even
            # when selected per-unit fields do not expose their values.
            if not shell.startswith("/") or shell.rsplit("/", 1)[-1] == "bash":
                return True
            if re.search(r"(?:^|[ \t])[\"']?(?:-[A-Za-z]*c[A-Za-z]*|--command)[\"']?(?=[ \t]|$)", arguments):
                if not re.fullmatch(r"-c[ \t]+(?P<quote>[\"']?)/[A-Za-z0-9_.+@=:/-]+(?P=quote)[ \t]*", arguments):
                    return True
                continue
            words = arguments.split()
            while words and words[0].startswith("-"):
                option = words.pop(0)
                if option == "--":
                    break
                if not re.fullmatch(r"-[aefnuvxC]+", option):
                    return True
            if not words:
                if not command.startswith("#!"):
                    return True
            elif not _LITERAL_PATH.fullmatch(words[0]):
                return True
    return False


def _unprofiled_interpreter_dispatch(text: str, known_modules: dict[str, str]) -> bool:
    """Unreviewed local interpreter dependency graphs are not a source profile."""
    pattern = r"(?<![A-Za-z0-9_./-])(?P<program>(?:/[A-Za-z0-9_.+@=:/-]+/)?(?:python[0-9.]*|node|nodejs|perl|ruby))[\"']?(?=[ \t]|$)"
    for _, command in _execution_command_views(text):
        for match in re.finditer(pattern, command):
            program = match.group("program")
            if not program.startswith("/"):
                return True
            family = program.rsplit("/", 1)[-1]
            words = command[match.end() :].split()
            options: set[str] = set()
            module = False
            while words and words[0].startswith("-"):
                option = words.pop(0)
                if option == "--":
                    break
                if family.startswith("python") and option == "-m":
                    if not words or words.pop(0) not in known_modules or not {"-I", "-B"} <= options:
                        return True
                    module = True
                    break
                if family.startswith("python") and re.fullmatch(r"-[BIEsSuOqvP]+", option):
                    options.update("-" + char for char in option[1:])
                elif family in ("perl", "ruby") and re.fullmatch(r"-[wTW]+", option):
                    continue
                else:
                    return True
            if module:
                continue
            if not words or not _LITERAL_PATH.fullmatch(words[0]) or ".." in Path(words[0]).parts:
                return True
            script = Path(words[0])
            try:
                if not _trusted_program(script) or not _trusted_program(script.resolve(strict=True)):
                    return True
            except OSError:
                return True
    return False


def _dynamic_local_dispatch(text: str, *, shell_source: bool = False) -> bool:
    # Literal direct paths can be inspected. An unresolved executable/source
    # expression cannot be made safe by approving the wrapper's name alone.
    # Known launchers can dispatch bare names through mutable PATH entries.
    # This profile does not infer their effective executable search.
    if _bash_startup_environment(text) or _unresolved_wrapper_dispatch(text) or _unsupported_shell_invocation(text):
        return True
    if re.search(r"(?<![A-Za-z0-9_./-])(?:/(?:usr/)?bin/)?env[\"']?(?=[ \t]|$)", text):
        return True
    if shell_source:
        # Support only literal command lines and non-dispatching builtins.
        # Unknown shell control syntax must not conceal another PATH lookup.
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if re.search(r"[\\;$`|&<>(){}]", line):
                return True
            if re.fullmatch(r"(?:export[ \t]+)?[A-Za-z_][A-Za-z0-9_]*=[A-Za-z0-9_./:+@=-]*", line) or re.fullmatch(r"set[ \t]+-[aefnuvxC]+", line):
                continue
            words = line.split()
            if words[0] in ("exec", ".", "source"):
                words = words[1:]
                if not words or not _LITERAL_PATH.fullmatch(words[0]):
                    return True
            elif not (_LITERAL_PATH.fullmatch(words[0]) or words[0] in (":", "exit")):
                return True
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
    shell_source: bool = False,
) -> tuple[tuple[CommandSourceFact, ...], bool]:
    """Read known local inputs; selectors never decide whether a source is safe."""
    if len(text.encode()) > _MAX_SOURCE_BYTES:
        raise CommandSourceUnavailable("local command source text exceeds bound")
    selected_modules = dict(known_modules)

    shell_inputs: set[str] = set()
    environment_inputs: set[str] = set()
    reviewed_module_inputs: set[str] = set()

    def inputs(source: str, *, shell_source: bool = False, reviewed_module: bool = False) -> list[str]:
        shell_source = shell_source or bool(re.match(r"#![^\n]*(?:/|[ \t])(?:sh|bash|dash)(?:[ \t]|\n|$)", source))
        executable_source = source.partition("\n")[2] if reviewed_module and source.startswith("#!") else source
        if _unprofiled_interpreter_dispatch(executable_source, selected_modules):
            raise CommandSourceUnavailable("unprofiled local interpreter dispatch or source binding is unsupported")
        if _dynamic_local_dispatch(executable_source, shell_source=shell_source):
            raise CommandSourceUnavailable("opaque dynamic or relative local script dispatch is unsupported")
        references = list(_references(source))
        shell_inputs.update(references if shell_source else _shell_script_references(source))
        environment_inputs.update(_environment_file_references(source))
        modules = _MODULE.findall(source) if re.search(r"\bpython[0-9.]*\b", source) else []
        for module in modules:
            if module not in selected_modules or not re.search(r"(?:^|\s)-I(?:\s|$)", source) or not re.search(r"(?:^|\s)-B(?:\s|$)", source):
                raise CommandSourceUnavailable("local Python module dispatch lacks a reviewed isolated source binding")
            references.append(selected_modules[module])
            reviewed_module_inputs.add(selected_modules[module])
        return references

    pending = inputs(text, shell_source=shell_source)
    facts: dict[str, CommandSourceFact] = {}
    inspected_shell_inputs: set[str] = set()
    inspected_environment_inputs: set[str] = set()
    candidate = any(cue in text for cue in cues)
    total_bytes = 0
    owners = frozenset((0, uid))
    while pending:
        name = pending.pop()
        if name in facts:
            # An environment/data reference may be read before a wrapper
            # sources that same file. Its newly discovered execution role
            # still requires parsing against the original observed identity.
            previous = facts[name]
            as_shell = name in shell_inputs and name not in inspected_shell_inputs
            as_environment = name in environment_inputs and name not in inspected_environment_inputs
            if (as_shell or as_environment) and previous.kind == "file":
                path = Path(name)
                try:
                    resolved = path.resolve(strict=True)
                    data, after = _protected_source(resolved, owners=owners)
                    info = path.lstat()
                    script = data.decode("utf-8")
                except (OSError, UnicodeDecodeError, UserControllerUnavailable) as exc:
                    raise CommandSourceUnavailable("cached local shell input cannot be independently reobserved") from exc
                if (
                    str(resolved) != previous.resolved
                    or path.resolve(strict=True) != resolved
                    or (info.st_dev, info.st_ino, info.st_ctime_ns, info.st_mode, info.st_uid) != (previous.device, previous.inode, previous.ctime_ns, previous.mode, previous.uid)
                    or (after.st_dev, after.st_ino, after.st_ctime_ns) != (previous.source_device, previous.source_inode, previous.source_ctime_ns)
                    or hashlib.sha256(data).hexdigest() != previous.sha256
                ):
                    raise CommandSourceUnavailable("cached local shell input changed during observation")
                total_bytes += len(data)
                if total_bytes > _MAX_SOURCE_BYTES:
                    raise CommandSourceUnavailable("local command-source bytes exceed bound")
                if as_environment:
                    if _bash_startup_environment(script):
                        raise CommandSourceUnavailable("Bash startup environment is unsupported")
                    inspected_environment_inputs.add(name)
                if as_shell:
                    nested = inputs(script, shell_source=True)
                    inspected_shell_inputs.add(name)
                    pending.extend(nested)
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
            if not _trusted_program(path) or name in environment_inputs:
                facts[name] = CommandSourceFact(name, name, "missing", 0, 0, 0, 0, 0, "")
            continue
        candidate = candidate or any(cue in str(resolved) for cue in cues)
        if _trusted_program(path) and _trusted_program(resolved) and name not in environment_inputs:
            # Trusted distro programs are not recursively interpreted. Root's
            # unrelated-service review remains a deliberate trust boundary.
            continue
        if stat.S_ISSOCK(target.st_mode) or stat.S_ISCHR(target.st_mode):
            continue
        if original.st_uid not in owners or target.st_uid not in owners or target.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise CommandSourceUnavailable("referenced local command source is mutable outside its approved owner")
        common = (name, str(resolved), "", original.st_dev, original.st_ino, original.st_ctime_ns, original.st_mode, original.st_uid, "", target.st_dev, target.st_ino, target.st_ctime_ns)
        if _trusted_program(resolved) and name not in environment_inputs:
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
            interpreted = path.suffix in (".py", ".pl", ".rb", ".js") or bool(re.match(rb"#![^\n]*(?:/|[ \t])(?:python[0-9.]*|node|nodejs|perl|ruby)(?:[ \t]|\n|$)", data))
            if interpreted and name not in reviewed_module_inputs and name not in environment_inputs:
                raise CommandSourceUnavailable("unprofiled local interpreter source is unsupported")
            facts[name] = CommandSourceFact(name, str(resolved), "file", original.st_dev, original.st_ino, original.st_ctime_ns, original.st_mode, original.st_uid, hashlib.sha256(data).hexdigest(), after.st_dev, after.st_ino, after.st_ctime_ns)
            if name in environment_inputs:
                try:
                    environment = data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise CommandSourceUnavailable("environment file encoding is unsupported") from exc
                if _bash_startup_environment(environment):
                    raise CommandSourceUnavailable("Bash startup environment is unsupported")
                inspected_environment_inputs.add(name)
            candidate = candidate or any(cue.encode() in data for cue in cues)
            # Follow explicit local paths in scripts, but do not parse an
            # arbitrary binary or treat data-file strings as commands.
            if name in shell_inputs or data.startswith(b"#!") or path.suffix in (".sh", ".py", ".pl", ".rb", ".js"):
                try:
                    script = data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise CommandSourceUnavailable("local script source encoding is unsupported") from exc
                as_shell = name in shell_inputs or path.suffix == ".sh"
                pending.extend(inputs(script, shell_source=as_shell, reviewed_module=name in reviewed_module_inputs))
                if as_shell:
                    inspected_shell_inputs.add(name)
        else:
            # Socket and device paths are external endpoints, not executable
            # source. Their actual deployment use is checked by its profile.
            if not (stat.S_ISSOCK(target.st_mode) or stat.S_ISCHR(target.st_mode)):
                raise CommandSourceUnavailable("local command source is an unsupported filesystem object")
        if path.resolve(strict=True) != resolved or (path.lstat().st_dev, path.lstat().st_ino, path.lstat().st_ctime_ns) != (original.st_dev, original.st_ino, original.st_ctime_ns):
            raise CommandSourceUnavailable("referenced local command source changed during observation")
    return tuple(facts[name] for name in sorted(facts)), candidate
