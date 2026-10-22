# Durable local PR repair lifecycle

Eligible local CI-failure and merge-conflict repairs use the same durable
production path for ordinary and explicit PR processing. The selected route
persists an exact head/base claim, a repository-scoped implementation execution,
and a local job before the PR worker returns or invokes an editor.

The local runner prepares a private exact-source workspace and checkpoints
provider entry before editing. Its immutable result manifest binds the job,
execution incarnation, source commits, workspace file identities, model output,
and observed backend invocation. A restarted controller may consume that
artifact, but may not infer provider entry or start another editor when entry is
indeterminate.

`PRRepairJobFinalizer` independently revalidates the artifact and current PR
target, creates the candidate commit, publishes only with an exact remote-head
lease, observes the resulting remote head, records the push receipt, and queues
fresh-head processing. Publication is not CI, review, or merge success. Those
decisions continue through the normal current-head gates.

Explicit processing waits for its target-scoped runner and finalizer. On a
confirmed publication it reports an applied repair with the exact resulting
head while still returning a deferred processing outcome because fresh CI and
independent review/merge validation remain outstanding. Worker-thread editing
evidence is projected to the explicit result only after the retained manifest
has been verified for that same job and execution.

## Recovery limits

An entered provider without a confirmed resumable session remains pending for
operator reconciliation. A retained result, commit intent, or push intent can
be reconciled from exact workspace and remote observations without invoking the
model again. Unknown remote state never authorizes a blind replacement push,
and a stale head/base makes the retained repair non-publishing.

The controlled regression inventory uses temporary Git repositories and SQLite
stores; it is not live GitHub/provider evidence. Credentialed smoke remains
opt-in and restricted to the operator-designated disposable profile. No such
profile or positive live run IDs were available in this implementation
environment, so live verification remains externally blocked rather than being
substituted with controlled tests.
