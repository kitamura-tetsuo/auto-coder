# Trusted observations for early dependency waiting

The daemon uses a separate process-local, per-Issue observation cache exclusively
to identify Issues with explicitly declared open prerequisites before ordinary
implementation candidate refresh and submitted-parent validation. Before the
operational wait is committed, generation-aware Review routing performs its own
authoritative family reconstruction so a specification edit cannot retain stale
lane identities. Startup discards observation-cache trust.
A cold observation fetches only the target and its declared prerequisites through
the strict GitHub reader. Complete Issue webhooks update that Issue without
invalidating unrelated observations. Ambiguous equal-timestamp updates, older
deliveries, incomplete payloads, deletion/transfer and relationship notifications
invalidate affected observations; an in-flight REST response cannot overwrite a
newer notification. Duplicate deliveries do not renew observation age.

Observations expire after 300 seconds. A cached wait retains a durable retry at
most 300 seconds later, so missing notifications cannot strand work indefinitely.
Issue notifications wake advisory dependency waits without lifting GitHub rate
limits or startup stabilization guards. Existing repository dependency discovery
continues to recover unknown reverse relationships. The cache never authorizes
implementation or replaces the final strict dependency and ownership gates.
Explicit/operator and provider-resumption paths retain their existing checks.

The dashboard detail view records `issue.cached-dependency-wait` as `deferred`,
with `waiting_on`, `retry_at`, `evidence_source=local-issue-observation`, and
`authorizes_execution=false`. This is observed dependency waiting, not a verified
specification result, implementation start, or completion.
