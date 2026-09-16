# PR review thread gate kill switch

When `pr_review_thread_gate` is set to `false`, Auto-Coder completely bypasses
unresolved PR review-thread gating during PR processing and merge evaluation:
- Unresolved PR review threads do not block internal merge eligibility, lower
  PR candidate priority, or trigger automatic review-thread repair/reply cycles.
- Failure or unavailability of review-thread enumeration, GraphQL detail lookups,
  provenance resolution, or thread-gate evidence does not cause the PR to be
  deferred or failed solely because that evidence is unavailable.
- Review-thread lookup paths are avoided when no other independently enabled
  feature requires thread data; when another enabled feature (such as adversarial
  validation) requires thread data, it reads review threads according to its own contract.
- Disabling `pr_review_thread_gate` does not disable `pr_adversarial_validation`;
  if adversarial validation remains enabled, its authoritative current-HEAD verdict
  may still block or reprioritize the PR according to its own contract.
- Existing review threads remain unmodified, unresolved, and untouched on GitHub
  (they are not resolved, dismissed, deleted, or edited).
- GitHub branch protection, required approving reviews, required status checks,
  mergeability enforcement, and merge rejections returned by GitHub remain strictly enforced.
- When `pr_review_thread_gate` is re-enabled, normal review-thread gating resumes
  using the authoritative current review-thread state on GitHub without carrying
  forward fake resolutions from the disabled interval.
