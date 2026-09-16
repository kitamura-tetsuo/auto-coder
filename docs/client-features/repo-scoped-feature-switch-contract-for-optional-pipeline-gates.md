# Repo-scoped feature-switch contract for optional pipeline gates

Auto-Coder exposes canonical, repo-scoped boolean feature settings for optional
pipeline gates and self-repair loops:
- `issue_specification_validation`: Controls whether individual Issue specification validation runs before implementation admission. Individual reviews perform ambiguity closure and next-review prediction; caller-reconciled child reviews additionally check material cross-Issue dependencies, propagation, invalidation, evidence reuse, and stale-result authority without treating related prose as normative.
- `issue_decomposition_validation`: Controls whether parent/child decomposition validation runs before sub-issue implementation admission.
- Decomposition reviews perform ambiguity closure and next-review prediction over
  the exact caller-supplied parent/direct-child set. They separately trace
  membership and member-content mutations through identities, individual and
  set evidence, invalidation, readiness, delayed completion, and implementation
  authority, including transitive parent/child graph consumers. These checks
  remain evidence constrained and do not turn implementation freedom, shared
  ownership, prose-only relationships, grandchildren, or hypothetical future
  lifecycles into blockers.
- `pr_adversarial_validation`: Canonical switch for pre-merge adversarial review validation.
- `pr_review_thread_gate`: Controls whether unresolved PR review threads gate PR processing and merge.
- `automatic_test_fix`: Controls whether automatic test-failure repair loops execute.

Each switch defaults to `true` when unconfigured. Effective values are resolved
with global-versus-repository precedence (`~/.auto-coder/<owner>/<repo>/config.toml`
overrides `~/.auto-coder/config.toml`).
Settings can be declared under the `[features]` table or at the top level of `config.toml`.
Configured values must strictly be booleans (`true` or `false`); any non-boolean
value raises an explicit configuration error identifying the invalid setting.
`AUTO_CODER_ENABLE_ADVERSARIAL_VALIDATION` remains supported as an environment-variable
compatibility override for `pr_adversarial_validation`, and
`AutomationConfig.ENABLE_ADVERSARIAL_VALIDATION` stays synchronized with the effective
`pr_adversarial_validation` value.
