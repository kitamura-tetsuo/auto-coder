# Private local Git workspaces

Git-backed local backend invocations run in a fresh, fully independent clone rather
than in the caller checkout or a linked worktree. The controller captures the
caller's repository, Git-directory identities, symbolic or detached HEAD, commit,
index, and file snapshot before launch. That immutable binding is available through
`get_current_local_workspace()` while the invocation runs.

Preparation preserves staged and unstaged tracked changes, files and symlinks,
ignored and untracked source content, modes, and non-disposable empty directories.
Disposable untracked cache/environment directories are omitted, but tracked paths
with those directory names remain present. Preparation fails closed if the source
changes during capture, isolation or seeding fails, or the source has an unborn
HEAD, unmerged index, sparse checkout, or gitlink entries.

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
