# Codex execution error diagnostics

When local Codex execution fails, its console summary includes a `Cause:` line
from the terminal `turn.failed` event, or the last top-level `error` event when
there is no terminal diagnostic. Provider errors encoded as JSON inside event
messages are unpacked to expose the actual rejection reason. Metadata warnings,
assistant messages, and command output inside item events are not selected as
the cause. The cause is redacted and limited to 2,000 characters; the existing
exception and interaction log retain the complete execution evidence.

If the provider rejects a configured model while the CLI reports missing model
metadata, check the executable used by the running application with
`codex --version`. The production runtime pins its Codex version in `Dockerfile`;
an older, already-running container can still use an earlier executable. Update
the runtime to the pinned version and verify the configured model before changing
model selection. A successful request with the updated CLI confirms model access
for that runtime; catalog presence alone does not establish account access.

This console diagnostic change is observability-neutral: it does not change
provider routing, invocation admission, retry decisions, processing outcomes,
durable state, trace emissions, or dashboard event schemas. Existing production
error evidence remains intact for dashboard consumers.
