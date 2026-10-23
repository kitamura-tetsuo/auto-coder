# Durable local PR repair admission

Selected local CI-failure and merge-conflict repairs are offered to
`LocalJobStore` as exact repository, PR, head, base, repair-kind and backend
operations. The producer repeats the authoritative open/unmerged and branch
identity read immediately before admission. CI admission additionally retains
the complete terminal failed-check execution identities; an affirmative
conflict observation is independent of empty CI evidence.

The ordinary PR processor enters this boundary after selecting either its
local CI route or its definitive local conflict route. An accepted envelope is
an asynchronous handoff, so the selector does not continue into the former
synchronous checkout/model operation. Re-entry recovers the same envelope;
failed or blocked admission is deferred without falling through to local or
cloud execution.

The SQLite transaction returns an identical retained envelope for an exact
replay and refuses a competing unsettled CI, conflict, or review-correction
edit owner for the same PR. A changed head/base, fork origin, non-local route,
invalid owner allowance, incomplete evidence, contradictory replay, or storage
failure is a deferral rather than permission to fall back to another backend.
The final metadata read re-evaluates local-versus-cloud routing, and an
unavailable allowance snapshot fails closed. A readable ledger with no namespace
for this PR is a normal first-use state, including when only one of the blocker
and allowance namespaces exists. The exhaustion check does not create namespaces;
unreadable, corrupt, unsupported, or explicitly required missing state remains an
error. Existing exhausted allowance records continue blocking admission even
before a canonical blocker namespace exists. Independent CI reads use the
stable current-head workflow execution identities rather than their transient
read-cycle UUID, so controllers and restarts recover the same envelope.
Review-correction claims are included in the cross-store transaction even
before their local-job envelope is transferred, and the reverse transfer also
checks unsettled CI/conflict owners.
Pending, running, and downstream-publication envelopes continue suppressing
competing work. Historical envelopes are retained when branch identity later
changes; they do not provide publication authority for the newer branch.

Admission does not invoke a backend or establish publication, CI success,
review clearance, mergeability, merge completion, or slot release. A running
job whose provider-entry outcome is unknown remains recoverable and cannot be
converted into a fresh execution merely because a controller restarted.

Focused coverage is in `tests/test_local_pr_repair_job.py` and exercises
reopened stores, genuinely concurrent offers, branch and origin changes,
CI-state distinctions, conflict admission, competing review work, and failed
persistence.

Local repair backend selection reads `[backend_for_failed_pr].order` in
`llm_config.toml`, independently of the general LLM manager. For example:

```toml
[backend]
order = ["codex-cloud"]

[backend_for_failed_pr]
order = ["codex", "claude"]
```

The first enabled candidate is captured in the job without constructing a client
or invoking a provider. A repository override replaces the order, and an explicit
empty order disables local repair admission. Unknown names, malformed orders,
and task-only remote candidates in this section are rejected during configuration
loading. When this section is absent, only the local candidates in the ordinary
backend selector are eligible, in selector order; a cloud-only ordinary selector
does not invent a local default. Re-entry never replaces a retained job's backend
or starts an additional editor because the selection policy changed.

Every reached durable admission emits `pr.repair-delegation` with
`effect=local-pr-repair-admission`, `admission_status`, `repair_kind`, `backend`,
`job_id`, and `reason`. Both admitted jobs awaiting execution and rejected offers
are `DEFERRED`; only the admitted case carries a job ID. Rejections retain a
specific operational reason in the log, action result, and dashboard facts,
including the exception type for unavailable metadata/owner evidence. These
diagnostics never imply provider entry, publication, CI success, or merge.
The real-store/configuration and mounted-detail regressions run with
`bash scripts/test.sh tests/test_local_pr_repair_admission_regression.py`.
