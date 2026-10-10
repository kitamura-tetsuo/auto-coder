# Asynchronous Issue Job Workspaces

Accepted local Issue implementation jobs prepare a persistent, job-owned Git
clone before model entry. The clone is bound to the durable job and execution
incarnation, exact repository, pinned source ref and commit, and intended work
branch. Preparation fails closed if the source ref has advanced or durable job
authority changes, or if the shared checkout is not currently at that exact
commit and branch. While holding the short preparation lease, it snapshots the
accepted checkout's index separately from its tracked, unstaged, untracked, and
ignored working files, preserving distinct staged and working-file versions in
the clone; the lease is released before model entry.

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
