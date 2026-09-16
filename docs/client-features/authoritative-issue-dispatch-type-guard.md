# Authoritative Issue dispatch type guard

Before any Issue implementation side effect (backend start, CloudRun
creation/persistence, attempt increment, work branch creation, or Issue
task-start comment), Auto-Coder establishes the target's authoritative
GitHub item type with a cache-bypassing lookup (`get_item_type_strict`),
which talks to GitHub directly rather than through the shared hishel
caching client. GitHub's Issues API represents pull requests as issue-like
objects, so a caller-supplied candidate type -- including one built from a
cached Issue response, or one from collection, an explicit
`target_type="issue"` request, a retry path, or another internal enqueue
path -- is never trusted on its own. A target that resolves to a pull
request is rejected from Issue dispatch even when
it carries the `@auto-coder` label or a Codex Cloud task reference; it
remains eligible for the normal PR-processing lifecycle. A failed or
ambiguous type lookup fails closed: no Issue backend task or lifecycle
state may be started. Genuine Issue processing (backend selection, CloudRun
duplicate-dispatch protection, attempt handling, task comments) is
unaffected.
