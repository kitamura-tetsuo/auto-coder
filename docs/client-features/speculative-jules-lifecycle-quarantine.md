# Speculative Jules lifecycle quarantine

Pull requests durably associated with a Jules competition are classified before
ordinary PR effects. Selected results may continue through normal processing;
active unselected, uncertain, and conflicting artifacts are deferred, while
verified retired artifacts are fenced and recorded for durable cleanup. A force
target does not bypass this authority check.
Classification searches the durable competition namespaces rather than relying
on editable PR text, so late results with no Issue link and PRs containing several
Issue references cannot bypass or ambiguously select the fence.

Cleanup is an independent replayable operation. It rechecks authoritative
candidate membership immediately before closing a PR, confirms the resulting
GitHub state before acknowledging the obligation, and leaves denied, ambiguous,
or unconfirmed requests pending across restart. Already closed or merged PRs need
no further close. Cleanup never mutates the source Issue, advances attempts,
deletes branches, or claims that a remote Jules session was cancelled.
The existing startup/hourly Jules maintenance pass consumes due cleanup even when
no new Issue or PR event arrives, and cleanup reasons name the selected PR whenever
the generation ledger has one.
