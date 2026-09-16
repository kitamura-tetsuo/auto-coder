# Native sibling dependency reconciliation

GitHub Issue lifecycle, native dependency, and sub-issue deliveries now create
a durable repository-scoped reevaluation obligation before acknowledgement.
The obligation conservatively rediscovers every open Issue (including body-only
declarations), coalesces duplicate deliveries, survives restart, and feeds each
identity through normal authoritative intake. Endpoint Issue numbers are used
only when their repository identity matches the monitored repository. Startup
performs the same open-Issue reconstruction, while creation-anchored
stabilization remains the only fixed eligibility delay.

After the Issue creation-stabilization window, production intake treats a
supported `Blocked-By:` line as the complete desired native GitHub incoming
dependency set. It first materializes declared parent relationships, requires
every dependency to be a distinct direct sibling Issue, validates the complete
family (including closed children) for cycles, and then reconciles GitHub using
stable Issue IDs with complete incoming/reverse pagination and authoritative
readback. Removing the declaration leaves native dependencies unmanaged, while
an explicitly empty declaration clears them for a supported child declaration.
A standalone Issue may carry an empty `Blocked-By:` to mean no sibling
prerequisites only when its parent declaration is absent and a strict GitHub
read confirms no native parent. Nonempty/malformed declarations, invalid parent
metadata, native children without supported parent declarations, and unavailable
parent reads do not receive this exception. This avoids indefinitely waiting for
an undeclared parent on otherwise eligible standalone Issues. The final dispatch
check emits `issue.sibling-dependency-gate` with completed (dependency check
satisfied), blocked (invalid), or deferred (waiting/unavailable); satisfaction
alone is not implementation completion.

Malformed, conflicting, non-sibling, nonexistent, pull-request, and cyclic
declarations are rejected before implementation ownership. A marked actionable
diagnostic is reused across retries, and readiness is withdrawn independently
from both the declaring child and its current authoritative parent. Operational
or ambiguous GitHub failures fail closed without asserting a specification
defect or withdrawing readiness. Valid open direct or transitive prerequisites
defer implementation (but not enabled semantic validation); only authoritative
closure satisfies them.

Issue numbers do not imply precedence. Independent eligible siblings may run
concurrently; numeric ordering is used only as a deterministic presentation
tie-breaker. Authors must express required order explicitly: every generated
child should accompany `Parent-Issue: #<parent>` with `Blocked-By:` (empty for a
dependency root, or comma-separated local references such as `#12, #34`).
Dependency metadata is structural and remains outside the child's normative
`## Requirements` section. Existing unannotated families use their reconciled
native dependency edges and receive no historical number-based backfill.
