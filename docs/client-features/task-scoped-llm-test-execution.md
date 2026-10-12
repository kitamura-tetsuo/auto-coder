# Task-scoped LLM test execution

Every editable implementation, correction, and verification prompt delivered to a
local or remote LLM carries the same task-scoped test policy. Agent-initiated runs
must select tests for the changed behavior, directly affected integration or
dependency boundaries, and the regression being added or repaired. A relevant
focused integration or end-to-end test may live outside the edited directories.

Before executing a command, the agent must establish that the repository runner
honors its explicit file, case, or otherwise justified bounded affected-test
selection. It must not run the complete repository suite, reproduce it by
iterating files, packages, shards, or agents, or fall back to an unfiltered wrapper
whose selection behavior is unknown. CI retains responsibility for full-suite
execution. If focused verification is unavailable, the agent preserves required
regression-test implementation and truthfully reports the specific check as not
run and why.

The policy is composed by `prompt_loader` into initial Issue work, label-selected
and custom prompts, PR repair and validation operations, and Codex Cloud
continuations. Existing-session repair payloads receive it directly rather than
assuming their original bootstrap included a later policy revision. Backend
execution wrappers preserve the composed task, so configured aliases and fallback
selection do not change the instruction.

Label selection retains the policy of the originating operation even when a
configured mapping selects a custom or otherwise non-governed template key. Jules
pause recovery also attaches the current continuation policy immediately before
the `sessions/:sendMessage` transport, so sessions created before a policy revision
cannot resume under bootstrap guidance alone. Custom resume text is preserved and
the current policy is appended.

The automatic workspace-failure and test-stability repair prompts use the same
composition path. Their task-specific instructions request only the explicit
failing test and justified affected isolation boundaries; they never retain a
conflicting directive to rerun the complete suite.

This instruction does not grant execution authority. Explicit no-test, no-shell,
or no-edit controls remain stricter, and controller-owned test scheduling, CI
workflows, merge gates, retry authorization, provider lifetime, and timeouts are
unchanged. Semantic prompt evaluation is advisory; deterministic final-payload
tests establish instruction delivery, not model compliance.
The registered `task-scoped-test-execution` prompt-evaluation target separately
exercises focused integration selection, forbidden suite reconstruction through
shards, and an unscopable wrapper as advisory semantic evidence.
