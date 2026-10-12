# Worker-independent local Issue implementation

Eligible synchronous local Issue implementations are transferred from the
ordinary dispatch claim into `LocalJobStore` before the editing backend is
entered. The dispatch result is `local_accepted`, which means durable,
in-progress ownership rather than completed implementation. The Issue worker
can acknowledge its invalidation and serve another candidate while the local
runner retains the Issue attempt and its logical implementation slot.
Daemon invalidations and explicit `process-issues --only` dispatch lazily open
the same durable authority, so an explicit route does not fall back to a
synchronous editing call merely because `start_automation` was not entered.

The daemon polls the durable store on a runner-owned executor lane.
The lane is explicitly bound to the daemon's repository; it cannot claim,
recover, or notify jobs belonging to another repository in the shared store.
Immediately before provider entry, the Issue adapter strictly refetches the open Issue,
reapplies the normal readiness, family, specification, dependency and author
authority path, verifies the exact dispatch incarnation and attempt, and
requires the original implementation owner to remain active. Refusal returns
an unentered claim to pending; it never falls through to another provider.
Authorization also recomputes the current contract generation against the
generation retained by the exact active execution. Workspace preparation does
not count as provider entry: the adapter repeats this authorization immediately
before the editing call and only then checkpoints `provider_entered`.
Accepted work executes in its job-owned private clone. Its immutable result
manifest is checkpointed by the runner and routed to `IssueJobFinalizer`, which
owns commit, push, Issue-to-PR association and settlement. Restart discovery
uses the same job identity and does not require a new GitHub webhook.
Private roots are selected through the command-execution context rather than
process-wide `chdir`, so separate runner slots may enter their backends
concurrently. Backend construction remains a synchronous pre-handoff boundary:
a positively unavailable CLI or other confirmed-not-started preparation
releases the dispatch claim and advances the existing ranked fallback policy;
only successful preparation may create the durable local job.

The outer Issue dispatch deliberately does not finish an execution whose result
is `local_accepted`. Active Workers therefore reports the released queue worker,
while implementation-slot and durable-job projections continue to report the
in-flight implementation until finalization settles it.

Regression coverage includes the durable dispatch boundary in
`tests/test_cloud_backend.py` and the store, runner, workspace and finalizer
suites.

Explicit `process-issues --only` commands own a target-scoped runner after
handoff and wait through execution and publication. They reuse the same adapter,
private workspace, and finalizer as the daemon. The daemon remains nonblocking;
only the explicit command waits, and it never drains other accepted jobs.

The result producer unwraps Codex, Claude, and Muse CLI event streams using the
same final-response parsers as adversarial validation. It validates the actual
assistant result rather than treating JSON event lines as implementation prose.
Malformed/failed envelopes and missing responses cannot authorize publication.
`ACTION_SUMMARY:` remains the required single-line successful response; an exact
`CANNOT_FIX` settles as cannot-fix without commit, push, or PR creation. Coverage
is in `tests/test_explicit_local_issue_completion.py` and
`tests/test_issue_job_workspace.py`. Repeated explicit commands observe settled
results or resume missing publication effects without invoking the model again.

Every new job workspace, including a retry, fetches the configured base branch
from `origin` directly into its own repository. The adapter does not select the
controller's current HEAD, even when it remains on an unrelated branch or is
detached. The fetched commit and remote-tracking ref become the durable source
identity. The shared checkout's index and working files are not copied.
Preparation failure cannot invoke the model or publish a PR. Finalization releases
only its transferred execution ID even when preparation failed before workspace
ownership binding; a replacement execution remains active. Real-Git regressions
cover stale local refs, remote advancement, retry attempts, dirty shared files,
and nested BackendManager workspaces in `tests/test_issue_job_workspace.py` and
`tests/test_explicit_local_issue_completion.py`.

Durable local dispatch renders `issue.local_implementation`, retaining the full
current Issue body and parent scope context. This editing-only template requires
one verified `ACTION_SUMMARY:` line or `CANNOT_FIX` and delegates commit, push,
PR creation and CI follow-up to Auto-Coder. Cloud lifecycle templates and
label-selected cloud action templates cannot replace this local execution
contract; labels remain supplied as context. The production dispatch regression
in `tests/test_cloud_backend.py` verifies the actual stored invocation input,
including mapped labels and Requirements beyond the former body truncation.
Unconfirmed final prose remains a failure, with a bounded redacted response
retained in the failure diagnostic for investigation.
