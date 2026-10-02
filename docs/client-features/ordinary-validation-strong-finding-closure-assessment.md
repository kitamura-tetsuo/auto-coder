# Ordinary validation strong-finding closure assessment

Ordinary adversarial validation accepts an optional controller-captured
`ReviewExecutionInput` for an accepted strong-review finding set. When supplied,
the normal ordinary prompt includes the complete accepted finding bundle,
Requirements and policy identities, audited and evaluated heads, cumulative
diff, repository evidence, round, revision, and ordinary attempt identity. The
reviewer returns the ordinary verdict and a separate `closure_assessment` in the
same semantic response; no extraction or closure-only model call is made.

The controller binds omitted computable identity fields to the captured input
and rejects contradictory identities, missing or duplicate dispositions,
unknown finding IDs, missing evidence, or invalid scope. A complete assessment
retains the actual ordinary backend/model provenance and independently reports
each finding as `FIXED`, `INVALID`, `OPEN`, or `INCONCLUSIVE`, plus cumulative
scope as `BOUNDED`, `EXPANDED`, or `UNKNOWN`. Invalid or absent extension data is
recorded as a closure diagnostic without erasing a valid ordinary verdict.

The extension is deliberately non-authorizing. It does not accept durable
findings, complete a review cycle, resolve threads, publish a review, or merge a
PR. Follow-up semantic responses must contain their own complete assessment;
favorable evidence is never carried forward from an earlier response. Calls
without closure input preserve the existing ordinary-only prompt and result.
