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
`~/.auto-coder/pr-repair-job-workspaces/`. Active, paused, failed, interrupted,
cannot-fix, stopped, superseded, ambiguous, replaced, symlinked, unbound legacy,
or incompletely evidenced workspaces remain intentionally available. Filesystem
failures retain a retryable durable state and do not stop independent candidates.

Terminal replay uses durable artifacts and effect receipts and therefore remains
available after deletion without recreating a repository, invoking a provider,
pushing, creating a PR, associating an Issue, or waking CI again. Local-job,
artifact, continuation, ownership, execution, and effect records are retained;
cleanup performs no GitHub mutation and terminates no process.
