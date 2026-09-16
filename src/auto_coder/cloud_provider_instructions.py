"""Provider-scoped initial-instruction composition with lossless task preservation.

This module defines a pure configuration/composition boundary: independently
editable initial-instruction slots for each supported cloud provider (Jules,
Claude Routine, Codex Cloud), and a function that composes at most one
Auto-Coder-owned instruction component onto a raw task string without ever
mutating the original task bytes.

Scope note: this module does not perform any provider dispatch, transport, or
session-lifecycle work. It never talks to GitHub or a cloud provider. Wiring
actual startup/recovery call sites to this boundary is owned by a separate
change; until that wiring lands, the shipped instruction entries below are
all empty, so composing a prompt through this module for any provider is a
no-op that returns the original task unchanged.

Configuration
-------------
Initial instruction text is read from ``src/auto_coder/prompts.yaml`` under::

    cloud_provider_instructions:
      jules:
        initial: ""
      claude-routine:
        initial: ""
      codex-cloud:
        initial: ""

An equivalent flat dotted key (e.g. ``"cloud_provider_instructions.jules.initial"``
as a single top-level mapping key) is also supported and takes precedence over
the nested form when present, mirroring `prompt_loader._traverse`'s existing
flat-key-first precedence. A present flat key wins even when its value is
empty or invalid (an invalid flat value raises rather than silently falling
back to the nested entry). An absent entry, or a string containing only
whitespace, is the neutral "no component" case; it must never raise and must
never be supplied by another provider's entry or an implicit default.

Composition contract
---------------------
`prepare_cloud_task` always rebuilds its output purely from the retained
original task text plus the freshly resolved instruction for the currently
requested recipient/operation/no-edit inputs. It never inspects the previous
output for markers, and it never appends more than one managed component.
This is what makes repeated composition idempotent, makes switching providers
replace (rather than accumulate) the managed component, and makes it safe for
a raw task to contain text that merely *looks like* a managed component
(that text is inert data, not something this module searches for or reacts
to).

When a component applies, it is appended after the complete original task,
never prepended, so any leading YAML frontmatter in the original task stays
at the very start of the outgoing prompt (frontmatter consumers such as
`jules_engine._parse_prompt_file_content` only recognize a frontmatter block
anchored at position 0).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict, Optional, Union

from .prompt_loader import load_prompts

SCHEMA_VERSION = 1

SUPPORTED_CLOUD_PROVIDERS = ("jules", "claude-routine", "codex-cloud")

_CONFIG_ROOT_KEY = "cloud_provider_instructions"

_COMPONENT_HEADING = "===== AUTO-CODER CLOUD PROVIDER INITIAL INSTRUCTIONS ({recipient}) ====="
_COMPONENT_FOOTER = "===== END AUTO-CODER CLOUD PROVIDER INITIAL INSTRUCTIONS ====="

_PREPARED_PROMPT_FIELDS = frozenset(
    {
        "schema_version",
        "original_task",
        "recipient",
        "operation",
        "no_edit",
        "instruction_text",
        "instruction_revision",
        "prepared_task",
    }
)


class CloudProviderInstructionError(RuntimeError):
    """A configuration or managed-representation error in instruction composition."""


class CloudTaskOperation(str, Enum):
    """Whether a composition call targets a brand-new task/session or a continuation."""

    NEW_TASK = "new_task"
    CONTINUATION = "continuation"


@dataclass(frozen=True)
class PreparedCloudPrompt:
    """A versioned, serializable record of one prompt composition decision.

    Restoring an instance from its serialized form reproduces the exact
    captured `prepared_task` bytes without recomputing anything. Preparing a
    *new* task (including one built from a restored instance's
    `original_task`) always recomputes `prepared_task` from `original_task`
    under the newly supplied recipient/operation/no_edit inputs.
    """

    schema_version: int
    original_task: str
    recipient: str
    operation: str
    no_edit: bool
    instruction_text: Optional[str]
    instruction_revision: Optional[str]
    prepared_task: str

    def to_json(self) -> str:
        """Return a deterministic, lossless JSON serialization of this record."""
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


def _validate_entry_value(value: Any, key: str) -> Optional[str]:
    """Interpret one configured instruction value; enforce REQ-006 typing."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise CloudProviderInstructionError(f"prompt configuration entry '{key}' must be a string")
    if value.strip() == "":
        return None
    return value


def _resolve_instruction_entry(prompts: Dict[str, Any], provider: str) -> Optional[str]:
    """Resolve one provider's literal initial-instruction text, or None.

    A present flat dotted key (checked first) takes precedence over the
    nested equivalent, even when the flat value is empty or invalid.
    """
    flat_key = f"{_CONFIG_ROOT_KEY}.{provider}.initial"
    if flat_key in prompts:
        return _validate_entry_value(prompts[flat_key], flat_key)

    root = prompts.get(_CONFIG_ROOT_KEY)
    if root is None:
        return None
    if not isinstance(root, dict):
        raise CloudProviderInstructionError(f"prompt configuration entry '{_CONFIG_ROOT_KEY}' must be a mapping")

    provider_node = root.get(provider)
    if provider_node is None:
        return None
    if not isinstance(provider_node, dict):
        raise CloudProviderInstructionError(f"prompt configuration entry '{_CONFIG_ROOT_KEY}.{provider}' must be a mapping")

    nested_key = f"{_CONFIG_ROOT_KEY}.{provider}.initial"
    if "initial" not in provider_node:
        return None
    return _validate_entry_value(provider_node["initial"], nested_key)


