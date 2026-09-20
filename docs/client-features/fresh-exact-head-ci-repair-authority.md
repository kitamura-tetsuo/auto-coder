# Fresh exact-head CI repair authority

Every Codex Cloud continuation and Jules feedback request triggered by CI is
admitted again at its actual provider-mutation boundary. The admission bypasses
cached PR metadata, requires the PR to remain open at the proposed head, and
uses one complete production checks-and-workflows observation whose selected
facts are settled and include a current terminal failure. Explicitly older
workflow attempts, empty or unavailable reads, pending or unknown facts, healed
CI, and changed or closed pull requests cannot start repair.

The final observation and first outbound effect share the process-local CI
authority barrier. A webhook invalidation accepted before that effect fences the
observation, while a request that already crossed the boundary is reported as a
historical delivery. Refusals are retryable/deferred and do not publish a
fix-requested receipt. Each later initiation, including resumed work, performs a
new strict read rather than inheriting an earlier failure or persisted intent.
