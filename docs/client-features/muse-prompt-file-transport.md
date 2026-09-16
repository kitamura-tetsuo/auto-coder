# Muse prompt-file transport

Every local Muse task is rendered in full and delivered to `muse exec` through
exactly one adapter-owned `--prompt-file`.  Each invocation uses a distinct,
owner-only regular file outside the target worktree and Git metadata; the file
is finalized before launch, remains available until the child exits, and is
removed on every handled outcome.  Conflicting configured prompt sources fail
before launch, while model, mode, authentication, workspace, session arguments,
and the existing Git-state audit remain unchanged.  Prompt contents are never
placed in process arguments, environment values, shell intermediaries, or
diagnostics.
