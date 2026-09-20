# Explicit implementation retry production activation

`process-issues --only <issue> --force --retry` creates a distinct durable
operator request for one authoritative Issue generation. The request is
accepted only after the normal readiness, review, hierarchy, dependency,
author, shutdown, ownership, capacity, and provider gates have run. A selected
container parent is rejected with guidance to select a direct child; PR targets
and incomplete flag combinations are rejected without retry authority.

At production ownership admission the request is bound to a durable attempt and
acquires an execution through the request-aware ownership adapter. This is the
narrow exception to an existing same-generation start tombstone: ordinary
processing, `--only`, and `--only --force` remain suppressed after a completed
start. The same request and attempt are passed to the local or cloud handoff
journal, so adapter replay cannot allocate another attempt or creation.

A different CLI invocation has a new request identity even when its text is
identical. A still-pending request whose creation outcome is not known blocks a
second grant and is named in the diagnostic. Command diagnostics distinguish
that deferral from historical-generation suppression, and the existing
`issue.manual-retry` and dispatch-route traces describe accepted production
processing without introducing a parallel dashboard schema.
