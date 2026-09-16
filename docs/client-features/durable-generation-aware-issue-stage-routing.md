# Durable generation-aware Issue-stage routing

Authoritative Issue classification can be persisted independently for Review
and Implementation lanes without introducing another GitHub readiness signal.
Each lane stores one coalesced item per target, ordered by current Issue priority
and a durable FIFO arrival that survives restart and in-place reprioritization.
Semantic supersession or loss of stage eligibility removes the old item; later
admission receives a new arrival. Review generations bind enabled exact
validation identities but not their verdicts, while Implementation generations
bind only the target/family contract snapshot and therefore ignore validation
policy, labels, operational state, and review outcomes.

Review routing omits exact-current terminal READY/BLOCKED decisions and
coalesces in-flight identities. Implementation routing requires READY for every
enabled exact-current identity. A durable owned-start tombstone prevents an
Implementation generation from being started again after restart, readiness
withdrawal/restoration, duplicate wakeups, or exact semantic reversion; a crash
before ownership returns the same durable arrival to pending work. This routing
foundation performs no Review execution or implementation dispatch itself.
The durable invalidation worker records routing before invoking the legacy
validation boundary, then refreshes it afterward even when validation returns
ERROR, so partial terminal results shrink rather than erase retryable Review
work. After ordinary Issue processing, routing performs another strict refresh
so a standalone READY/BLOCKED decision is handed off immediately rather than
waiting for another webhook or restart. Family-tagged Implementation rows allow a reconciled parent membership
change to retire departed children. Startup re-authorizes every durable lane
target, including targets absent from the open-Issue enumeration, so closure
removes pending work and unchanged reopening receives a new arrival.
An empty authoritative child set also retires every Implementation row owned by
the former family before the former parent is reclassified as standalone.
Engine-owned validation lifecycles are rebound when the effective provider/model
route changes. Authoritative routing therefore requires decisions for the new
policy identity, while an exact route restoration can reuse its earlier durable
terminal decisions.