def _build_component_block(recipient: str, instruction_text: str) -> str:
    return "\n\n" + _COMPONENT_HEADING.format(recipient=recipient) + "\n" + instruction_text.rstrip("\n") + "\n" + _COMPONENT_FOOTER


def _coerce_operation(operation: Union["CloudTaskOperation", str]) -> "CloudTaskOperation":
    if isinstance(operation, CloudTaskOperation):
        return operation
    if isinstance(operation, str):
        try:
            return CloudTaskOperation(operation)
        except ValueError as exc:
            raise CloudProviderInstructionError(f"unsupported cloud task operation: {operation!r}") from exc
    raise CloudProviderInstructionError("operation must be a CloudTaskOperation or its string value")


def prepare_cloud_task(
    task: Union[str, PreparedCloudPrompt],
    *,
    recipient: str,
    operation: Union["CloudTaskOperation", str],
    no_edit: bool,
    prompts_path: Optional[str] = None,
) -> PreparedCloudPrompt:
    """Compose at most one Auto-Coder-owned instruction component onto a task.

    `task` may be a raw task string, or a previously prepared
    `PreparedCloudPrompt` (in which case its retained `original_task` is what
    gets recomposed; its previous `prepared_task` is never reused or built
    upon). The result is always rebuilt purely from the original task text
    and the instruction resolved for the currently supplied inputs.

    Only a new task/session (`operation` is `NEW_TASK`) for one of the three
    supported provider keys, with `no_edit` false, is eligible to receive its
    configured component. Every other combination (continuation, no-edit,
    local/unrecognized recipient) returns the original task unchanged.
    """
    if isinstance(task, PreparedCloudPrompt):
        original_task = task.original_task
    elif isinstance(task, str):
        original_task = task
    else:
        raise CloudProviderInstructionError("task must be a string or a PreparedCloudPrompt")

    if not isinstance(recipient, str):
        raise CloudProviderInstructionError("recipient must be a string")

    resolved_operation = _coerce_operation(operation)
    eligible = resolved_operation is CloudTaskOperation.NEW_TASK and not no_edit and recipient in SUPPORTED_CLOUD_PROVIDERS

    instruction_text: Optional[str] = None
    instruction_revision: Optional[str] = None
    if eligible:
        prompts = load_prompts(prompts_path)
        instruction_text = _resolve_instruction_entry(prompts, recipient)
        if instruction_text is not None:
            instruction_revision = hashlib.sha256(instruction_text.encode("utf-8")).hexdigest()

    if instruction_text is None:
        prepared_task = original_task
    else:
        prepared_task = original_task + _build_component_block(recipient, instruction_text)

    return PreparedCloudPrompt(
        schema_version=SCHEMA_VERSION,
        original_task=original_task,
        recipient=recipient,
        operation=resolved_operation.value,
        no_edit=bool(no_edit),
        instruction_text=instruction_text,
        instruction_revision=instruction_revision,
        prepared_task=prepared_task,
    )


def _validate_prepared_prompt(record: PreparedCloudPrompt) -> PreparedCloudPrompt:
    if type(record.schema_version) is not int or record.schema_version != SCHEMA_VERSION:
        raise CloudProviderInstructionError("unsupported schema_version for prepared cloud prompt")
    if type(record.original_task) is not str:
        raise CloudProviderInstructionError("original_task must be a string")
    if type(record.recipient) is not str:
        raise CloudProviderInstructionError("recipient must be a string")
    if type(record.operation) is not str or record.operation not in (op.value for op in CloudTaskOperation):
        raise CloudProviderInstructionError("operation must be a recognized cloud task operation")
    if type(record.no_edit) is not bool:
        raise CloudProviderInstructionError("no_edit must be a boolean")
    if record.instruction_text is not None and type(record.instruction_text) is not str:
        raise CloudProviderInstructionError("instruction_text must be a string or null")
    if record.instruction_revision is not None and type(record.instruction_revision) is not str:
        raise CloudProviderInstructionError("instruction_revision must be a string or null")
    if (record.instruction_text is None) != (record.instruction_revision is None):
        raise CloudProviderInstructionError("instruction_text and instruction_revision must both be present or both absent")
    if type(record.prepared_task) is not str:
        raise CloudProviderInstructionError("prepared_task must be a string")
    return record


def restore_prepared_cloud_task(payload: str) -> PreparedCloudPrompt:
    """Deserialize a `PreparedCloudPrompt` produced by `PreparedCloudPrompt.to_json`.

    This is pure restoration: it reproduces the exact captured record,
    including its `prepared_task` bytes, without recomputing anything from
    `original_task`. Malformed or unsupported payloads raise
    `CloudProviderInstructionError` rather than being reinterpreted as a
    clean raw task.
    """
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as exc:
        raise CloudProviderInstructionError("prepared cloud prompt payload is not valid JSON") from exc
    if not isinstance(data, dict) or set(data) != _PREPARED_PROMPT_FIELDS:
        raise CloudProviderInstructionError("prepared cloud prompt payload must contain exactly the v1 fields")
    try:
        record = PreparedCloudPrompt(**data)
    except TypeError as exc:
        raise CloudProviderInstructionError("prepared cloud prompt payload has invalid field types") from exc
    return _validate_prepared_prompt(record)
