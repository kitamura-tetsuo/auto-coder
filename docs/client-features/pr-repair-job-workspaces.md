# Retained local PR repair workspaces

An admitted local PR CI or merge-conflict job is prepared in a private,
execution-specific Git repository. Preparation reads only the controller
checkout's authorized `origin` URL, fetches the retained head branch and the
exact admitted base commit into private refs, and verifies the admitted H and B
commit identities. The head branch must still resolve to H. The base is fetched
by its recorded SHA: GitHub PR metadata can retain B after the base branch tip
has advanced. Preparation must neither replace B with that tip nor require the
tip to equal B. An unavailable B still fails before provider entry. The adapters
and finalizer retain their strict PR metadata checks, including B and the base
branch identity, before execution and publication.
It never copies the controller checkout's HEAD, index, working
files, or stale remote-tracking refs. Foreign-head inputs and incomplete legacy
admission records fail closed. The workspace binding is persisted before an
adapter may enter a provider.

When a positively unentered PR job returns to pending, its next successful
claim clears the previous workspace binding atomically with the new execution
incarnation. Preparation then creates a fresh private workspace for that
incarnation and revalidates the retained H/B authority. Releasing an unentered
claim atomically records disposal of its producer-bound directory, and the runner
removes it immediately. Startup/hourly cleanup retries interrupted removals even
after a new incarnation replaces the binding. Failed preparations remove their
own partial directories before returning. An unsuccessful competing claim cannot
clear the current binding or authorize its disposal.
Physical workspace identity is the bound directory's inode plus device. Because
block device numbers can be renumbered across reboots or remounts (for example
`259:4` becoming `259:2`) while inodes persist, cleanup accepts a differing
device only when the inode matches and the producer root itself now reports the
same device as the target; a different inode is still treated as a replacement.
`tests/test_pr_repair_workspace.py::test_unentered_cleanup_survives_filesystem_device_renumbering`
and `tests/test_local_job_workspace_cleanup.py::test_finalized_release_survives_filesystem_device_renumbering`
cover this.
Entered jobs cannot be returned to pending or acquire a replacement workspace.
`tests/test_pr_repair_workspace.py::test_unentered_retry_prepares_new_workspace`
covers both repair kinds and ordinary/dead-owner pre-entry release.

The shared producer prepares both repair kinds but does not invoke a model,
form a conflict merge, commit, push, or authorize publication. Conflict
adapters own the real H+B merge snapshot. CI edit results must remain at H;
controller-verified conflict results must expose B as their actual
`MERGE_HEAD`.

After execution, the producer creates one canonical versioned manifest for the
runner's existing immutable result-artifact store. It records the exact job and
execution incarnation, upstream authority, remote/ref/H/B provenance, observed
outcome, output, Git lineage, and complete file identities. File identity
includes contents, executable mode, deletion, and symlink target. Neither
successful prose nor a zero exit status is publication evidence.

Recovery reopens that same artifact and workspace without rerunning the model.
It verifies the artifact checksum, retained admission and execution, current
Git lineage, and every retained file identity. A missing workspace, incomplete
legacy source claim, changed file, changed lineage, unreadable artifact, or
mismatched incarnation remains unverifiable and grants no publication
authority. Temporary protected invocation worktrees remain a separate
isolation boundary inside this job-owned workspace.

`tests/test_pr_repair_workspace.py` covers moving base branches for both CI and
conflict workspaces, unavailable B, and rejected head movement using real Git
repositories. The moving-base case of
`tests/test_z_local_ci_repair_job.py::test_production_runner_composition_discovers_admitted_ci_job`
also drives production admission, adapter preparation, runner provider entry,
and verified result capture for the same retained job when PR metadata still
advertises B.
