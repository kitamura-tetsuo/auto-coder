# Muse Code local backend

Meta Muse Code is available as the `muse` backend and as named aliases with
`backend_type = "muse"`. Install and authenticate the `muse` CLI (or provide
`MUSE_API_KEY`) before startup. Auto-Coder starts a separately owned
`muse serve` MSP host for each invocation and submits the complete rendered
task as one typed text part over the protocol.
Muse may inspect files, edit the working tree, run tests, and use local Git
operations inside its controller-owned private repository. Staging, commits,
branches, refs, stashes, merges, rebases, cherry-picks, and private subagent
worktrees are retained as implementation state; Auto-Coder still owns result
handoff and pull-request publication. Any repository mutation during no-edit
execution fails the run. Muse Code is also capable of serving as a
read-only review and adversarial validation backend (`[backend_adversarial_validation]`);
in no-edit mode, Auto-Coder starts Muse with `--disable-write` and permits shell
inspection, then establishes `onRequest` approval mode over MSP
before submitting the turn. Dangerous bypass and unauthorized workspace-trust
options fail closed rather than being stripped or passed to the host.

No-edit mode does not implicitly add `--disable-shell`. Read-only commands such
as `git diff`, `git log`, `git ls-files`, directory listings, and text searches
are available subject to the host's permission rules. An explicitly configured
`--disable-shell` remains respected. `GIT_OPTIONAL_LOCKS=0` prevents optional
index refreshes during inspection. Shell commands must not modify repository
files or Git state; formatters, dependency installers, and checks that write
artifacts are outside no-edit authority. The existing repository snapshot,
Git-command trace, restoration, and process-group settlement still reject a
mutating no-edit result, including transient Git lifecycle commands. This is
mutation detection and recovery, not filesystem confinement for arbitrary shell
commands outside the captured repository.

The execution prompt directs shell inspection to use simple read-only commands
with literal arguments and to read exit codes from tool-result metadata. Diagnostic
echo commands, shell-variable expansion (including `$?`), and command substitution
can cause the host to request interactive approval even during harmless inspection;
additional evidence should be gathered through separate inspection calls. This is
prompt guidance, not a guarantee of model compliance or a permission-rule change.
Approval refusal, trace emissions, and dashboard projection remain unchanged.

Editable implementation sessions explicitly request and confirm the official MSP
`allowAll` mode before submitting a turn, so a host configured for interactive
approval cannot defer an unattended issue merely because a tool needs approval.
No-edit invocations request and confirm `onRequest`, which permits the host
read-only shell allowlist and sandbox-contained non-dangerous parsed stages.
Unlike `denyUnmatched`, it does not deny every shell stage lacking a saved rule.
No-edit authority still prohibits mutation, with detection and restoration as
described above. Approval requests under `onRequest` fail promptly without
sending a decision. `MUSE_DISABLE_APPROVAL_JUDGE=1` prevents an automatic
reviewer from granting approval during no-edit execution. Explicit `--disable-approval` instead selects `denyUnmatched`. Fresh sessions must echo the requested mode; absent or mismatched
metadata fails before the turn. Exact-session resume selects the mode for the
current invocation through `session/setApprovalMode` when it differs from the
stored mode, and verifies the correlated completed/noop acknowledgement and
effective mode before submitting work. A retained session does not carry a prior
invocation's restrictions into an editable implementation or vice versa.

These modes are host-defined; Auto-Coder does not send individual approval decisions
or modify permission rules. Host sandbox restrictions and permission ceilings still
apply, including network access restrictions. Dependencies should be prepared by
the controller's initial `scripts/test.sh` execution or by the target repository's
setup procedure before invoking the provider. An unexpected interactive request
under `allowAll` still fails promptly rather than being blindly approved.
The protocol modes are described in the
[official Muse approval guide](https://meta-models.github.io/muse-code-sdk/next/guides/msp-concepts/approvals/).

### Host-resolved approval settlement

When the current session has confirmed effective `denyUnmatched` before its turn
(from the start/resume result or an accepted `session/setApprovalMode` echo), the
host may emit an `approval/request` RPC and then resolve it by policy. Auto-Coder
treats `approval/request`, `approval/requested` and `approval/updated` as pending
observations. A valid `approval/request` receives the empty `{}` result as its
presentation receipt (typed server id echoed, never matched against client
request ids); this is not a decision. Auto-Coder never sends `approval/decide`,
edits rules or mode, or acknowledges unknown server methods.

An approval is settled only by a matching `approval/resolved` notification
(session, turn, approval id, `policyResult` exactly `deny` or `allow`). Traffic
before the `turn/start` acknowledgement is retained and checked against the
admitted turn; `approval/updated` is attached by session/approval id without a
turn id. Each approval has a five-second monotonic budget from its first
observation (capped by the invocation deadline) that duplicates, updates and
unrelated traffic cannot renew; expiry starts the normal failure cleanup. Terminal
observations persist against delayed duplicates, conflicting results fail, and
resolutions for another session, turn or approval are ignored. Observation state
is fresh per invocation.

The invocation succeeds only through the correlated `turn/completed` with final
assistant text; a completed turn with an unresolved approval fails and a later
resolution cannot rescue it. A resolved approval emits no
`llm.muse-interactive-request` stage.

Approval traffic without confirmed denial, all `userInput/request(ed)` messages,
unsupported server requests and resume with `pendingRequests` fail promptly.
A known `approval/request` still receives its receipt first; unsupported methods
receive `-32601`. The
`llm.muse-interactive-request` stage reports a blocked outcome, the requested and
observed effective policy (`unknown` when absent), and bounded correlation
identifiers (session, turn, approval, request, plus a distinct method such as
`approval/settlement-expired` for an unresolved approval), without copying shell
commands, prompt text, or approval subjects. The owned host and its process group
are settled by the existing failure path.

Positive process-group settlement records writer-completion evidence even when
the protocol, invariant check, or host exit fails. A failed result remains failed
and cannot authorize handoff, but its settled execution lease can be released so
the private snapshot and clone do not accumulate. Settlement failure remains
unknown and preserves the root. `tests/test_muse_msp.py::test_muse_failed_invocation_releases_settled_workspace`
checks a real nonzero host exit and absence of provider dispatch on preparation
failure; dispatch/result trace outcomes and dashboard error pairing are unchanged.

No-edit validation and recovery remain bound to the captured worktree and
worktree-private Git directory. Its snapshot and Git trace detect both final and
transient mutation, and recovery never replays unrelated or newer refs. Editable
turns intentionally do not run this Git-mutation rejection or snapshot recovery;
their exact current generation is instead validated and delivered through the
shared local-result handoff.

Editable turns also omit creation of the unused no-edit recovery snapshot. Large
ignored or untracked runtime context is preserved by the shared private-workspace
preparation without being loaded again into Muse's Python memory before submission.
No-edit turns still capture and enforce their recovery snapshot and Git trace.
The omission changes no admission, approval, provider-routing, handoff, or trace
outcome; `test_muse_editable_large_context_does_not_allocate_recovery_snapshot` in
`tests/test_muse_msp.py` covers the real protocol-host path with large context.
