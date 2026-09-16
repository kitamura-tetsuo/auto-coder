# Provider-aware unresolved review repair

Actionable unresolved review threads keep merge blocked and are routed through the
provider-aware durable `cloud.csv` association to the existing originating cloud
session. Jules and Codex Cloud therefore receive repair feedback through the same
`CloudTaskClientBase.send_followup` contract, targeting the existing PR and its
current head branch, fetched from a cache-bypassing authoritative GitHub lookup
immediately before transport. Per-finding local and PR-side receipts prevent
redelivery, while new findings remain eligible. A durable pending reservation is
written before transport; if an accepted request cannot be confirmed in either
receipt store, later runs suppress speculative duplicate delivery and report the
indeterminate state explicitly. Provenance-clarification threads remain excluded.
Missing or ambiguous ownership, unsupported providers, rejected requests, and
transport failures produce an explicit failed processing outcome rather than a
successful defer.
