# Asynchronous Issue Job Workspaces

Accepted local Issue implementation jobs prepare a persistent, job-owned Git
clone before model entry. The clone is bound to the durable job and execution
incarnation, exact repository, pinned source ref and commit, and intended work
branch. Preparation fails closed if the source ref has advanced or durable job
authority changes.

The long-running invocation operates only in that clone and therefore does not
retain or mutate the controller's shared checkout. Concurrent jobs for the same
repository have different clone paths and branches, so completion and cleanup
order cannot redirect either result.

A successful result checkpoint records the source and branch identities plus
hashes of the actual tracked diff and untracked files. Model-created commits or
HEAD changes are not accepted as promotion authority. Failed, interrupted, or
unconfirmed calls remain running with their bound workspace available for
diagnosis and recovery; only a confirmed result enters the downstream state.
Workspaces are deliberately not deleted by the producer because later
publication and retirement stages own consumption and cleanup.

Regression coverage: `tests/test_issue_job_workspace.py`.
