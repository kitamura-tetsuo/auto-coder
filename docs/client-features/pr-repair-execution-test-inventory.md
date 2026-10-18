# PR repair execution test inventory

The permanent `pr_repair_contract` pytest group characterizes the supported
PR-repair origins at revision `007e97cb8be80ccc98e8c4a2604b3b084c23d156`.
It is a controlled production-boundary regression suite, not a live-provider
E2E claim. Run it through the repository test wrapper:

```console
bash scripts/test.sh -m pr_repair_contract
```

## Origin and boundary map

| Origin or decision | Admission boundary | Execution and result-consumption boundary | Maintained coverage |
| --- | --- | --- | --- |
| Ordinary candidate collection | `AutomationEngine._get_candidates` and `select_pr_progress` | `_process_single_candidate_unified` into `_handle_pr_merge` | `tests/test_pr_lifecycle_contract.py`, `tests/test_pr_progress_policy.py` |
| Explicit PR processing | `_handle_pr_merge` current-head CI and mergeability checks | `_start_mergeability_remediation`, CI repair dispatch, and typed processing outcome | `tests/test_pr_lifecycle_contract.py`, `tests/test_pr_mergeability_remediation.py` |
| Local CI failure | `current_ci_failure_authority` and automatic-fix/origin gates | synchronous local test/fix, Git commit/push evidence, and `PRRepairResult` consumption | `tests/test_ci_repair_authority.py`, `tests/test_pr_lifecycle_contract.py` |
| Local conflict | mergeability remediation and local-origin selection | real temporary Git conflict/private workspace, publication, and typed applied/failed result | `tests/test_local_conflict_workspace.py`, `tests/test_cloud_conflict_delegation.py` |
| Existing cloud conflict owner | cloud-origin resolution and durable delivery reservation | send/reconcile/replay receipt and deferred or accepted typed result | `tests/test_cloud_conflict_delegation.py`, `tests/test_pr_lifecycle_contract.py` |
| Jules CI or conflict owner | existing session and retirement admission | provider continuation through typed repair dispatch | `tests/test_pr_lifecycle_contract.py`, `tests/test_cloud_conflict_delegation.py` |
| Base update | branch preparation and commits-behind check | no-op, clean merge/push, Git failure, dependency-bot exclusion, cloud delegation, or unresolved local conflict | `tests/test_pr_mergeability_remediation.py`, `tests/test_cloud_conflict_delegation.py`, `tests/test_local_conflict_workspace.py` |

The group keeps the exact-head terminal CI-failure positive control alongside
changed-head, pending, known-empty, unavailable, exhausted-allowance, and
automatic-fix-disabled negative controls. In particular, an affirmative
conflict with enabled remediation remains eligible when its complete
current-head observation is `KNOWN_EMPTY`; that fact grants neither CI-failure
repair authority nor merge authority.

Persistence-sensitive cloud cases use the production delivery store in a
temporary directory. Git-sensitive cases use temporary repositories and the
private-workspace integration. The lifecycle cases include observable
head-change and durable-send ordering checkpoints and same-controller reentry
after a temporary wait. External GitHub and provider transports remain
controlled, so their successful execution must not be described as live E2E.

## Preserved behavior and limitations

These regressions preserve the existing repair behavior and process-exit
policy. A clean base merge is complete only after publication; the currently
terminal second push failure still propagates `SystemExit` and is not reported
as a successful repair. Local test success, provider request acceptance, action
wording, and `ACTION_FLAG` presentation artifacts do not establish current-head
CI success or permission to merge.

The action strings and flags are recorded because production still consumes
some of them. Their removal belongs to later extraction work. This inventory
does not introduce a new local job type, cloud journal, provider policy, test
exclusion, skip/xfail, or CI threshold change. Possible redesigns of terminal
push failure and unreachable manual workflow dispatch are out of scope.
