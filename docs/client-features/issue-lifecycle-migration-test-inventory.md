# Issue lifecycle migration test inventory

The maintained `issue_lifecycle_contract` pytest group is the production-origin
regression boundary for extracting Issue execution and recovery code. It is not
a live-provider E2E suite: provider and GitHub edges are controlled, while the
production candidate, ownership, dispatch, durable-store, local runner,
publication, and later PR-gate boundaries consume those responses.

## Selection and supported origins

Run the bounded group through the repository test wrapper:

```console
bash scripts/test.sh -m issue_lifecycle_contract tests/test_issue_lifecycle_contract.py
```

The group enters both supported production origins: the ordinary durable
Implementation lane and explicit single-Issue processing. Ordinary cases use
the common candidate path through real generation acquisition and dispatch;
the explicit case enters `process_single`, traverses unified generation
acquisition and durable local dispatch, and stops only at the completion
consumer after proving the retained job can pass real provider-entry authority.
The group reuses the integration fixtures and file-backed SQLite/JSON stores
owned by the focused test modules rather than substituting preselected results
at the contested boundaries.

## Verification revision

The integrated boundary was inspected and locally verified at revision
`0769b84eaabcf2d0c6a80f6c62660bd179003e02`, after the execution, adapter,
retry, local-job, and repository-runtime extractions. Normal GitHub Actions
results belong to the published PR head and are not inferred from this local
record.

## Authoritative assertions

The remote cases hold an actual dispatch claim at an observable barrier and
prove that competing cloud and local work cannot enter. Normal cloud admission
also traverses common-candidate generation acquisition and selected Jules
dispatch, then reopens both the dispatch journal and ownership store to observe
the exact accepted attempt/session and retained provider membership after the
controller execution has ended. The local cases assert that
`LOCAL_ACCEPTED` only transfers the retained execution: the common candidate
call returns while the job remains pending, no PR is reported at acceptance, and the
same execution proceeds through result recording and publication. A later PR
route still independently applies validation and merge gates.

Negative controls refresh authoritative Issue state immediately before
dispatch, reject a changed generation without entering a provider, and suppress
an already-owned generation after routing-store recovery. Dispatch receipts
also distinguish a positively confirmed `NOT_STARTED` release from an
indeterminate or accepted attempt, so only the former advances candidate
selection.

The composed startup probe enters `AutomationEngine.start_automation`, holds ordinary producers, workers, and the local Issue runner behind a failed-and-retried enumeration barrier, then proves accepted retry reconciliation and local publication resume on the same controller. It also reopens the job store, preserves a retained job for another repository, and proves repeated scheduling retains one operation identity.

Recovery probes reopen decisive stores. Accepted Codex retry replay retains its
request, task, run, environment, branch, URL, and PR identities without a
second provider submission. A cross-provider promotion case pauses an older
writer at an event barrier while a newer accepted request is promoted. Local
publication recovery uses a temporary real Git repository and remote; replay
after a lost create response retains the job/execution identity and observes
one push and one PR effect rather than repeating publication.

## Coverage limitations

The provider and GitHub boundaries are controlled and therefore provide no
live-provider or production-GitHub evidence. The local publication scenarios do
use real temporary Git repositories. Semantic-review fixtures establish READY
state before entry but do not bypass the generation refresh or ownership
boundary under test. The group is intentionally a migration safety net rather
than an exhaustive replacement for focused module tests or the normal CI suite.

The focused verification commands are:

```console
bash scripts/test.sh -m issue_lifecycle_contract tests/test_issue_lifecycle_contract.py
bash scripts/test.sh -m pr_lifecycle_contract tests/test_pr_lifecycle_contract.py
```

This inventory and group add no runtime behavior, trace producer, or dashboard
event, so the production-to-dashboard observability contract is unchanged.
