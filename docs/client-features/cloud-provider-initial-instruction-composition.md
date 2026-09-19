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

`cloud_provider_instructions.py` itself performs no GitHub or cloud provider
transport; it is a pure composition boundary. See
[`jules-autonomous-execution-checkpoint-recovery-guidance.md`](jules-autonomous-execution-checkpoint-recovery-guidance.md)
for the shipped Jules initial-instruction text and its regression coverage
(Issue #2092). It is wired into every supported provider's actual new-task
submission boundary:
`JulesClient.start_session`, `ClaudeRoutineClient.fire_routine`, and
`CodexCloudClient.submit_task` each call `prepare_cloud_task` with their own
canonical `recipient` immediately before building the outgoing HTTP
payload/CLI argument, so their public wrappers (`start_task`, `_run_llm_cli`,
Issue dispatch in `issue_processor.py`/`pr_processor.py`, and Jules recurrent
task launch in `jules_engine.py`) all compose exactly once regardless of
which wrapper was used to reach them. `send_followup` on each client passes
`operation=CONTINUATION`, which is always ineligible, so existing-session
follow-ups (PR review/CI/conflict repair, ordinary resume) never receive the
initial component. Because eligibility and composition happen at the
concrete adapter method rather than in a shared prompt template, the
historical `is_jules=True` template flag that Claude Routine dispatch also
sets (`issue_processor.py`, `pr_processor.py`) has no bearing on which
component is chosen. The shipped Jules entry now carries real guidance text (Issue #2092), so
composing through this module for a new Jules session decorates the outgoing
prompt in practice. The Claude Routine and Codex Cloud entries remain empty
by design (see the linked fragment above), so composing for either of them
is still a no-op.

## Recovering the original task (`managed_prompts.py`)

`src/auto_coder/managed_prompts.py` is the durable side channel that makes a
composed prompt's original task recoverable after the current process (and
any in-memory state) is gone. Every successful composition at a client
boundary calls `save_managed_prompt(repo_name, task_id, prepared)`, which
records the full `PreparedCloudPrompt` (keyed by the provider's own
task/session id) under `~/.auto-coder/<repo>/managed_prompts.json`.

`recover_original_task(task_text, repo_name, task_id)` is the read side:

- A durable record, when present and valid, is authoritative — it returns
  that record's retained `original_task`, ignoring `task_text` itself.
- Absent a record, a prompt containing no managed-instruction marker is a
  genuine legacy/undecorated task and is returned unchanged.
- Absent a record, a prompt that *does* contain the marker cannot be split
  back into original task and managed component without guessing, so this
  raises `ManagedPromptRecoveryError` instead of stripping guessed text or
  resending the opaque payload as-is.

`jules_engine.check_and_resume_or_archive_sessions`'s failed-session restart
path is the one call site that resends a saved prompt as a new session: it
calls `recover_original_task` (and reads the saved record's `no_edit`) before
rebuilding, and lets `ManagedPromptRecoveryError` abort that specific restart
rather than send an ambiguous payload. The remaining Jules recurrent-task
call sites (`check_and_start_recurrent_jules_tasks`,
`check_and_restart_recurrent_jules_task_for_pr`) only need to *match* a
session's YAML frontmatter, not resend it — since composition always
appends after the complete original task, frontmatter anchored at the start
of the string survives decoration either way, so matching works directly off
the raw saved prompt. Those two sites still attempt best-effort recovery and
silently fall back to the raw prompt on `ManagedPromptRecoveryError`, so a
missing or corrupted managed record can never be misread as "no matching
session" and cause a duplicate launch.

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
