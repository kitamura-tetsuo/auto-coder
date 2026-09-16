# Individual Issue specification validation kill switch

When `issue_specification_validation` is set to `false`, Auto-Coder completely
bypasses individual Issue specification validation at the candidate admission
boundary:
- No individual validation jobs are scheduled, joined, or executed for candidates.
- Candidates proceed directly through the remaining pipeline gates without
  requiring an individual specification validation READY verdict.
- Bypassing validation does not rewrite, delete, or convert existing durable
  specification records (such as BLOCKED, ERROR, or REISSUE_REQUIRED states).
- The bypass produces no synthetic validation records, PR/Issue comments, label
  mutations, or implementation attempt increments.
- Non-validation admission gates (author allowlist, implementation-ready label,
  open state, parent/child relationships, explicit sibling dependencies, and slot
  ownership) remain strictly enforced.
- Disabling individual validation does not consume scheduler capacity or block
  other enabled validation categories, such as parent/child decomposition validation.
- When the switch is re-enabled, the authoritative current generation resumes
  normal validation lifecycle enforcement.
