# Retained local PR repair workspaces

An admitted local PR CI or merge-conflict job is prepared in a private,
execution-specific Git repository. Preparation reads only the controller
checkout's authorized `origin` URL, fetches the retained head and base refs
into private refs, and requires those refs still to resolve to the admitted H
and B commits. It never copies the controller checkout's HEAD, index, working
files, or stale remote-tracking refs. Foreign-head inputs and incomplete legacy
admission records fail closed. The workspace binding is persisted before an
adapter may enter a provider.

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
