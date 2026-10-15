# PR lifecycle migration test inventory

The maintained `pr_lifecycle_contract` pytest group is the prerequisite
production-origin regression boundary for extracting PR lifecycle code. It is
not a live-provider E2E suite: GitHub/provider/Git edges are controlled, while
the production CI interpreter and PR merge route consume those responses.

## Inspected baseline and selection

The characterization was inspected at revision
`6c0cc09cde557985ecc484dbc35cb90fd85486f0`. Run the maintained group with:

```console
bash scripts/test.sh -m pr_lifecycle_contract tests/test_pr_lifecycle_contract.py
```

The directly affected pre-existing mergeability tests can be selected with:

```console
bash scripts/test.sh tests/test_pr_mergeability_remediation.py
```

## Production origins and controlled effects

The group enters `_check_github_actions_status` with complete passing, pending,
known-empty, unavailable, superseded, and replacement-attempt observations.
It enters `_handle_pr_merge` for enabled, disabled, and exhausted conflict
remediation. It also carries those CI observations into the merge consumer,
uses explicit checkpoints to order a remote H1-to-H2 change and a durable newer
validation attempt before the merge effect, proves the unchanged-head positive
case reaches that effect, and replays a real temporary cloud-repair receipt
through `_handle_pr_merge`. Green-CI cases additionally retain review-thread
blocking and a completed local correction awaiting independent validation as
deferrals; neither is reinterpreted as CI failure or merge authority. A green
review-repair case enters the same PR boundary twice with a temporary durable
receipt store, proving replay retains the cloud owner without redelivery or a
local substitute. Temporary production review-cycle stores retain an accepted
Strong finding and a pending Strong audit separately under green CI; both stop
before merge without entering CI-failure repair.
Only `observe_ci` and service construction are controlled for CI;
the resulting `GitHubActionsStatusResult` is produced by production code. The
terminal-failure case retains Codex Cloud ownership at the provider boundary.
Conflict replay traverses `_start_mergeability_remediation` and the production
base-update route with controlled Git commands, retaining one cloud delivery
from its temporary receipt and never entering local conflict repair.

The existing final-boundary and durable-owner coverage remains in
`tests/test_adversarial_validation_pr_flow.py`,
`tests/test_cloud_conflict_delegation.py`, and
`tests/test_pr_repair_exhaustion.py`. Those tests cover changed-head refusal,
newer applicable attempts, green-CI review deferral, retained cloud delivery
receipts, and repair exhaustion. Persistence-sensitive cases use temporary
stores; Git-state-sensitive conflict work remains covered by the real temporary
repositories in `tests/test_local_conflict_workspace.py`.

## Extraction risks and observed results

Existing tests commonly patch imported globals in `pr_processor`, especially
`_check_github_actions_status`, `check_github_actions_and_exit_if_in_progress`,
`_get_mergeable_state`, and `_merge_pr`. Moving those names without a shim will
break patch lookup even if behavior is unchanged. Intermediate extraction
commits can also duplicate the early candidate CI veto and final CI read, lose
newest-attempt filtering, or move the remote-head/newer-validation checks after
the merge effect. The final-attempt cases pause immediately before the final
serialized admission, after earlier validation/publication checks; a newly
registered durable attempt fences merge while the equivalent current attempt
reaches it. The maintained group therefore keeps a production CI
interpreter boundary and an actual `_handle_pr_merge` route boundary available
independently of those historical patch sites.

At correction, the maintained group completed with 24 passing tests. No test
was skipped or xfailed, no paid provider was contacted, and ordinary CI
remained unchanged. This test-only characterization changes no processing origin,
admission rule, route, outcome, durable resumption path, or trace schema, so the
dashboard production-to-view contract and `docs/dashboard-observability.md`
require no update.

## Extracted review composition boundary

Green-CI processing now delegates ordinary and two-tier review composition to
`_coordinate_pr_review`. The coordinator receives the selected repository, PR,
head, and base identities through `PRReviewScope`, returns a typed
`PRReviewCoordinationResult`, and continues to use the existing review,
correction, durable-result, publication, and gate producers. The caller owns
`PRReviewRuntime`, so its validation admission lease and attempt-transition
state remain live until the coordinator has completed the remote-head, current
attempt, CI-authority, two-tier authorization, and merge-effect checks.

The maintained contract group patches external GitHub/model effects at their
existing production owners while traversing both `_handle_pr_merge` and the
coordinator. It asserts the typed positive disposition and exact scope, plus
stale-head and newer-attempt barriers before the controlled merge effect.
