# Merge Conflict Handling

Local LLM PRs (including the explicit `auto-coder:local-llm` marker) resolve
conflicts in one editable LLM execution without a separate analysis-only call.
Before invoking the backend, the controller stages the current merge files,
including their conflict markers, so the independent private repository can
capture a stage-zero index. The caller retains its merge operation; the backend
edits files and runs tests without changing Git history or lifecycle state.
After handoff the controller stages the repaired files, checks changed working
files for markers even when the index is staged, and commits and pushes only
when none remain. `CANNOT_FIX`, leftover markers, and failed publication are
repair failures, never successful remediation or evidence of quality degradation.
Only a confirmed push produces `pr.mergeability-remediation` `COMPLETED`;
failed local repair produces `FAILED`, without closing the PR as a degrading merge.
Real Git/private-clone regressions are in `tests/test_local_conflict_workspace.py`;
single-execution and production outcome coverage are in
`tests/test_conflict_resolver.py` and `tests/test_cloud_conflict_delegation.py`.

Cloud conflict repair serializes its journal and provider send under a shared
repository lock. A concurrent sender causes an immediate deferral rather than
waiting while holding an implementation-owner lock. Codex repairs persist a
stable logical delivery identity for the repository, PR, head, base, and task.
After interruption, a pending receipt is reconciled with the Codex provider
journal while this lock excludes a live sender. Confirmed delivery repairs the
local receipt without another POST; definite non-delivery permits the existing
guarded send path; indeterminate delivery remains blocked. A pending legacy
receipt without a logical identity, or a provider without a typed delivery
reader, still requires operator verification and is never blindly resent by
`--force`.

The dashboard's existing `pr.mergeability-remediation` stage reports
`ACCEPTED_HANDOFF` only after a confirmed send or provider receipt, and `DEFERRED`
when delivery remains unconfirmed. Lock contention and unknown receipt state
must not appear as a successful repair. Runnable regressions are in
`tests/test_cloud_conflict_delegation.py` and `tests/test_codex_work_fence.py`.

  mergeability_check:
    description: "Decides whether merging the base branch into a PR would degrade code quality before conflicts are resolved."
    implementation: "check_mergeability_with_llm in src/auto_coder/conflict_resolver.py"
    behavior:
      - "Local LLM PRs bypass this analysis-only check and perform safety assessment and repair in the same editable execution."
      - "The legacy Jules path uses SAFE_TO_MERGE or DEGRADING_MERGE; unclear or missing answers are treated as unsafe."
      - "Dependency-bot PRs never reach this check because their conflicts are not resolved at all (see dependency_bot_conflict_skip)."
      - "Jules PRs (google-labs-jules[bot]) are not treated as dependency bots and still go through the LLM check."

  dependency_bot_conflict_skip:
    description: "Merge conflicts of dependency-bot PRs are never resolved by auto-coder."
    implementation: |
      _perform_base_branch_merge_and_conflict_resolution in src/auto_coder/conflict_resolver.py,
      _update_with_base_branch / _resolve_pr_merge_conflicts / _merge_pr in src/auto_coder/pr_processor.py
    behavior:
      - "Dependency-bot PRs (Dependabot/Renovate/'[bot]' logins, detected via _is_dependabot_pr) are detected before any conflict resolution starts."
      - "When a conflict is detected on such a PR the in-progress merge is aborted with 'git merge --abort' and the routine returns without calling the LLM."
      - "_resolve_pr_merge_conflicts fetches the PR details first and returns False for dependency-bot PRs before checking out the PR branch."
      - "_merge_pr does not attempt conflict resolution when an unmergeable PR is authored by a dependency bot."
      - "Rationale: dependency bots recreate or rebase their PRs against the updated base branch themselves, so resolving conflicts locally only wastes LLM calls and can clobber the bot's own update."
      - "Jules PRs (google-labs-jules[bot]) are not treated as dependency bots and keep their existing conflict handling."
