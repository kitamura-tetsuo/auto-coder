# Revision-bound review adjudication protocol

The production GitHub boundary fully hydrates nested review-comment pages with
stable actor/comment identities, actor type, revisions, reply relation, raw
Markdown, and thread resolution. A repository-scoped SQLite ledger atomically
preserves one live context per root, decision history, retirement, observation,
and publication state across restart. Copy-ready context replies expose the
complete contract binding and a renderer-produced v1 envelope; ambiguous writes
are reconciled against the actual target thread before retrying. Ordinary
discussion and webhook bodies remain non-authoritative.

Every normal PR-processing origin now crosses this refresh boundary before PR
admission. Startup requeues open and unfinished tracked PRs, while authoritative
contributing-Issue associations wake the affected PR on Issue invalidation even
when its head is unchanged. Configured refresh failures remain operational PR
errors rather than allowing a stale positive adjudication to be consumed.

Auto-Coder provides a provider-independent, side-effect-free v1 model for
authorized decisions about one complete automated-review root thread. Literal
versioned JSON envelopes distinguish `UPHOLD/FIX`, `OVERRULE/NO_CHANGE`, and
`UNDECIDED/NONE` from implementation-addressed claims. Registered contexts bind
repository and GitHub thread identities, source revisions, exact head/base,
canonical explicit Issue contracts, and byte-exact Objective fingerprints.

Decision authority uses a repository-scoped `review_adjudicator_allowlist`
independently from the automated root's `pr_review_allowlist`; both fail closed
and accept only positive numeric GitHub actor IDs. The durable decision graph is
ordered by authoritative GitHub creation instant and numeric comment ID, exposes
conflicting tips, and permanently retires a context after accepted-source
collision/edit/deletion, bound revision changes, or current-tip authorization
revocation. Incomplete evidence instead reports `SOURCE_UNAVAILABLE` without
changing durable state. A checked-in JSON schema and copy-ready renderer use the
same production parser. This foundational model emits no processing trace and
performs no GitHub, repair, test, thread-resolution, Issue, or PASS side effect,
so the dashboard observability production-to-view contract is unchanged.

## Dashboard operator surface

PR diagnostic pages link to a separate `/dashboard/adjudication/pr/<number>`
GitHub review-adjudication surface. The diagnostic page remains a process-local
projection and makes no GitHub request; the separate surface reads and writes
only through the authenticated adjudication service. Issue diagnostic pages do
not expose adjudication controls.

The operator page reports configuration, the server-verified numeric publishing
identity and repository allowlist result before enabling work. It renders the
root finding and rationale as inert text, identifies observation freshness and
contract/head bindings, and keeps applicability, confirmed publication, and
durable downstream processing as separate states. Previewing uses the common
server renderer and allocates a reader-bound decision identity; only the final
confirmation can publish. Rejected stale or retired drafts retain the typed
rationale on the page but require a fresh context, preview, and decision ID.
Unknown publication outcomes are recovered by status lookup under the original
decision ID and are never blindly reposted.
