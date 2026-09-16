# Cloud provider initial-instruction composition

`src/auto_coder/cloud_provider_instructions.py` defines independently
editable initial-instruction slots for the three supported cloud task
providers — `jules`, `claude-routine`, `codex-cloud` — and a pure
configuration/composition boundary for applying them to a raw task string.
It replaces the ambiguous `is_jules` flag (which `render_prompt(...,
is_jules=True)` calls for both real Jules and Claude Routine dispatch alike;
see `src/auto_coder/issue_processor.py`) as the concept that should eventually
select provider-specific initial guidance, without changing any current
`render_prompt` behavior yet.

This module is a composition boundary only. It performs no GitHub or cloud
provider transport, and nothing in the codebase currently calls it from a
production dispatch path — wiring actual startup/recovery call sites to it is
a separate, later change. Until that wiring lands, the shipped configuration
entries are empty, so composing through this module is a no-op for every
provider.

## Configuration

Each provider's literal instruction text lives in `src/auto_coder/prompts.yaml`
under:

```yaml
cloud_provider_instructions:
  jules:
    initial: ""
  claude-routine:
    initial: ""
  codex-cloud:
    initial: ""
```

An equivalent flat dotted key (e.g. a single top-level mapping key literally
named `"cloud_provider_instructions.jules.initial"`) is also supported and
takes precedence over the nested form whenever it is present — even when its
value is empty (which suppresses the nested entry, yielding "no component")
or invalid (a non-string flat value raises a configuration error rather than
falling back to the nested entry). This mirrors the flat-key-first precedence
`prompt_loader._traverse` already uses elsewhere in this file.

An absent entry, or a string containing only whitespace, means "no
component" — a neutral case, not an error. A present non-string value (in
either form) is a configuration error. Editing or misconfiguring one
provider's entry never changes another provider's resolved component: there
is no shared default and no implicit Jules fallback for a missing entry.

Instruction text is literal: it is composed onto the outgoing prompt as-is
and never undergoes a second `render_prompt` template substitution.

## Composition inputs and eligibility

`prepare_cloud_task(task, *, recipient, operation, no_edit, prompts_path=None)`
takes explicit inputs rather than inferring provider identity from labels,
backend names, model names, prompt contents, or the historical `is_jules`
flag:

- `recipient`: the canonical provider key. Only `"jules"`, `"claude-routine"`,
  and `"codex-cloud"` are eligible for a component; `"local"` and any
  unrecognized string always yield the original task unchanged.
- `operation`: a `CloudTaskOperation` (`NEW_TASK` or `CONTINUATION`, also
  accepted as their string values). Only `NEW_TASK` is eligible; a
  continuation on an existing session never receives the initial component.
- `no_edit`: eligibility requires this to be falsy. A no-edit call never
  receives the initial component.

## Lossless composition

For an eligible call with a non-empty resolved instruction, the result
appends exactly one Auto-Coder-owned block after the complete, unmodified
original task text:

```
<original task, byte-for-byte>

===== AUTO-CODER CLOUD PROVIDER INITIAL INSTRUCTIONS (<recipient>) =====
<instruction text>
===== END AUTO-CODER CLOUD PROVIDER INITIAL INSTRUCTIONS =====
```

The block is always appended, never prepended, so any leading YAML
frontmatter in the original task (the `---\n...\n---\n` block that
`jules_engine._parse_prompt_file_content` recognizes only when anchored at
position 0 of the string) stays at the very start of the outgoing prompt.
When there is no component to add (ineligible call, or an empty/absent
entry), the outgoing prompt equals the original task exactly — no new
header, delimiter, or whitespace change.

Composition never scans the original task for the heading/marker text to
decide whether a component is "already present": a raw task that happens to
quote or discuss the heading, or contains a complete fenced copy of a
component, is inert data. It is preserved untouched, and composition still
adds its own single managed copy on top. Every composition call rebuilds
purely from the retained original task text and the freshly resolved
instruction for the current inputs — it never reuses or inspects a previous
`prepared_task`. This makes repeated composition with identical inputs
idempotent (no duplicate managed block), and makes composing for a different
recipient or a changed instruction revision replace the managed block
entirely rather than layering another one on top or retaining the former
recipient's guidance.

## Serialized representation

`PreparedCloudPrompt` is a versioned (`schema_version`), frozen dataclass
capturing `original_task`, `recipient`, `operation`, `no_edit`,
`instruction_text`, `instruction_revision` (a content hash identifying which
instruction text produced this result), and the exact `prepared_task` bytes.
`PreparedCloudPrompt.to_json()` / `restore_prepared_cloud_task(payload)`
round-trip this record losslessly in a fresh process, with no reliance on an
in-memory cache. Restoring a prepared record reproduces its captured
`prepared_task` bytes exactly; explicitly calling `prepare_cloud_task` again
with a restored record as its `task` argument re-derives a fresh
`prepared_task` from that record's retained `original_task` under whatever
`recipient`/`operation`/`no_edit`/configuration are supplied this time.
Malformed or schema-mismatched serialized payloads raise
`CloudProviderInstructionError` rather than being reinterpreted as a clean
raw task.
