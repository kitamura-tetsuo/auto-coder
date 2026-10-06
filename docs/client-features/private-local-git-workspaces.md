# Private local Git workspaces

Git-backed local backend invocations run in a fresh, fully independent clone rather
than in the caller checkout or a linked worktree. The controller captures the
caller's repository, Git-directory identities, symbolic or detached HEAD, commit,
index, and file snapshot before launch. That immutable binding is available through
`get_current_local_workspace()` while the invocation runs.

Preparation preserves staged and unstaged tracked changes, files and symlinks,
ignored and untracked source content, modes, and non-disposable empty directories.
Disposable untracked cache/environment directories are omitted, but tracked paths
with those directory names remain present. The omitted names include `coverage`,
`coverage-backups`, `htmlcov`, `playwright-report`, and `test-results`, at any
depth, so prior test reports are not duplicated into both the source snapshot
and the clone. Other ignored source/context files remain preserved. Preparation fails closed if the source
changes during capture, isolation or seeding fails, or the source has an unborn
HEAD, unmerged index, sparse checkout, or gitlink entries.

For local merge-conflict repair, the controller first stages the merge snapshot
to remove unmerged index entries while preserving conflict-marker file contents.
The independent clone receives those contents, without the caller's active merge
metadata. The controller checks markers after handoff before committing the merge;
staging alone never proves resolution. See `merge-conflict-handling.md`.

The clone does not use hard-linked objects or alternates. Its refs, index,
configuration, objects, and worktree registrations are therefore private to one
invocation. Cleanup removes only that invocation's temporary clone after execution
and edit handoff have finished; non-Git operations continue to use their original
working directory.

After creating a new implementation clone, the backend manager runs
`bash scripts/test.sh` in that clone before launching the LLM. It uses the target
repository's startup-validated `TEST_SCRIPT_PATH`, with no additional existence
probe or fallback runner. This controller operation runs outside the provider
sandbox and prevents container redirection into a different checkout. The script
owns dependency preparation; running it does not guarantee that every target's
script installs missing dependencies.

Ordinary test failures remain a failed baseline rather than preventing a task
whose purpose may be to fix those failures. Exit codes indicating launch failure
or timeout refuse provider submission. Sanitized output is saved in the private
`.agent-tmp/initial-tests.log`, which is excluded from result handoff. Read-only
reviews and reuse of a retained implementation clone do not run this preparation.

A controller may designate an invocation as PR CI-failure repair with
`invocation_admission.bind_ci_repair_designation()`. Within that block (including
automatic backend/provider fallback), a fresh editable clone does not run the
unscoped test script. The omission is reported as a `local.workspace-tests` result
with outcome `skipped`, `baseline: not_run`, and `reason: ci_repair_policy` for the
affected invocation; no baseline log is written and nothing is claimed about
dependency preparation or verification. The designation is an explicit per-context
input (never inferred from prompts, branch names, or providers), is reset when the
block exits for any reason, and does not affect concurrent or later invocations.
Private workspace creation, ownership, and stale-caller handoff checks are unchanged.
The `local.workspace-tests` trace stage records start, result, invocation identity,
and exit code; its completion proves only initial-test success, never successful
implementation or publication.

Editable results are computed from the invocation's captured working-file baseline
to the bound private root's final working-file state. Private commits, branch names,
staging, and clean status do not control the delta. Before applying it, the
controller revalidates the caller Git-directory/common-directory identity, HEAD,
index, and supported files under a
per-target writer lock; stale targets, unsafe paths, symlink ancestors, or partial
application failures are refused without overwriting caller work. Failed and
no-edit turns never enter this handoff.

After result handoff, retained-root checkpoint advancement compares the same source
path scope as handoff: tracked files, non-ignored non-disposable untracked files,
and the preserved source baseline. The comparison uses the union of caller and
private source paths, so a new file tracked only in the private index or a baseline
file covered by a new ignore rule remains validated. Newly generated ignored build
outputs and logs remain private runtime context and do not block successful
handoff or a later retained review. They are not added to the next source baseline.
The caller's complete context checkpoint still includes non-disposable ignored
files, so concurrent caller changes continue to invalidate stale result handoff.

Workspace preparation keeps file contents and binary staged/unstaged patches on
invocation-owned temporary disk rather than retaining them as Python byte arrays.
Regular-file and index checksums use bounded streaming reads; baseline and final
file comparisons retain only hashes, modes, and symlink targets. Large ignored
runtime files remain preserved context and still invalidate stale results when
changed. Handoff copies changed files in bounded chunks and saves rollback copies
on temporary disk before applying any change. Temporary snapshots share the
private clone's ownership and cleanup lifecycle; retained sessions keep their
snapshots until all owners release them. Disk capacity must accommodate the clone,
captured context/patches, and changed-file rollback copies. Before copying context,
preparation estimates two copies of untracked context, three copies of tracked
working files, and the common Git directory, plus a 1 GiB free-space reserve on
the temporary filesystem. Insufficient space refuses preparation before provider
submission and removes the partial temporary directory. This is a conservative
preflight, not a filesystem quota: concurrent writes and new provider/test output
can still consume additional space. It also refuses new copies when orphaned
workspaces from a hard crash have consumed the available capacity; it does not
delete other processes' roots or legacy directories based on age or names.

The handoff lease is released when its context exits, including failure or
interruption. A direct context with no external execution owner also releases
execution on exit. BackendManager releases a failed execution only before provider
submission or with positive writer-settlement evidence. Uncertain descendant
writers keep their execution lease; cleanup never assumes an exception stopped
them. Session retention is bounded by the manager lifecycle described in
[generation-safe local session continuation](generation-safe-local-session-continuation.md).

Capacity refusal occurs at the existing preparation boundary, before
`local.workspace-tests` or `llm.local-execution` dispatch. Session disposal produces
no provider invocation or successful implementation evidence. Existing interaction
completion and fresh/continuation identity emissions remain authoritative; no
structured trace schema or dashboard rendering changes are needed. See runnable
production-to-view checks in `docs/dashboard-observability.md`. Real Git regressions
in `tests/test_worktree_isolation.py` cover report omission with tracked-source
preservation, capacity refusal, failed-context cleanup, bounded memory, ignored
context staleness, and rollback.
