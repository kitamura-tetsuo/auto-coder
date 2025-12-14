## 2025-05-24 - Optimistic Git Commands
**Learning:** Instead of verifying remote branches and calculating merge-bases explicitly (3-4 commands), we can optimistically try `git log origin/base..HEAD`. If it fails, we fall back. This reduces process spawning by 50%+.
**Action:** When dealing with git operations, prefer optimistic execution and fallback over "check then execute" patterns.
