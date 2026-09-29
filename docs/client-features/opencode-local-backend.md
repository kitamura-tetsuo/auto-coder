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

Editable OpenCode tasks run only in a controller-owned private Git repository
through the shared local execution boundary. OpenCode may stage and unstage,
commit and amend, create private branches/refs/stashes/worktrees, and use reset,
restore, merge, rebase, or cherry-pick while implementing the task. Those Git
changes are private implementation state: they are neither rejected nor copied
as publication history. The intended final source files must remain integrated
in the original bound result root, where the generation-specific shared handoff
validates and synchronizes them before Auto-Coder performs its normal commit,
push, and pull-request workflow. A direct editable client call without the
matching controller binding is refused before provider submission.

The Git/GitHub command wrapper and branch/HEAD/ref/index snapshots are installed
only for no-edit execution. OpenCode's checkpoint/session repositories outside
the result repository remain provider runtime state and are not source results.

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
produces a plausible final answer. In addition to the branch/HEAD/refs/index guard, a no-edit call snapshots the complete
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

### Explicit session continuation

After a successful fresh invocation, `get_last_session_id()` returns the
opaque root provider session ID established by that run's own structured
events (never inferred from text, a session listing, a timestamp, or another
invocation); it is left unchanged by a failed or identity-inconsistent
invocation rather than exposing a stale or wrong ID as that invocation's own
result. `continue_session(session_id, prompt, is_noedit=...)` performs one
finite, non-interactive, local continuation of exactly that caller-supplied
session via `opencode run ... --session <id>`, transporting the new rendered
prompt once through stdin followed by EOF; it never uses implicit
`--continue`, `--fork`, or a replacement session, and a caller/configuration
option that would conflict with the exact session, mode, model, directory,
output format, or finite local execution (including a one-time model
override attempted during a continuation) is rejected before the task is
submitted. The requested `is_noedit` is authoritative on every continuation
regardless of the session's own prior mode or permissions: a no-edit
continuation goes through the same freshly generated, randomly named
enforcing agent and preflight described above, independent of whatever mode
originally created the session.

A continuation is reported as resumed only once exit status is zero, no
root-session error occurred, the final assistant message's `step_finish`
reason is `stop`, and the observed root-session identity equals the
requested ID; a missing/changed/absent session ID, an incomplete stream, or
an error anywhere in the stream (even one following an otherwise-plausible
final answer) fails the call instead of promoting a same-looking but
non-continuous result. At the `BackendManager` boundary,
`_last_continue_session_resumed` reflects this per attempt and is reset to
`False` on any failure or on a fallback to a fresh session, another alias,
or another backend/provider — even when that fallback itself returns a
plausible answer under a different session.

**Workspace association.** The real OpenCode CLI ties a continued session's
actual tool execution to the directory it was originally *created* in, not
to the `--dir` given for the continuation call itself (verified directly
against the released CLI, not assumed from its documented flags): it either
continues operating against that original directory if it still exists, or
fails internally once it no longer does. Passing a different `--dir` on
`--session` does not redirect it. Consequently, before submitting a
continuation task, Auto-Coder runs the read-only `opencode session list
--format json` in the current execution directory (itself scoped to the
directory it runs in) and refuses the continuation — before any task is
launched — unless the requested session ID appears in that directory's own
list. This keeps a continuation from ever silently operating against, or
crashing on, a workspace other than the caller's current one: if the
provider session was created in a different temporary worktree that has
since been replaced or removed, replaced, or recreated at the same path, continuation fails closed
before provider submission rather than redirecting, reconstructing a binding,
or silently starting fresh. Controller-retained continuations reuse the exact
original invocation/workspace association, receive a new turn/result generation,
and cannot start until the preceding generation's writers and required handoff
state are settled.

One-shot resume state is always consumed at the start of the next
invocation on the same client instance, whether that invocation succeeds or
fails, so it can never resume an unrelated later call; an ordinary (fresh)
invocation never carries a `--session` flag even if stale session state was
seeded elsewhere (e.g. a prior/unrelated backend rotation).

### Adversarial review and PR-scoped reviewer sessions

OpenCode (`opencode` and named aliases with `backend_type = "opencode"`) is
fully recognized as read-only review-capable
(`is_read_only_review_capable_backend("opencode") is True`) and selectable for
adversarial validation and specification analysis via:
- `[backend_pr_adversarial_validation]` (dedicated PR adversarial validation order)
- `[backend_issue_adversarial_validation]` (dedicated Issue specification and decomposition order)
- `[backend_adversarial_validation]` (generic fallback order)
- Dynamic high-score adversarial review routing

