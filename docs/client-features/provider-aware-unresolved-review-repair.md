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

Saved adversarial reports can identify existing actionable roots by an exact
native thread ID with a `STILL_VALID` disposition, including strong-audit roots.
This preserves repair routing when the latest report rewords the original
finding rather than reproducing its paragraphs. Other explicit dispositions
do not authorize corrective delivery.

External review comments also receive controller-owned canonical blocker IDs at
repair preparation. Their original scope joins managed findings in the repair
bundle, including mixed reviews; native comment delivery receipts continue to
prevent duplicate follow-ups. See [External review feedback tracking](external-review-feedback-tracking.md).

When a reviewer uses the PR implementer's GitHub account, a corrective reply must
end with `<!-- auto-coder-review-guidance:v1 -->` on its own line. This explicitly
selects the reviewer role for that reply. Unmarked author replies (including
acknowledgments that report incomplete work) remain excluded; neither account
identity, prose heuristics, nor absence of an addressed claim establishes that
role. PR-author replies carrying an addressed marker remain implementation claims even if
they also contain the guidance marker. A trailing guidance marker cannot make
an adjudication envelope into ordinary feedback, regardless of its author. Adjudication envelopes still belong only
to the adjudication orchestrator. The guidance marker grants no adjudication,
closure, approval, or merge authority. This selection also applies to explicit
local review corrections after the latest addressed claim.

New guidance uses its native comment identity with the existing receipt and
pending-reservation protocol. It does not renew an old root request, and editing
a delivered comment does not allocate a new delivery. Genuinely new guidance
uses a new explicitly marked reply; copying old guidance into a new reply creates
a different identity and can duplicate work semantically.

An existing unmarked reply can instead receive an explicit role annotation on
that same native comment. The reviewer/operator must first verify that the
original reply is reviewer guidance, still applies to the current head and
contract, and has not been superseded by later implementation or review. Append
the guidance marker to that original reply, preserving its substantive prose
and native ID, rather than copying its text into a new comment. A formerly excluded identity can then be delivered once through
the normal polling path. If that identity already has a confirmed receipt or an
uncertain pending reservation, the marker/edit does not authorize redelivery;
wording edits after delivery also retain the same identity. The production-path
regression covers unmarked, annotated and subsequently edited versions of one
native reply. This is an explicit operator declaration, not automatic inference
from historical prose. Auto-Coder does not annotate old comments, clear receipt
state, force reevaluation, resend requests or restart active work for recovery.
Ordinary independent adjudication and closure gates still apply.

A poll finding only prior delivery receipts is deferred while the unresolved
thread remains blocked. It does not claim a fresh accepted handoff. Receipts
prove prior delegation, not current implementation progress or completed repair.
