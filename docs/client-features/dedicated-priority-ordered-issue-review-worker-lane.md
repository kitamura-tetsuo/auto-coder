# Dedicated priority-ordered Issue Review worker lane

Semantic Issue review executes in a dedicated Review worker lane that consumes
only review-needed work in priority/FIFO order and never acquires
implementation slots, selects providers, creates implementation tasks, or
dispatches implementation. The lane runs on its own validation scheduler
instance, so saturated shared validation capacity cannot starve review
progress; eager family validation scheduling and the invalidation worker also
submit through this independent bound while keeping their existing join and
drain semantics. Immediately before semantic review the lane refreshes the
authoritative snapshot, recomputes admission, exact validation identities,
generation, and priority, and drops superseded work without invoking the
reviewer. Every exact-current identity is decided through the production
specification or decomposition lifecycle, which reuses durable terminal
decisions without a new reviewer-backend invocation only when baseline,
evidence, and Objective-anchor authority is re-established, persists only
valid READY or BLOCKED, keeps ERROR retryable, and never synthesizes READY.
BLOCKED publication effects run with fresh-current authority and idempotent
completion, genuine reissue-required stops are established before handoff,
and completion durably requests routing reevaluation without dispatching
implementation, so a crash between persistence and the wake recovers from
durable state. Implementation-start gates for standalone issues observe these
durable decisions through the lane instead of submitting new validation jobs;
family scheduling keeps its existing join contracts on the independent bound.
A disabled validation category consumes no Review capacity and requires no
fabricated decision, and re-enabling reuses exact-current stored terminals.
