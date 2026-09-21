# Verified Codex task PR-body projection

Normal, explicit, and resumed PR processing project a Codex Cloud task URL only
from the durable `VERIFIED` origin for that exact repository and PR. The
projection never consults the mutable Issue session pointer or Issue comments,
and unresolved, unavailable, conflicting, foreign, or non-Codex attribution
cannot authorize a body update.

Immediately before an update, Auto-Coder bypasses caches to read the live PR
body and rechecks the durable attribution token. It preserves that body and
appends only `https://chatgpt.com/codex/tasks/<task-id>`. An already-present
supported equivalent URL confirms the projection without rewriting it. A
successful update or authoritative read is required for confirmation; rejected
or indeterminate writes remain retryable, and the next processing pass rereads
GitHub before deciding whether an append is still needed.

Projection is presentation only. Its success or failure does not create,
replace, continue, cancel, or otherwise modify task ownership, attempts,
provider routing, Issue tracking, repair authority, or implementation outcome.
Removing the link permits a later eligible pass to restore the same durable
origin without rebinding it.
