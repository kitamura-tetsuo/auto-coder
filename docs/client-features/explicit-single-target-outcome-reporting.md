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

For local Issue implementations, `--only` drives the exact accepted job on a
command-owned runner and waits for its invocation and controller-owned
publication finalization. Acceptance alone never authorizes Success. Confirmed
PR publication or authenticated no-change settlement succeeds; failed, interrupted
or cannot-fix invocation results fail; denied entry, ambiguous execution, and
unconfirmed publication remain Deferred with durable recovery evidence. The
runner does not claim, recover, or finalize unrelated jobs. Regression coverage
is in `tests/test_explicit_local_issue_completion.py`.

If another controller wins the exact job claim, the command also waits while
that recorded process lifetime is authoritatively alive. It never reclaims a
provider-entered job or invokes the model twice. Missing or ambiguous owner
liveness remains Deferred for reconciliation.
