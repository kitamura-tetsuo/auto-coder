# Bounded current-head test-oracle-gap completion

Ordinary adversarial validation compares every accepted ordinary material
test-oracle gap with the decisions returned for the current repository, pull
request, head, base, and Requirements snapshot. A recorded gap omitted by the
otherwise applicable reviewer and evidence rounds receives one focused semantic
completion request. Explicit current-target OPEN decisions, accepted current-head
terminal decisions, and gaps owned by the accepted Strong lifecycle are excluded.

The request batches all missing IDs and supplies their canonical original scope,
historical evidence, target identity, Requirements manifest, changed-file and
test manifests, prior adjudication, and available CI evidence. It uses no-edit
execution and can use either safe session continuation or the existing fresh
execution path. The completion prompt forbids broad discovery and requires an
exact OPEN, RESOLVED, or INVALID decision for every requested ID.

Qualifying transitions pass through the existing lifecycle reconciliation and
reviewer-session checkpoint. Independently established findings, specification
gaps, provenance blockers, and thread dispositions survive the focused response.
A malformed response, an omitted requested ID, or execution failure consumes the
single opportunity and returns a diagnostic BLOCKED result naming the target and
remaining IDs; it never manufactures a production repair or retries again during
the invocation. Accepted Strong findings remain owned by their separate store and
are never closed by this completion.
