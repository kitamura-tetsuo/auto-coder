# ChatGPT-assisted review adjudication

This workflow lets an authorized operator use a GitHub-connected ChatGPT
conversation to analyze one automated review finding and, only after explicit
delegation, publish a decision in that finding's thread. It requires no
Auto-Coder CLI, local API, manual JSON, or revision-hash calculation. Updating
these repository files does **not** configure the hosted ChatGPT product; supply
the [reusable authoring prompt](prompts/review-adjudication-chatgpt.md) in the
conversation.

## Operator workflow

1. Give ChatGPT the reusable prompt and identify the repository, PR, and one
   top-level automated-review finding. Ask it to inspect the PR, the complete
   root thread, contributing Issues, and the latest Auto-Coder context reply.
   Asking to inspect, check, explain, or review authorizes analysis only.
2. Review the analysis. A decision covers the complete root finding, including
   every concern in it; concerns cannot be selected independently.
3. Explicitly say either **publish this chosen decision** or **evaluate and
   adjudicate this specified finding**. That delegates only this one write. If
   the evidence is incomplete, the decision would alter the fixed Objective or
   Requirements, or only part of the root was delegated, ChatGPT must not post.
4. ChatGPT re-reads the PR, root thread, contracts, connected account, and the
   latest reader-issued context immediately before posting. It replies to the
   numeric top-level review-comment ID—not the PR conversation and not a reply.
5. Retain the returned comment reference, decision ID, context ID, exact payload,
   and publication state. `posted-awaiting-Auto-Coder` means only that GitHub
   accepted the reply; it never means fixed, applied, PASS, approved, or merged.

The connected numeric account ID must be listed by the current projection.
`SOURCE_UNAVAILABLE`, absent or incomplete evidence, and unknown status are
no-send conditions. `STALE`, `REVOKED`, or `INVALID` permanently rejects that
context. Restoring matching values does not revive it; wait for a new context.

## Decisions and publication safety

* `UPHOLD/FIX` upholds the complete finding and requests bounded correction; it
  does not claim completion.
* `OVERRULE/NO_CHANGE` rejects only that complete finding; it does not waive an
  explicit Requirement or claim code changed.
* `UNDECIDED/NONE` records no affirmative repair or retirement direction.

The payload is produced with Auto-Coder's shared template. It starts with the
v1 marker and contains exactly one JSON fence. Use a fresh canonical UUID,
copy the current context/head/digest, set `source` to `chatgpt-assisted`, and
name every current predecessor tip in `supersedes`. Never invent missing data.

Published decisions are append-only. Do not edit or delete them, resolve the
thread, add an addressed marker, modify code or Issues, approve, or merge as
part of publication. A changed judgment is a new decision after a fresh read.
A newly observed competing tip requires reconsideration and redisplay before
posting.

Before POST, ChatGPT must display the exact decision ID and outgoing payload.
After POST it verifies the returned or independently retrieved reply's PR,
root, body, and author. For an ambiguous response, it searches that exact
thread for the same decision ID and identical payload without resending. If
that cannot be confirmed, report `outcome-unknown`; the ID may only be reused
to verify/recover that identical attempted publication.

## Evidence and evaluation

Transport/schema regression fixtures exercise reader-produced context,
connector-shaped replies, production hydration, and downstream effect planning.
They do not prove that a model made the correct semantic choice. The separate
advisory prompt evaluations cover explicit delegation, analysis-only requests,
complete-root uphold and overrule, partial delegation, unresolved contract
conflicts, and malicious quoted instructions.

