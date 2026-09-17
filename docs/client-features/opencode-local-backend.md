# OpenCode local backend

OpenCode is available as the `opencode` backend and as named aliases with
`backend_type = "opencode"`. Each alias must set an explicit `model` in
`provider/model` form (e.g. `model = "anthropic/claude-sonnet-4-5"`);
OpenCode's remembered/default model is never selected implicitly, and a
missing or malformed model fails before the prompt is submitted. Install and
authenticate the `opencode` CLI (or set a provider credential such as
`OPENAI_API_KEY`/`OPENROUTER_API_KEY`, or configure the backend's `api_key`)
before startup; Auto-Coder never runs interactive login and never enables
public session sharing.

Auto-Coder invokes `opencode run --format json --dir <execution directory>
--model <provider/model>` once per task, transporting the complete rendered
prompt through stdin followed by EOF (never via argv, environment variables,
or a positional message). The invocation is always finite, non-interactive,
and fresh: `--attach`, `--command`, positional/`--file` prompt sources,
`--continue`/`--session`/`--fork`, `--share`, and permission-weakening flags
such as `--auto`/`--yolo`/`--dangerously-skip-permissions` are rejected
before the task launches, whether they come from the backend's configured
`options` or from a one-time CLI override. A configured `--variant` (or a
similar model-specific option) is still honored. No-edit execution is not
implemented for this backend: a no-edit request fails before task launch
instead of silently running as an edit.

OpenCode may inspect files, edit the working tree, and run tests or other
implementation/build commands, but Auto-Coder exclusively owns staging,
commits, branches/HEAD, merges/rebases, pushes, and GitHub Issue/PR lifecycle
operations. While OpenCode runs, its `git` and `gh` executables are replaced
(via a `PATH`-prepended, per-invocation directory) with wrappers that deny
every subcommand except a small read-only allowlist (status, diff, log,
show, and similar) before the real executable ever runs; `gh` is denied
outright. Auto-Coder additionally snapshots the branch, HEAD, refs, and
staged index before the run and re-asserts them afterward (and on timeout),
restoring and failing the invocation if anything still changed. A denied or
detected lifecycle mutation makes the invocation unusable for publication;
Auto-Coder only stages, commits, and pushes the working-tree result after
the run succeeds and this boundary is confirmed intact.

A successful result requires exit status zero, a single consistent
root-session event stream with no session-level error event, and a
completed final assistant message whose terminal `step_finish` reason is
`stop`. The returned text is exactly that message's completed text parts, in
emitted order, deduplicated by part identity; tool output, reasoning,
intermediate assistant messages, and event envelopes are excluded.
Tool-call-only completion, a non-`stop` finish reason, missing final text, a
malformed event, or a session-identity conflict fails the invocation instead
of returning a best-effort result. A recoverable tool failure followed by a
valid completed answer is not itself a terminal failure. Terminal
rate-limit/quota evidence (inspected only from stderr and session-error
diagnostics, never from assistant or tool content) raises
`AutoCoderUsageLimitError`; an invocation timeout raises
`AutoCoderTimeoutError` and terminates the whole process group (not just the
immediate child) before any workspace cleanup or replacement execution; an
exhausted transient provider transport failure raises
`AutoCoderRetryableBackendError`; other terminal failures (execution,
protocol, authentication, configuration) raise a backend failure with an
actionable message.
