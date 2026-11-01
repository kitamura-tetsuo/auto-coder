# Local job workspace retention

Successfully published local Issue implementations and PR repairs retain their
incarnation-specific repositories for 86,400 seconds after the first durable
positive release. Set `AUTO_CODER_JOB_WORKSPACE_RETENTION_SECONDS` to a finite,
nonnegative number of seconds to change the interval; `0` enables immediate
eligibility. Invalid values produce a warning and use the default.

The daemon scans after startup local-job reconciliation and during its hourly
maintenance cycle. It does not infer success from age or job settlement. Before
removing a workspace it verifies the exact job, execution incarnation,
repository, result artifact, publication receipts, path, filesystem identity,
and writer settlement. The release time and `RELEASED`, `DELETING`, or `DELETED`
phase live in the local-job SQLite store outside the workspace, so restarts do
not reset retention and interrupted deletion can resume.

Reclamation removes only the producer-owned execution-incarnation directory
under `~/.auto-coder/issue-job-workspaces/` or
`~/.auto-coder/pr-repair-job-workspaces/`. Failed and interrupted executions
are eligible when the exact negative result and writer settlement are confirmed;
an explicitly superseded Issue execution is eligible with its exact durable retry
receipt and settled writer. In either case,
their source commits and invocation input remain independently available, and a
producer-created ownership marker is still linked to its external anchor.
Muse-backed CI and conflict repair adapters retain their provider workspaces
under the job and bind the actual session and process lifetime to that job.
Cleanup also requires a settled, correlated provider observation without pending
tasks or turns. Multiple fresh provider sessions in one PR repair are marked
ambiguous and retained rather than treating the last host as the only writer.
Configured credential and runtime roots, including the OpenCode provider-runtime
layout, are protected. Active, paused, cannot-fix, stopped,
ambiguous, replaced, symlinked, unbound legacy, or incompletely evidenced
workspaces remain intentionally available. Filesystem
failures retain a retryable durable state and do not stop independent candidates.

Terminal replay uses durable artifacts and effect receipts and therefore remains
available after deletion without recreating a repository, invoking a provider,
pushing, creating a PR, associating an Issue, or waking CI again. Local-job,
artifact, continuation, ownership, execution, and effect records are retained;
cleanup performs no GitHub mutation and terminates no process.

New producer roots are published with an atomic no-replace rename after the
ownership marker and its external hard-link anchor have been persisted. On
filesystems without this identity mechanism, work may continue but the new
negative-result cleanup cannot establish ownership. During negative-result
deletion the marker remains until the other contents have been removed. If a
crash leaves a directory after its marker is removed, its identity is unverified
and it remains for manual inspection rather than being adopted on restart.

PR CI/conflict incarnations positively released before provider entry are
disposable immediately, without the successful-publication retention interval.
The pending transition and disposal receipt are committed together, preserving
the old path and filesystem identity when a retry clears the workspace binding.
The runner attempts removal as soon as its adapter returns; startup/hourly
maintenance retries durable disposal receipts after failures or restarts. A
retained result artifact (including a controller-only clean merge) or provider
continuation prevents pre-entry release. Disposal checks the producer root,
exact incarnation, physical directory identity, and absence of unattributed
sibling directories; replaced or symlinked paths remain protected. Failed Git
preparations also remove the partial directory created by that call. Already
removed released directories can be marked deleted, including after manual
removal, without recreating them.
