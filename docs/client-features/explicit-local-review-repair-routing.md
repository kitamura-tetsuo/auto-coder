# Explicit-local unresolved-review repair routing

An open pull request whose uncached, authoritative body contains
`<!-- auto-coder:local-llm -->` or the supported legacy
`<!-- auto-coder:local -->` marker is classified as requiring local review repair
before linked-Issue cloud history or cloud-delivery receipts are consulted. The
classification records the API origin, repository, pull-request number, head
repository/ref/SHA, and declaration observed at the boundary. It remains a
not-yet-executed, merge-blocking result; it is not a local completion receipt.

A durable association for that exact repository and pull-request number, or
verified Codex publication attribution for that pull request, conflicts with an
explicit local marker and fails closed. Unavailable authoritative PR or
association evidence also fails closed. The target evidence is read again before
the local-required result is consumed, so a changed marker, target, head, or
association invalidates the earlier decision. Linked-Issue bindings and receipts
are neither erased nor used to override an explicit-local route.
