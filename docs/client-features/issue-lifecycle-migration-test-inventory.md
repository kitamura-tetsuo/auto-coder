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
the explicit case traverses the real local runner and publication finalizer.
The group reuses the integration fixtures and file-backed SQLite/JSON stores
owned by the focused test modules rather than substituting preselected results
at the contested boundaries.

## Authoritative assertions

The remote case holds an actual dispatch claim at an observable barrier and
proves that competing cloud and local work cannot enter, then reopens the store
and observes the accepted provider identity. The local cases assert that
`LOCAL_ACCEPTED` only transfers the retained execution: the queue worker can
return while the job remains pending, no PR is reported at acceptance, and the
same execution proceeds through result recording and publication. A later PR
route still independently applies validation and merge gates.

Negative controls refresh authoritative Issue state immediately before
dispatch, reject a changed generation without entering a provider, and suppress
an already-owned generation after routing-store recovery. Dispatch receipts
also distinguish a positively confirmed `NOT_STARTED` release from an
indeterminate or accepted attempt, so only the former advances candidate
selection.

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

This inventory and group add no runtime behavior, trace producer, or dashboard
event, so the production-to-dashboard observability contract is unchanged.
