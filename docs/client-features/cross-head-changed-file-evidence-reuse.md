# Cross-head changed-file evidence reuse

Recovered changed-file evidence is persisted with versioned identities for the
complete authoritative per-file PR representation and exact Issue Requirement
manifest. A later PR head reuses only `RECOVERED` entries whose two identities
still match. Commit-qualified REST locator URLs are excluded from that semantic
identity, while a missing or incomplete patch cannot produce a reusable
identity. Changed or unverifiable paths are invalidated independently; legacy
entries and `IRRELEVANT` classifications fail closed and are re-adjudicated.
The durable ledger preserves explicit fresh, reused, invalidated, and
re-adjudicated transitions together with original and current provenance. Reuse
and fresh recovery are accepted only after a final cache-bypassing confirmation,
after all dynamic work, that the current validation snapshot stayed fixed.
Only file-evidence accounting is reused: Requirement coverage, findings,
test-oracle gaps, and the top-level verdict are always adjudicated for the
current snapshot.
