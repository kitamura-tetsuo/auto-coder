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

An explicit retry also recovers a predecessor local Issue job stranded in
`running` or `paused` after its runner and provider writer process group have
both positively stopped. Recovery occurs after the current Issue admission
gates and durable retry authorization, before acquiring the new execution.
The exact job/incarnation, runner, writer, executor token, and continuation
frontier are compared atomically. A concurrent resume or result checkpoint
prevents replacement. Live or unknown processes, missing continuation evidence,
confirmed output, result artifacts, and publication effects remain protected.

The predecessor is settled with an explicit retry-supersession receipt and a
replaced executor token. Its files, session, task evidence, and historical
generation ownership remain intact; no provider result or successful
implementation is inferred. Only its exact execution is released. Replaying a
crash-interrupted retirement finishes the same receipt before admitting the
request's original new attempt. Ordinary processing and `--force` alone never
perform this retirement. `issue.retry-predecessor-retirement` reports
`superseded` separately from later retry authorization and provider dispatch.

Run `bash scripts/test.sh tests/test_stopped_issue_retry.py` for stopped-runner
recovery, protected live/unknown/result states, concurrent resume, durable
retirement replay, and the mounted dashboard detail projection.
