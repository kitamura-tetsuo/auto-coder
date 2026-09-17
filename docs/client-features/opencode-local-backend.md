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
similar model-specific option) is still honored. No-edit (read-only)
execution is described separately below.

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

### No-edit (read-only) execution

A no-edit call (`_run_llm_cli(prompt, is_noedit=True)`) runs OpenCode through
a freshly generated, randomly named agent (`autocoder-noedit-<random>`)
defined entirely through the `OPENCODE_CONFIG_CONTENT` environment variable
for that one invocation only — nothing is written into the repository or any
persistent OpenCode configuration/authentication store. That agent's
permission map denies every tool by default (`"*": "deny"`) and allows only
`read`, `glob`, and `grep`; because the agent name is generated fresh per
call, no pre-existing global, project, `.opencode/`, or agent-level
configuration can already define (and thereby weaken) it, and this holds
regardless of how permissive those other layers are. Before the task is
submitted, Auto-Coder runs `opencode debug agent <name>` and requires the
resolved tool policy to show every non-inspection tool (`bash`, `edit`,
`write`, `task`, `webfetch`, `skill`, `todowrite`) denied and `read`/`glob`/
`grep` allowed; if that cannot be established — a missing/incompatible CLI,
an unparseable response, or a policy that doesn't match — the call fails
before the task launches rather than falling back to an editable run.
`--agent` is reserved to Auto-Coder for a no-edit call (a configured or
one-time `--agent` override is rejected before launch), and `options_for_noedit`
is honored the same way `options` is for an edit call.

During the run, any `tool_use` event naming a tool outside `read`/`glob`/
`grep` — whether OpenCode reports it denied or, contrary to that policy,
lets it through — rejects the whole result, even if a later step still
produces a plausible final answer. Beyond the branch/HEAD/refs/index guard
shared with edit mode, a no-edit call additionally snapshots the complete
working tree (tracked file contents and modes, the staged and unstaged
diff, untracked and ignored file contents, and directory modes, excluding
`.git` and conventional disposable caches) before running and compares it
afterward; any difference restores the pre-run state and fails the
invocation. A no-edit result is never promoted or published; it is returned
directly to the caller (e.g. adversarial review) as read-only output.

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
