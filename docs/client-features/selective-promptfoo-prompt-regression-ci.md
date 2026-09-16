# Selective Promptfoo prompt-regression CI

  selective_prompt_regression:
    description: "Provides selective, human-only advisory prompt evaluations without changing ordinary CI or repair decisions."
    implementation: |
      prompt-evals/registry.json, prompt-evals/run_prompt_evals.py,
      .github/workflows/prompt-regression.yml,
      .github/workflows/prompt-regression-report.yml
    behavior:
      - "The active Prompt Regression (Advisory) workflow runs on pull-request opened, synchronize, and reopened events and on workflow_dispatch. It is separate from PR Tests and must never be configured as a required check. Before activation, an authorized operator must recheck branch protection and rulesets and remove any legacy prompt-regression required context; this repository does not mutate those server-side controls or bypass permissions. Deploy the controller-side advisory isolation first, then activate these workflows."
      - "The versioned registry associates stable target IDs with dot-addressable production prompt dependencies, target-local Promptfoo configuration, and corpus globs."
      - "CI compares dependency values between the pull request base and head, so independent prompt changes select only their targets while a declared shared dependency selects all and only its dependents."
      - "Target config and corpus changes select their owner; selector, registry, and either active evaluation/reporting workflow change selects every target, while unrelated changes select none and malformed selection metadata fails closed."
      - "No selected target and selected targets with no executable cases are NOT_RUN with a reason before credentials or Promptfoo are needed. Fork PRs are also NOT_RUN because provider credentials are never exposed to untrusted PR content; selected nonempty same-repository work with missing credentials is ERROR."
      - "PASS requires at least one fully completed case and all assertions passing; complete semantic failures are MISMATCH; selection, preparation, authentication, provider, transport, timeout, cancellation, runner, parsing, missing-result, and partial-execution failures are ERROR; intentional absence is NOT_RUN. ERROR takes precedence over partial semantic evidence. The direct runner keeps nonzero exits for MISMATCH and operational errors."
      - "PR runs evaluate the event's exact base/head commits. Manual workflow_dispatch accepts optional base and head refs, defaults head to the dispatched revision and base to that resolved head's first parent, and fails rather than substituting another revision. Reports include both full SHAs and upstream run/attempt identity."
      - "Every terminated evaluation is followed by the trusted Prompt Regression Advisory Report workflow. Actions summaries and the prompt-regression-report artifact retain structured outcomes and evidence. Authoritatively associated MISMATCH/ERROR attempts receive one immutable top-level github-actions[bot] PR comment per upstream run attempt; PASS/NOT_RUN-only attempts do not. Delivery denial or uncertainty is diagnostic only and is not blindly retried."
      - "The report marker and identity bind repository, PR, head SHA, upstream run ID/attempt, and evaluation workflow path. Reporting runs only default-branch code with a write token, validates GitHub API provenance and destination, and treats downloaded report/model values solely as display data; evaluation itself has contents-read permission only."
      - "Promptfoo and its Node tooling remain isolated in CI and are not Auto-Coder application dependencies."
