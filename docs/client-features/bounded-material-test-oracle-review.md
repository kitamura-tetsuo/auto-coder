# Bounded material test-oracle review

The initial authoritative adversarial review may report a small, consolidated
set of material test-oracle gaps separately from demonstrated implementation
violations and specification gaps. Each gap is tied to an explicit Issue
requirement, an exact production boundary and invariant, a plausible incorrect
implementation that existing tests would miss, and one focused regression
scenario. An open gap produces `NEEDS_TESTS`, requests only committed regression
protection, and blocks automatic merge without claiming that current production
behavior is wrong.

The reviewer identifies a gap's Issue requirement only by its stable manifest ID.
Unknown IDs fail closed, while human-readable requirement text used in review
comments, prompts, and persisted lifecycle state is populated deterministically
from the authoritative requirement manifest rather than copied from model output.

Gap identities and scopes persist with the PR-scoped reviewer session. Rereviews
revalidate recorded gaps and the corrective diff instead of restarting broad
test exploration. New rereview gaps are accepted only when the corrective diff
introduced a boundary, weakened protection, or required revalidation directly
exposed an entirely untested authoritative material boundary. Closed gaps cannot
be reopened for input variants or stronger assertions outside their original
boundary and invariant.
Previously published gap identities reuse their existing unresolved review
threads; insufficient corrective commits do not create duplicate root threads.
An evidence-backed independent `ADDRESSED` disposition for a thread whose root
identifies the exact persisted gap and requirement closes that gap even if the
same validation payload repeats it as `OPEN`. The reconciled `RESOLVED` state is
persisted before the corresponding GitHub thread mutation and remains distinct
from unrelated non-passing findings or operational failures. A visible resolved
thread, an implementer claim, malformed disposition evidence, or an ambiguous
gap-to-requirement association never supplies this authority.
Accepted closures record the evaluated head. Unchanged-head observations reuse
that evidence despite omitted or stale `OPEN` projections, while a later head
keeps the prior evidence as historical only: explicit current `OPEN` evidence
reopens the gap and missing current protection evidence blocks without inventing
a new gap. Explicit structured closure entries must carry their stored gap ID.

The owning PR attempt defers reviewer-session persistence until its serialized
application transition has confirmed both the authoritative head and that no
newer attempt was registered. It commits an accepted exact-gap closure before
thread effects even when unrelated bounded evidence recovery leaves the overall
review inconclusive; persistence failure suppresses those effects and reports a
dedicated non-passing diagnostic.

The broad initial review independently traces every material requirement from a
supported production origin across the parser/configuration, model, manager,
selector, persistence, authorization, UI, or public-API boundaries that matter
to its observable behavior. Direct helper calls and tests that synthetically
construct an internal representation are not sufficient proof that the state is
production-reachable or preserved. For each material requirement, the reviewer
also considers a minimal plausible incorrect implementation that might leave the
committed tests green, including a downstream helper that accepts a Set/group
while its real upstream path flattens or never creates that representation.
Demonstrated production reachability failures produce `NEEDS_FIX`; evidence that
remains decision-critical after bounded recovery produces `INCONCLUSIVE`; and
correct production behavior lacking a material regression oracle continues to
produce `NEEDS_TESTS`. A successful initial `PASS` therefore requires valid
production-path evidence for every `VERIFIED` cross-boundary requirement. The
narrow incremental rereview policy is unchanged.

Issue implementation and adversarial-fix prompts likewise require regression
coverage to begin at a supported production origin and cross enough material
boundaries to detect information loss, flattening, reinterpretation, or bypass.
A synthetic-state helper test remains useful as additional unit coverage, but
cannot be presented as the regression oracle unless production-origin coverage
proves the same state can be created and preserved.
