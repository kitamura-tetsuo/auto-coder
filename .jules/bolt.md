## 2025-05-24 - Optimistic Git Commands
**Learning:** Instead of verifying remote branches and calculating merge-bases explicitly (3-4 commands), we can optimistically try `git log origin/base..HEAD`. If it fails, we fall back. This reduces process spawning by 50%+.
**Action:** When dealing with git operations, prefer optimistic execution and fallback over "check then execute" patterns.

## 2025-12-15 - Heavy Dependencies at Startup
**Learning:** Importing `GitPython` (even just `import git`) takes ~130ms. For CLI tools, start-up time is critical. Using `git` subprocess commands is just as fast at runtime and avoids this start-up penalty.
**Action:** Prefer `subprocess` calls to `git` CLI over `GitPython` for simple read operations like checking remote URLs or repo status.
