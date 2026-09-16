# Bounded GitHub admission history reads

The GitHub governor computes active-request and rolling-window counts and earliest
eligible times in SQLite. Each admission poll returns one aggregate row, avoiding
Python allocation of the retained hour under the shared database write lock.
Explicit single-target startup also reuses its completed relationship preflight
when entering child-generation validation instead of running the same preflight twice.
Request limits, mutation spacing, cooldowns, owner-liveness recovery, corrupted
state refusal, and dashboard request-outcome semantics remain unchanged.
