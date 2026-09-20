# Production speculative Jules competitions

Auto-Coder can opt newly admitted Jules Issue implementations into one durable
competition. Configure the effective repository configuration with:

```toml
[jules]
speculative_parallelism = 3
```

This starts three distinct competitors for **one** Issue implementation and one
logical implementation owner. The default is `1`, which preserves the legacy
singleton route. Values must be positive TOML integers; booleans, zero,
negative values, strings, and fractional values fail configuration loading
before provider dispatch. Repository-specific configuration overrides the
global value.

For widths above one, Auto-Coder persists the generation, immutable candidate
set, Issue oracle, source branch, initial-PR timeout, and PR-CI timeout before
the first provider request. Repeated admission resumes that active generation;
live configuration changes do not resize it. Candidate submission claims and
accepted/unknown/rejected outcomes remain durable, and accepted sessions are
attached to the same implementation slot. A partial launch never causes a
replacement candidate to be invented.

Known candidate sessions are refreshed directly from their durable identities
at daemon startup and on the 60-second maintenance cadence. The same consumer
delivers durable adoption and aggregate-failure obligations into the normal PR
or Issue invalidation lanes and attempts retained loser cleanup. This targeted
work does not call the account-wide Jules session listing, whose existing
no-startup-list and hourly-attempt cadence remains unchanged.

The existing `[jules].issue_pr_timeout_hours` setting (default 12 hours) is the
initial-PR policy captured by a competition. It is distinct from
`[jules].wait_timeout_hours`, which remains the two-hour repair-wait setting.
