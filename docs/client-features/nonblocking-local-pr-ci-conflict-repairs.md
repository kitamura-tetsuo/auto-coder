# Non-blocking local PR CI and conflict repairs

Eligible locally owned CI-failure and merge-conflict repairs are admitted as durable local jobs at the existing PR routing boundary. Admission returns a typed handoff containing the exact job, head/base, repair kind, and implementation execution; it does not claim that editing, tests, publication, review, or merge completed.

The ordinary PR worker releases its transient processing and checkout scopes after admission while the durable job retains the resolved logical owner's implementation execution. The repository-scoped local runner later performs model work in its private workspace, and the repair finalizer publishes exact-head artifacts, retires that execution while preserving lifetime ownership, and wakes a fresh PR evaluation. Cloud-owned work, dependency bots, unsupported Jules routes, and green-CI review/merge deferrals retain their existing routing. The supported explicit checked-out-local Jules exception is retained in the immutable job input and revalidated by the runner instead of being flattened into ordinary local ownership.

A missing or contradictory durable ownership read is fail-closed: the execution is retained rather than being retired while a committed handoff may exist. Repeated explicit observations, including `--force`, attach to the retained job and execution identity instead of attempting another admission, while ordinary observations use the stable current-head operation identity and cannot infer success from an offer.

Stale-process cleanup preserves execution IDs owned by any unsettled local job,
even after the accepting controller exits. If an older controller already removed
a pending PR repair's execution record, explicit processing can restore that same
ID under the existing idle lifecycle owner. Restoration requires a retained exact
PR claim, no provider entry or execution incarnation, no competing execution or
provider session, and matching standalone-PR identity or retained PR membership.
It cannot recreate a missing lifecycle owner or overwrite another owner. Unknown
entered work remains deferred for reconciliation. The runner still revalidates
the original head/base and current routing before editing; attachment is not new
repair authority or permission to publish a stale candidate.

Before explicit reattachment, a complete strict PR metadata read compares the
retained head/base repository, branch, and commit identities with the current
target. A changed identity retires only a pending, positively unentered job with
no execution incarnation or continuation. An atomic state check refuses retirement
if a runner claimed it concurrently. The controller retires that job's execution,
preserves lifetime ownership, and continues ordinary current-target admission in
the same command. Current routing, CI, and allowance gates still apply. Unavailable
or incomplete metadata and entered or uncertain work never authorize replacement.
This handles changed authoritative PR metadata. A base branch tip advancing
while PR metadata still advertises the admitted B preserves the existing job;
its workspace fetches B by SHA, as described in
[retained workspaces](pr-repair-job-workspaces.md). Pending results
include the retained preparation/authorization diagnostic in the displayed reason.
Supersession emits the existing admission stage with `admission_status=superseded`,
the retired job ID, and `provider_started=false`; it does not claim repair success.

Retained attachment emits `pr.repair-delegation` with
`effect=local-pr-repair-admission`, `admission_status=retained`, the original
backend and job ID, and `execution_restored`. Refused attachment emits `deferred`
with its operational reason and cannot fall through to a fresh editor. Coverage
and the mounted dashboard regression are in `tests/test_local_pr_repair_recovery.py`.

The production runner reconstructs CI repairs from the complete immutable offer, including the nested failed-check snapshot captured at admission. Terminal `CANNOT_FIX` and stale-target outcomes settle that exact edit claim and retire only its execution; lifetime ownership and normal retry authorization remain intact for a later independent PR evaluation.

A settled envelope for the same head/base/failure evidence is never replayed as a contradictory offer. An ordinary evaluation reports a `deferred` admission naming the settled job, its outcome and diagnostic, and that a new head or `--only <PR> --force` is needed to retry. An explicit `--only <PR> --force` run admits a successor envelope with a deterministic, distinct operation identity (the evidence incarnation is unchanged), and repeated forced runs after further settled attempts receive further distinct identities. Current routing, CI, allowance, and competing-owner gates still apply to the successor. Coverage is in `tests/test_local_pr_repair_job.py`.

Standalone explicit PR processing immediately drives only its accepted job through the production local runner composition. It may wait for that exact job, but it never drains unrelated retained work. Successful H2 publication durably records a repository-scoped PR invalidation before acknowledging the wake, and the handoff remains deferred until fresh PR validation establishes the post-publication outcome.
