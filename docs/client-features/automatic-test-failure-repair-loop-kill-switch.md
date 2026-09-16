# Automatic test-failure repair loop kill switch

When `automatic_test_fix` is set to `false`, Auto-Coder completely disables
automatic repair loops for local test and GitHub Actions failures:
- Auto-Coder does not invoke an LLM/backend to repair local-test or GitHub Actions
  failures, does not start or continue automatic test-fix iterations, and does not
  create repair commits or pushes caused by those failures.
- Disabling automatic test fixing does not convert a failing, timed-out,
  unavailable, or otherwise non-passing test or check result into PASS, success,
  or mergeable state; observed test/check state continues to participate in PR
  eligibility according to existing non-repair policy.
- Repeated processing of a failing PR while automatic test fixing is disabled
  does not increment automatic test-fix attempt counters or consume MAX_FIX_ATTEMPTS
  budget solely because the disabled repair path was encountered.
- Disabling automatic test fixing does not disable execution or observation of
  tests or checks when required for merge eligibility or other workflows, and does
  not disable independent PR automation such as adversarial validation, review-thread
  gating, mergeability remediation, or auto-merge policy.
- Bypassing automatic repair does not discard, reset, revert, or destructively clean
  an existing workspace or branch state; it stops cleanly before repair-owned mutation.
- When `automatic_test_fix` is re-enabled, a failing PR resumes ordinary repair
  policy with its genuine configured attempt budget unaffected by the disabled interval.
