# Backend routing and execution-path matrix

Source snapshot: **`0769b84eaabcf2d0c6a80f6c62660bd179003e02`** (2026-10-18 UTC). [Scope and notation](README.md).

Here "local" describes the controller-side execution/repair route or explicit-local marker. It does not, by itself, prove that model weights run on local hardware: a locally invoked client can use a configured inference transport. Model hosting, execution lifecycle and provider task ownership are different axes.

## Observed paths, not a live compatibility certification

| Work | Local/controller-side path | Cloud/task-owned path | Important limitation |
| --- | --- | --- | --- |
| New Issue implementation | Common dispatch normally accepts an `ISSUE_IMPLEMENTATION` durable job; executor and finalizer are separate | Provider adapters use their own submission/receipt tracking | Existing ownership is reconciled before choosing a new backend |
| Existing PR review correction | Explicit-local route can accept `PR_REVIEW_CORRECTION` with captured head and reserved repair allowance | Authorized provider continuation through the PR route | Explicit-local plus an exact-PR cloud binding is `CONFLICT` |
| PR CI failure | `_fix_pr_ci_with_local_llm` and related worktree/test/Actions path | Codex follow-up and provider-specific active-task handling | Not a generic local-job kind; origin, auto-fix and CI gates affect reachability |
| PR conflict/base update | `_start_mergeability_remediation` and `_update_with_base_branch` participate in repair | Applicable provider-specific continuation/remediation branches | Do not infer parity across every provider from one helper name |
| Local result publication | Issue finalizer checks artifact, non-force push, exact PR and association | Remote provider owns its task-specific implementation/publication behavior | Model exit alone is not confirmed publication |
| Independent PR validation | Pinned-head review and accepted-findings/two-tier gates | Reviewer backend selection can be separate from implementation backend | Implementing a repair does not approve that repair |
| Merge and slot retirement | Controller coordinates current-head merge and durable cleanup | GitHub supplies remote merge evidence | Command acceptance is not observed merge or released capacity |

## Review-route conflict table

This table describes review correction, not a universal provider-selection rule for all work types.

| Explicit-local marker | Applicable exact-PR cloud binding | Observed route meaning |
| --- | --- | --- |
| Present | Present | `CONFLICT`: do not silently transfer or choose an owner |
| Present | Absent | `LOCAL_REQUIRED`: use local repair admission, subject to current checks |
| Absent | Usable cloud ownership | `CLOUD`: use applicable cloud route |
| Absent | No usable route | `UNAVAILABLE`: do not invent a provider binding |

The markers include `<!-- auto-coder:local-llm -->` and `<!-- auto-coder:local -->`. Mere source-Issue history is not the cloud-binding evidence in this table.

## Temporary cloud-only operation does not migrate existing work

Choosing cloud backends for new Issues changes future selection, not an already accepted job or task. Existing explicit-local PRs and retained local jobs keep their applicable ownership/recovery rules. A policy change is not a migration receipt, and simply deleting a marker is not documented here as a safe transfer procedure.

Implementation, repair and validation configuration must be examined separately. This document records no configuration mutation and does not certify that all local starts have been disabled. The [CI candidate gate](pr-ci-and-repair.md) can prevent progress regardless of model hosting, so cloud-only operation does not by itself eliminate that failure mode.

## What remains unproven by static inspection

The existence of a CI/conflict repair function does not prove a complete Issue-to-merge local-LLM workflow. Actual evidence would require reaching it from the supported origin, producing a repair on the owned PR, observing new-head CI/review outcomes, and completing the applicable cleanup path. No such live end-to-end run is claimed in this refresh.

Sources: [Issue policy/adapters][dispatch], [local jobs][jobs], [local executor][local], [Issue publication][finalizer], [PR routing and CI/conflict branches][pr], [local review correction][correction].

[dispatch]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/issue_dispatch.py
[jobs]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/local_job_handoff.py
[local]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/issue_local_job.py
[finalizer]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/issue_job_finalizer.py
[pr]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/pr_processor.py
[correction]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/pr_correction_job.py
