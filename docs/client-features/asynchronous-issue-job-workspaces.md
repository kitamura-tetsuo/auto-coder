# Asynchronous Issue Job Workspaces

Accepted local Issue implementation jobs prepare a persistent, job-owned Git
repository before model entry. For every new implementation attempt, including
an explicit retry, preparation reads the controller checkout's `origin` URL,
initializes an empty private Git repository, and fetches the configured
`MAIN_BRANCH` (default `main`) directly from that remote. The fetched commit is
pinned before creating the attempt branch, and the durable job records
`refs/remotes/origin/<MAIN_BRANCH>`, the exact fetched commit, publication remote,
and execution incarnation. Missing origin, failed fetch, or missing remote branch
fails before model entry; there is no fallback to local HEAD or cached refs.

The shared checkout's branch, HEAD, index, working files and remote-tracking refs
remain unchanged. Its unrelated commits, staged edits, unstaged edits, untracked
files and ignored local context are not implementation input. Each retry fetches
again; retained result publication continues to use its already pinned workspace
without another fetch or model invocation.

The long-running invocation operates only in that clone and therefore does not
retain or mutate the controller's shared checkout. Concurrent jobs for the same
repository have different clone paths and branches, so completion and cleanup
order cannot redirect either result.

A successful result checkpoint records the source and branch identities plus a
filesystem-derived manifest of every actual changed, added, or deleted file.
This capture does not trust index flags such as `assume-unchanged`. Model-created
commits or HEAD changes are not accepted as promotion authority. Only a single,
non-empty `ACTION_SUMMARY:` response confirms success; inability, failure,
ambiguous, interrupted, or unconfirmed responses remain without a completed
result while their bound workspace stays available for diagnosis and recovery.
Workspaces are deliberately not deleted by the producer because later
publication and retirement stages own consumption and cleanup.

Regression coverage: `tests/test_issue_job_workspace.py`.

Result snapshots omit untracked disposable cache, environment and test-output
paths using the same directory-name policy as private local backend workspaces.
Tracked files under those directories remain preserved and verified. Generated
cache writes therefore do not enter publication manifests.

Publication reuses the producer's file-identity scan and stages only tracked and
non-ignored, non-disposable untracked paths using literal Git pathspecs. Cache-only
activity is no-change; generated caches appearing during or after implementation
neither block verified publication nor enter its commit. Other post-checkpoint
source changes still refuse publication. Coverage includes
`tests/test_issue_job_finalizer.py` and
`tests/test_explicit_local_issue_completion.py`.

Tracked paths are staged with `git add -u`; eligible new source paths are staged
separately. A later ignore rule covering an already tracked directory therefore
does not block publication or require forced staging. Ignored untracked runtime
output stays excluded. The real-Git regression
`test_tracked_ignored_context_is_published_without_ignored_runtime_output` in
`tests/test_issue_job_finalizer.py` checks the confirmed remote commit.
