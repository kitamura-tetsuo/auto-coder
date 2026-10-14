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
remaining IDs. Qualifying transitions for other IDs are still checkpointed before
that non-pass result. Completion never replaces unrelated Requirement coverage or
evidence blockers, manufactures a production repair, or retries again during the
invocation. Accepted Strong findings remain owned by their separate store and are
never closed by this completion.

Local completion continues a provider session only when the manager can prove its
retained workspace binding; otherwise the self-contained prompt uses the supported
fresh no-edit route. Terminal RESOLVED evidence must identify committed assertions
at the recorded boundary that protect the original invariant, while INVALID
evidence must state how a necessary material-gap criterion fails rather than merely
name that criterion. Generic all-VERIFIED or green-CI prose is non-authorizing.

The four advisory Promptfoo cases under
`prompt-evals/targets/test-oracle-gap-completion/` evaluate genuine protection,
misleading all-VERIFIED/green-CI evidence, an out-of-scope variant, and a real
regression. They execute a real model only when the selective prompt-evaluation
workflow is explicitly run; no real model was executed while authoring the
fixture cases.