Kind-specific sections take strict precedence over generic routes. When a
kind-specific section is configured in `~/.auto-coder/llm_config.toml`, it is
authoritative: an order that resolves to no candidate or an incapable backend
blocks validation rather than silently falling back to generic
`[backend_adversarial_validation]`.

**Strict no-edit enforcement.** Adversarial review invocations always run with
`is_noedit=True` (`use_noedit_options=True`), enforcing the generated
`autocoder-noedit-*` agent policy that denies all execution, bash, edit, and write
tools while permitting only `read`, `glob`, and `grep`. Preflight verification
(`opencode debug agent <name>`) runs before task launch and must verify this exact
tool denial policy; any preflight failure, missing command, or policy discrepancy
fails closed before the prompt is submitted, with no fallback to editable execution.
Invocation-level `git` and `gh` wrappers block modifying repository subcommands and
deny `gh` completely.

**Normalized assistant delivery.** The adversarial review parser receives only
the normalized final assistant message text from a single consistent root session
where `step_finish` reason is `stop`. Tool calls, reasoning blocks, intermediate
assistant steps, and event stream envelopes are stripped before parsing. Malformed
JSON, missing final text, non-`stop` termination, or contradictory results fail
closed as structured review errors.

**PR-scoped reviewer session persistence.** PR adversarial review maintains
durable reviewer session checkpoints in `ReviewerSessionRegistry` (stored at
`~/.auto-coder/reviewer_sessions.json`), keyed by
`(repository, pr_number, backend_name, backend_type, model_name)`. Reviewer
session state is completely decoupled from implementation task session state:
review managers do not read, write, or alter `backend_session_state.json`.

**Continuation lifecycle and workspace association.**
- **Initial review:** When no session is associated with the PR for the selected
  backend and model identity, Auto-Coder launches a fresh review invocation.
- **Incremental rereview:** When an existing session is associated, Auto-Coder
  attempts an exact continuation (`--session <session_id>`) in the current
  execution directory. Because OpenCode associates sessions with the directory
  where they were created, Auto-Coder preflights continuation using
  `opencode session list --format json` in the current workspace. If the session
  is not present in that workspace list or if continuation fails to resume
  continuously (`_last_continue_session_resumed is False`), Auto-Coder starts a
  fresh review in the current workspace and records a new session checkpoint
  under the PR key (never relabeling or reusing stale history).
- **Multi-step evidence completion:** During multi-step evidence completion
  within an active review pass, continuation must match the exact session ID and
  backend/model identity, with `_last_continue_session_resumed is True`. Any
  session discontinuity or identity mismatch immediately terminates the review
  as an ERROR.
- **Snapshot binding and invalidation:** Review outcomes and session checkpoints
  are bound to an immutable `evidence_validation_snapshot` (PR HEAD SHA and
  normative Issue contract hash). Changes to the PR HEAD or Issue contract
  invalidate applying older cached results, requiring a fresh validation pass.
- **Persistence resilience and cleanup:** If writing to `ReviewerSessionRegistry`
  fails, the unpersisted checkpoint is cleared from the validation result so
  non-durable state is not assumed. When a PR is closed or merged, Auto-Coder
  automatically removes its associated reviewer session entries.
- **Failover:** If an OpenCode review backend fails, failover proceeds strictly
  through configured eligible read-only review backends in priority order. If all
  candidates fail or are exhausted, review fails closed, blocking PR merge.

### Distributed container runtime and operator setup

The distributed Auto-Coder container image includes a pinned OpenCode CLI
release (`1.18.31` on Linux `amd64` and `arm64`) alongside required system
dependencies (`git` and `ca-certificates`). For full architecture and channel
details, see `docs/client-features/opencode-container-runtime.md`.

In `compose.channels.yml`, containers run with `HOME=/runtime/home`. OpenCode
configuration and native data are stored under this effective user home:
- Configuration: `/runtime/home/.config/opencode/opencode.jsonc` (or `.json`)
- Authentication store: `/runtime/home/.local/share/opencode/auth.json`
- Native session database: `/runtime/home/.local/share/opencode/opencode.db`
- Auto-Coder configuration: `/runtime/home/.auto-coder/llm_config.toml`

Channel mounts (`./runtime/release` and `./runtime/beta`) keep release and beta
state isolated across container recreations. Preservation of native session data
does not trigger implicit last-session resumption for new implementation tasks.
Credentials are never baked into image layers or build arguments; runtime
credential injection is performed non-interactively via environment variables
(`OPENCODE_API_KEY`, `OPENAI_API_KEY`, etc.), `auth.json`, or `llm_config.toml`.
Available models may be discovered via `docker compose run --rm release opencode models`,
and aliases may configure any explicit `provider/model` without dependency on
Union Alpha.
