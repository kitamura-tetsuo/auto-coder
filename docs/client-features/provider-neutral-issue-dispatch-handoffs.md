# Provider-neutral Issue dispatch handoffs

`IssueDispatchGuard` is the durable admission authority for one logical Issue
implementation attempt. Its identity consists only of repository owner/name,
Issue number, and the caller-supplied implementation-attempt identifier.
Backend configuration names and resolved providers describe candidate handoffs;
changing either does not create another logical attempt.

The guard writes a unique claim incarnation to SQLite before authorizing a
remote callback. Pending, accepted, indeterminate, failed, and deferred claims
suppress every other candidate, including local candidates and candidates for a
different cloud provider. Only an adapter-classified `NOT_STARTED` observation
durably releases the current incarnation. Exceptions, missing responses,
missing tasks, and absent PRs remain indeterminate. Late finalization from a
released incarnation cannot alter its successor.

Consumers receive structured `DispatchResult` values rather than parsing action
strings. Results retain the logical identity, backend, provider, provider task
or session reference, diagnostic, claim incarnation, and secondary-tracking
completeness. `LOCAL_COMPLETED` means only that the synchronous invocation
finished; it does not claim merge or requirement satisfaction. Remote acceptance
requires a provider reference. If secondary tracking fails, accepted ownership
and its incomplete-tracking state remain durable and suppress replacement work.

CloudRun and `cloud.csv` are migration inputs. Matching CloudRun records become
suppressing attempt ownership. Pending records remain uncertain, contradictory
run/binding references defer, and an attempt-unassociated `cloud.csv` binding
blocks automatic dispatch for that Issue. A caller may explicitly authorize a
separate new attempt; the old binding is retained in the legacy ownership table
and remains inspectable rather than being assigned to the new attempt.

`dispatch_candidates` consumes a caller-ranked sequence without partitioning it
by execution mode. Local and remote adapters acquire the same claim, each unique
candidate is attempted at most once, and only a confirmed `NOT_STARTED` result
releases ownership and advances the sequence. Empty or fully rejected sequences
return an explicit deferred result rather than silently selecting a default.

Provider clients must supply genuine provider references. In particular, a
successful Claude Routine HTTP status without a usable session reference is an
indeterminate send; no locally generated session identifier is persisted. The
legacy cloud selector likewise advances only for explicit pre-submission quota
or not-started rejections. General post-boundary failures stop the pass.

Both ordinary engine routes now resolve the aliases supplied by their existing
public local or cloud selector and pass that single ranked sequence to the same
boundary. The selected local alias is instantiated as a one-backend manager, so
its invocation cannot rotate to another configuration. The engine retains the
structured result alongside presentation actions and distinguishes synchronous
local completion from a provider-accepted remote handoff. Difficult/high-score
and explicitly owned retry routes remain on their dedicated continuations.

Jules aliases construct their transport from the selected alias rather than the
default `jules` configuration. Jules and Claude Routine publish their genuine
provider reference to the dispatch adapter before secondary CloudManager
tracking; a binding failure therefore remains `REMOTE_ACCEPTED` with
`tracking_complete=false` across restart. Local workflow exceptions propagate
back to the adapter, which retains workspace side effects and records an
indeterminate result instead of `LOCAL_COMPLETED` or launching a fallback.
