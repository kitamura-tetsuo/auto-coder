# PR CI-failure repair: focused verification, CI full validation

When a local PR's current-head CI failure is admitted for repair, Auto-Coder
repairs it from the reported failures and submits the result to CI for full
validation. It does not run the repository's full test suite locally.

For an operator-requested post-merge local test run,
`AUTO_CODER_DEFER_LOCAL_TESTS=1` also defers focused local checks. The initial
correction and publication still run; no local test success is claimed and
actual GitHub CI remains required for merge. Regression:
`tests/test_ci_repair_focused_verification.py::test_operator_deferral_publishes_correction_without_local_test_claim`.

- Initial processing and already-on-the-PR-branch (resumed) processing follow the
  same path: the initial CI-log correction always runs with all available failed-check
  diagnostics, and each local corrective invocation (initial and follow-up) runs under
  `invocation_admission.bind_ci_repair_designation()`, so the automatic unscoped
  workspace baseline is omitted, including across automatic backend fallback.
- The known-failure set F is every distinct failed test file identified by the
  production extraction path from the CI diagnostics. The local failed-test cache is
  never consulted. Non-test failures and targets that cannot run locally stay in the
  corrective prompt.
- After corrective edits every locally runnable file in F is run through the target
  repository's `TEST_SCRIPT_PATH` with that file as explicit argument
  (`ci_repair_verification.verify_targets`). The complete F is re-run after each
  follow-up correction, so an earlier pass is never reused for a changed working state.
  The runner is never called without a selector in this workflow.
- A target is `passed` only when its explicit-file run executed and succeeded. A missing
  file, an unsupported per-file selector, a launch failure or timeout, a "no tests ran"
  result, or empty F is reported as `unverified` with its reason; it never counts as a
  pass, never triggers a full run, and never creates a follow-up correction by itself.
  The aggregate "passed locally" report describes F only and requires every file in F to
  have passed on the latest corrected state.
- Follow-up local corrections (using the still-failing focused result) share one budget
  of `MAX_FIX_ATTEMPTS` per invocation of the workflow across all of F; the initial
  CI-log correction is not counted. An unbounded configuration stays unbounded, but a
  follow-up that produces no correction stops the loop.
- After the loop stops, a real non-empty working-tree diff is committed and pushed via
  `git_commit_with_retry`/`git_push` if `automatic_test_fix` is still enabled and the PR
  is still open, regardless of local full-suite state. The report states the submitted
  candidate separately from the focused results, and that full validation is pending CI
  on the new head. No diff, a closed PR, a commit failure, or a push failure is never
  reported as a submission.
- Local results and push success confer no CI clearance or merge authority; admission,
  review, and merge gates are unchanged, and a new current-head CI failure is the input
  to the next admitted repair.
- The `pr.github_actions_fix` and `pr.local_test_fix` prompts tell agents that CI performs
  full validation, list the failed test files, request focused checks only, and require
  unavailable verification to be reported as such.
- The standalone `fix-to-pass-tests` command, ordinary implementation bootstrap, and
  review/validation execution are unaffected; with `automatic_test_fix` disabled no
  correction or publication starts.
