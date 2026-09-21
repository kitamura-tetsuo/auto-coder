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

The guard is an adapter-stage production boundary and does not yet select
candidates or replace ordinary Issue dispatch. Provider response classification
and integration into Issue processing belong to the subsequent adapter stage.

