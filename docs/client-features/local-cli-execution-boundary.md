# Local CLI execution boundary

Each Git-backed local backend call has controller-owned execution state bound to
the private repository invocation. The state records the actual backend type,
effective edit mode, private workspace identity, unique turn identity, exact
provider-session association, backend outcome, filesystem and publication
enforcement, writer completion, and violation-observation status. Each
runtime fact starts unknown and must be positively established by an authoritative
producer for the same invocation. It is held outside model output and cannot be
replaced by completion text or an agent-provided flag.

Boundary state is scoped by context to one invocation, so concurrent invocations
cannot settle or invalidate one another. This state abstraction does not itself
confine filesystem or network access and does not prove that a provider's complete
descendant tree stopped; those facts require an enforcing launcher before this
evidence can authorize promotion. Denied policy operations remain recorded and logged as warnings. A denied
operation does not invalidate a successful provider result; completed observation
and positive writer settlement are still required. Enforcement installation or
monitoring failures remain terminal.

An ordinary successful provider return records only successful backend completion.
It remains a usable legacy result, but its enforcement and writer facts stay unknown
and it cannot authorize a certified confined result. The legacy confined-result
query requires positive backend completion, filesystem enforcement, writer
settlement, and violation observation. Default editable handoff instead requires
the exact editable turn, successful backend completion, positive writer settlement,
and completed violation observation; filesystem and publication confinement
certificates are not prerequisites. No-edit evidence can never authorize edit
promotion, and a provider session identifier is metadata—not continuation
admission or permission to reuse a result root.

Codex and OpenCode turns use this boundary at the real command launch. The controller strips
ambient Git target overrides, requires the command cwd to equal the immutable
private result root, installs the Linux filesystem policy, and launches the CLI in
its invocation-owned cgroup. A provider return is successful only after the cgroup
is positively empty and the direct child is reaped. Production deployments provide
the non-root child credentials with `AUTO_CODER_LOCAL_WORKER_UID` and
`AUTO_CODER_LOCAL_WORKER_GID`; missing or unusable cgroup/Landlock prerequisites
cause refusal before the CLI starts rather than an editable fallback.
Codex execution-safety failures preserve the executor's concrete diagnostic
(redacted and bounded to 2,000 characters) in the raised error and interaction
log. Preparation failures such as missing worker credentials therefore remain
visible instead of being described solely as uncertain writer settlement.
An uncertain settlement error is terminal for automatic and explicit session
resume handling: it retains the prior session identity and cannot be converted
into a fresh call or a different-backend replacement.

The checked-in `compose.channels.yml` production profile runs the controller with
host cgroup-v2 access and supplies the image's dedicated UID/GID 65532 worker
identity. Each turn receives a private provider home beneath
`$AUTO_CODER_RUNTIME_ROOT/local-invocations`, with a private temporary directory
and only required provider configuration and authentication files copied into it.
Codex also receives this
runtime for no-edit turns because its CLI initializes writable app-server state
before the review starts. `CODEX_HOME` is rebound to the private home, with its
original `auth.json` and `config.toml` copied there. The filesystem policy permits
writes there while keeping the private repository and caller checkout read-only
for the no-edit turn.
Codex's final-message output is written inside that runtime and copied to the
controller-created private repository file only after the worker has stopped.
Editable OpenCode turns may use ordinary private Git/index,
commit, ref, and worktree operations; the caller checkout remains isolated.

Explicit continuation compatibility failures are not redirected into fresh
invocations. Effective mode is resolved before boundary construction, including
clients constructed for no-edit operation, so controller evidence cannot advertise
editable authority for a read-only invocation.
Local sessions are refused once their invocation-owned private workspace has been
released; an opaque provider session ID is never resumed in a newly cloned root and
reported as a continuation. A future continuation implementation must retain or
capture the prior generation and establish the next caller checkpoint explicitly.

Automatic PR rereview selection checks this retained-workspace prerequisite for
every local adapter before requesting a continuation. A registry entry without
a workspace owned by the current manager starts a fresh read-only review with
the durable finding bundle; it does not attempt and then reinterpret a refused
explicit continuation. Cloud classification is shared with BackendManager.
`test_local_rereview_starts_fresh_when_private_workspace_was_released` covers
Muse, Codex, OpenCode, Claude, Gemini and Qwen. Existing interaction events record
the actual fresh invocation; no continuation is falsely reported as resumed.
`tests/test_adversarial_test_oracle_gaps.py::test_released_local_session_starts_fresh_and_preserves_gap_identity`
also exercises the real backend manager: the fresh reviewer receives the existing
gap identity and can resolve it with current-head evidence, without attempting
the unavailable continuation or switching to a fallback backend.
`tests/test_opencode_review.py` checks the same fresh-review selection through
executable OpenCode fixtures after restarting in a different checkout or loading
a stale provider session, while its evidence-completion continuation failure
remains an error within an active review.

For an existing Docker Compose deployment using an `app` service, merge the
following settings into the final override file (use the actual service name
when it differs):

```yaml
services:
  app:
    user: "0:0"
    environment:
      AUTO_CODER_LOCAL_WORKER_UID: "65532"
      AUTO_CODER_LOCAL_WORKER_GID: "65532"
    privileged: true
    cgroup: host
    volumes:
      - /sys/fs/cgroup:/sys/fs/cgroup:rw
```

Use the production image containing the dedicated worker identity. Apply the
override using the same project name and full existing Compose file list, with
the new override last. Drain active processing before recreating the service;
`docker compose restart` does not apply changed environment or mount settings.
After recreation, verify that the two worker variables are set, the controller
runs as root, and `/sys/fs/cgroup` is mounted read-write. Then reprocess the
deferred PR so its pending strong audit can run. Changing these deployment
settings does not turn an ordinary validation PASS into strong-audit completion.
