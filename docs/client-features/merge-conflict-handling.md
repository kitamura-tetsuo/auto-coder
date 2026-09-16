# Merge Conflict Handling

  mergeability_check:
    description: "Decides whether merging the base branch into a PR would degrade code quality before conflicts are resolved."
    implementation: "check_mergeability_with_llm in src/auto_coder/conflict_resolver.py"
    behavior:
      - "For regular PRs the LLM is asked once and must answer SAFE_TO_MERGE or DEGRADING_MERGE; unclear or missing answers are treated as unsafe."
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
