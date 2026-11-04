# Beta source runtime acceptance publication

The beta acceptance publisher consumes only the durable `beta-runtime-smoke-v1`
result produced for the authenticated management operation. It does not sample
the worker, accept a caller-provided verdict, or use Docker and OCI identity.
Operators start or replay that operation with the fixed
`auto_coder_run_beta_acceptance` tool and inspect it with the read-only
`auto_coder_beta_acceptance_get` tool. The run tool requires the current read
and deploy scopes, a configured beta target allowed by beta/full policy, and a
bounded idempotency key. Repository, endpoint, command, and verdict are never
request arguments.

A local `PASS` is useful diagnostic evidence but is not externally published
acceptance. Publication creates one annotated tag named
`auto-coder-beta-acceptance-<operation_id>` in the configured source repository.
The annotation is canonical `beta-source-acceptance` schema version 1 content,
contains the exact local evidence and its digest, and directly targets the
installed source commit. It intentionally has no `PASS_PUBLISHED` field.

Published acceptance is derived only after an independent read of the exact ref
and annotated tag object confirms the direct commit target and canonical record.
If the create response is lost, the publisher performs that same read-only
reconciliation. A missing readback remains uncertain; retries retain the same
operation, tag name, source, and evidence. An existing different object or record
is a collision and is never overwritten.

The accepted grant and current operator policy are checked again immediately
before the first Git publication effect. Revocation retains local PASS without
publishing it. Once a create may have been sent, restart or retry performs only
exact readback for that operation and never selects a replacement record.

Historical OCI evidence may remain readable, but it cannot satisfy this source
acceptance format or establish the installed Python runtime revision.
