# Parent/child decomposition validation kill switch

When `issue_decomposition_validation` is set to `false`, Auto-Coder completely
bypasses parent/child decomposition validation at candidate and parent processing
boundaries:
- No decomposition validation jobs are scheduled, joined, or executed for
  `implementation-ready` parent/child sets.
- Eligible child issues proceed through the remaining pipeline gates without
  requiring a READY decomposition verdict.
- Bypassing decomposition validation does not rewrite, delete, or convert
  existing durable decomposition-validation records (such as BLOCKED, ERROR,
  or REISSUE_REQUIRED states).
- Existing durable decomposition records do not block or authorize child
  implementation while the switch is disabled.
- The bypass produces no synthetic decomposition validation records, comments,
  label mutations, or implementation attempt increments.
- Non-decomposition admission rules (authoritative parent/child reconciliation,
  parent open state, child membership, sibling implementation ordering, parent
  `implementation-ready` eligibility, individual specification validation, and
  implementation slot ownership) remain strictly enforced.
- Disabling decomposition validation does not consume validation scheduler
  capacity or prevent enabled validation categories (such as individual Issue
  specification validation) from using the scheduler normally.
- When `issue_decomposition_validation` is re-enabled, Auto-Coder resumes the
  ordinary decomposition lifecycle using the authoritative current parent/child
  set identity, and existing durable evidence for the unchanged generation is
  again enforced.
