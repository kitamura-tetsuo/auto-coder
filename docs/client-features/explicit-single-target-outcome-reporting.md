# Explicit single-target outcome reporting

`process-issues --only` reports the authoritative outcome of the requested target
as Success, Deferred, Skipped, Blocked, or Failed. The engine preserves the
resolved Issue/PR identity, structured outcome, actions, and reason even when a
non-successful target is absent from the legacy processed-item collections.
Missing or contradictory explicit results fail closed, and plain-number targets
retain their authoritatively resolved Issue or pull-request type.
When target lookup fails before resolving its type, the CLI preserves the original
error without adding a redundant missing-type diagnostic; missing identity still
cannot authorize success, and a mismatched target number remains an error.
Local GitHub governor deferrals include the reason and Unix retry deadline in
their exception message and explicitly state that the request was not sent.
This diagnostic-only change leaves admission decisions, execution outcomes, and
structured dashboard trace schemas and emissions unchanged.
