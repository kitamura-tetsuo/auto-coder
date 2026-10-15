# Issue-2500 scoped CI maintenance

The version-controlled `.github/ci-maintenance.json` policy is the sole authority
for the temporary PR-lifecycle-refactor pause. Its active state applies only to
same-repository pull requests explicitly associated by a closing reference with
core Issues #2503 through #2506, or to PR numbers deliberately recorded in the
policy. Labels, titles, ordinary mentions, branches, forks, missing evidence, and
malformed policy do not grant the exception. A reference to restoration Issue
#2507 always selects normal CI, including when a core Issue is also referenced.

Paused PR runs retain the `PR Tests` entrypoint, lint/type checking, CLI smoke,
and the `pr_lifecycle_contract` group. The four ordinary coverage contexts and
their aggregate are visibly skipped rather than run and ignored. The selection
step records the repository, PR, head, parent, reason, stopped work, retained
checks, and restoration target in the workflow summary. Normal runs preserve the
ordinary four shards and aggregate unchanged.

While the policy is active on `main`, Publish Beta leaves its test/build/push job
skipped, preserving the deployed channel. Advance Beta validates the jobs of the
exact triggering Publish Beta run and refuses to advance unless that run contains
one completed successful `Build tested artifact` job. Removing the policy later
therefore cannot authorize a delayed paused run.

To restore normal operation, the #2507 PR must first run full normal CI, then set
`active` to false (or remove the policy, selector wiring, retained-only job, and
this temporary guidance) in a deterministic cleanup. Closure of either #2500 or
#2507 is not an activation input. This exception does not modify branch protection,
provider gates, browser/live selection, Auto-Coder CI observation, or release
promotion rules.
