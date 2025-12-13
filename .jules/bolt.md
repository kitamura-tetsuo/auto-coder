## 2025-12-13 - [GitPython vs Subprocess for Git Info]
**Learning:** Replacing `gitpython` with `subprocess` for simple git checks (`is_git_repository`) resulted in a latency regression (0.3ms -> 3.2ms) due to process spawning overhead, even though it removed a heavy dependency.
**Action:** When replacing libraries with subprocess calls, be aware of the latency trade-off. For high-frequency calls, keeping the library might be faster if it avoids spawning processes. However, for startup time and dependency reduction, removing the library is better.
