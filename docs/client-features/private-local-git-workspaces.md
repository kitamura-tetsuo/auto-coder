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

Editable results are computed from the invocation's captured working-file baseline
to the bound private root's final working-file state. Private commits, branch names,
staging, and clean status do not control the delta. Before applying it, the
controller revalidates the caller HEAD, index, and supported files under a
per-target writer lock; stale targets, unsafe paths, symlink ancestors, or partial
application failures are refused without overwriting caller work. Failed and
no-edit turns never enter this handoff.
