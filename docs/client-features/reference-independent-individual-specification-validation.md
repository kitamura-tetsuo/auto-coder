# Reference-independent individual specification validation

Individual Issue specification review treats the caller-supplied, ordered
Requirement manifest as its sole implementation contract. The production prompt
does not retrieve or request external documents, repository files, standards,
interfaces, commits, or URLs. A reference is ordinary provenance when the manifest
already states the decision-critical semantics; reachability and retrieval-error
diagnostics cannot change the semantic outcome. If required semantics are instead
delegated to a reference, the successful review reports the missing written
boundary as `unverifiable_requirement`, while an actual invocation or output
validation failure remains a fail-closed `ERROR`.

Every durable individual decision is bound to the exact caller-supplied manifest,
including Issue number, title, explicit-contract presence and validity, and the
ordered Requirement IDs and texts. This binding is independent of the Markdown
body digest and survives restart. Reuse, late-result acceptance, and implementation
authorization therefore cannot substitute a result produced for another manifest
or reconstruct a missing binding from Markdown. Invalid manifests fail before
semantic transport, and explicit rerun authority continues to revoke all older
decisions for the selected stable subject.
