# Authenticated accepted-source release dispatch

`auto_coder_promote_accepted_source_release` admits one exact Git source promotion
through the standalone management MCP resource. It requires current browser
consent for `auto-coder.read` and `auto-coder.deploy`, an active original grant,
current exact UID/client/resource policy, and `full` operating policy for the
configured release target. Observe and beta profiles refuse admission. The same
authority and operating policy are checked again immediately before first send.
OAuth claims and workflow inputs cannot change operator configuration.

The action takes exactly these fields:

| Field | Meaning |
| --- | --- |
| `target_id` | The operator-configured release promotion target |
| `source_sha` | Full lowercase 40-character accepted Git commit |
| `ci_run_id`, `ci_run_attempt` | Positive original successful tested-source Actions identity |
| `tested_source_ref` | `refs/tags/auto-coder-tested-<source_sha>` |
| `tested_source_oid` | Full immutable annotated tested-source object OID |
| `tested_source_record_sha256` | Canonical original tested-source record SHA-256 |
| `acceptance_ref`, `acceptance_oid` | Published immutable beta acceptance tag and annotated object OID |
| `idempotency_key` | 1–128 characters, first alphanumeric, then alphanumeric or `_.:-` |

The producer's `AcceptedSourceValidator` independently checks the annotated
objects, original CI attempts/jobs, original beta selection history, installed
source provenance and worker-produced advancing heartbeats. Mutable branch/tag
selection alone cannot qualify. The admission snapshot retains the validated
acceptance identities, policy, target, candidate and original authority. Replays
retain the original operation and authority; changed intent conflicts. All
clients and keys for the same source repository/release ref share one durable
journal lane. An unresolved old send blocks a new key or candidate.

## Trusted configuration

All four management YAML settings must be supplied together to enable dispatch:

```yaml
source_release_policy_path: /operator/accepted-source-release-policy.json
source_release_token_file: /operator/release-actions-token
source_release_target_id: release-source
source_release_workflow_id: 123456
```

Paths are absolute operator-owned configuration paths, never tool arguments.
The policy uses the strict `SourceReleasePolicy` schema documented in
`accepted-source-release-promotion.md`. It fixes the GitHub repository,
`refs/tags/auto-coder-release-source-current`, `main`, and
`.github/workflows/promote-accepted-source-release.yml`; the configured numeric
workflow ID is independently checked against that file and repository URL.
The operating policy separately maps `release-source` to channel `release` and
that source repository. No worker bridge or Docker permission is required.
The token needs Actions write and the reads necessary to verify source evidence,
Git refs, catalogs and Releases. Credentials are read only by the shared
`gh_cache.py` HTTP client and never appear in the journal, results or logs.

The fixed POST requests `return_run_details: true` and serializes every workflow
input as a JSON string, matching
[GitHub CLI's dispatch boundary](https://github.com/cli/cli/blob/trunk/pkg/cmd/workflow/run/run.go).
Original CI run ID and attempt remain positive integers in MCP arguments, the
retained intent, and source evidence. The workflow keeps its `number` declarations
and passes its environment values through the CLI's integer options before
publication.
No caller can choose an API URL, workflow, branch, credentials or additional
inputs. The run name is `accepted-release/<management_operation_id>`. The journal
records `SENT_UNKNOWN` before any possible send, and a process-owned file lock
keeps another manager from reconciling/freeing a still-active sender's lane.
A process crash releases the file lock, not the durable reservation.

## Readback and recovery

Use `auto_coder_operation_get` with the returned `operation_id` (optionally its
`target_id`). It enforces authenticated original actor/client ownership. Startup
also reconciles outstanding records. Each step performs finite reads, with no
background infinite polling or automatic redispatch. A valid positive dispatch
response is persisted before run readback; its ID cannot be replaced by a listing.
The exact run must match the configured workflow ID/file, repository, dispatch
event, main branch and exact display title. Workflow checkout `head_sha` is
retained separately from `selected_source_sha`.

For a lost response, search is limited to five pages of 100 runs from the fixed
workflow, filtered by event and branch. The result must be complete, internally
consistent and contain exactly one verified operation-name match. Empty, duplicate,
changing, truncated or unavailable results remain `NEEDS_ATTENTION`; a listing
gap never proves that the request was unsent. Once bound, only the original run
and attempt are read. No latest-run guessing, workflow editing or second POST
is used, including after a denied/ambiguous HTTP response or workflow failure.

An unconfirmed HTTP response records its status as a fixed category such as
`dispatch_http_422`. The recognized validation message for one of the fixed
input names adds that name, for example
`dispatch_http_422_invalid_input_ci_run_id`. Other provider response content is
not recorded in results, logs or journal history. These diagnostics retain
`SENT_UNKNOWN`, `NEEDS_ATTENTION` and the original lane; they never establish
absence of an effect or permit another send. Later readback may change the
current reason, while the original HTTP category remains in journal history.

Workflow success alone is insufficient. Readback must match the strict schema-2
source catalog, original candidate/test/acceptance/profile/policy binding, exact
Actions run and attempt, direct release source ref and GitHub Release association.
Historical evidence is checked at its recorded validation time, so elapsed
eligibility lifetime cannot erase an already dispatched operation's history.
Completed operations expose `SOURCE_PROMOTION_CONFIRMED` or
`COMPLETED_BUT_SUPERSEDED`; missing/partial/contradictory publication stays
`UNRESOLVED`. Failed or cancelled runs retain their lane because a source ref
effect may already exist. Proven pre-send refusals terminate as failed/rejected.
There is no timeout-based lane release, force-clear or reset-to-new-candidate API.

After grant loss or policy downgrade, exact existing-operation reconciliation is
read-only. An operator may authenticate anew with read scope to query their own
original operation without replacing its accepted authority. Missing publication
or ambiguous correlation requires independent investigation and intact retained
records, not another key or dispatch.

Every result says `worker_deployed: false`. Source promotion does not update a
release worker, pull/restart an image or establish worker health. Historical OCI
release catalogs and installed Docker provenance remain read-only context and
cannot satisfy the source-promotion postcondition. A future worker deployment is
a separate authenticated action consuming the confirmed source record.

Management admission and fixed-category observation messages use the existing
Loguru console/file sinks. This standalone action creates no Issue/PR scheduler
execution, provider routing or dashboard trace event. Its authoritative view is
the authenticated operation projection and immutable journal history; workflow
completion cannot become dashboard deployment success. See the inventory in
`docs/dashboard-observability.md`.

Run `bash scripts/test.sh --ci-tests-only tests/test_management_source_release_dispatch.py`
for production HTTPS MCP, real consent and persistent-journal regressions over a
controlled GitHub transport executing the actual acceptance/release producers.
These deterministic component tests do not dispatch a live GitHub workflow or
claim a live release-worker deployment.
