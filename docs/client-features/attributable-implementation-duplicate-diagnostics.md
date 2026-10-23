# Attributable Implementation duplicate diagnostics

When ordinary Issue processing suppresses a duplicate automatic start for an
already-owned semantic Implementation generation, the CLI, worker result, and
execution trace explicitly say that the durable start is not proof of a
successful implementation. The explanation reads existing local-job evidence
without changing ownership, dispatch, retry, publication, or scheduler state.

Evidence is restricted to the exact repository, Issue, and generation. Each
reported entry retains its attempt and job identity and describes invocation
state separately from downstream publication. A completed local result is
unpublished unless the exact job incarnation has a confirmed PR association;
even a confirmed PR is not described as merged or independently validated.
Failure reasons are bounded and redact credential-shaped values.

An active implementation execution selects its bound job. A sole attributable
historical job is unambiguous; multiple historical attempts without that
binding remain history and leave the current outcome unknown. Missing, legacy,
contradictory, or unreadable evidence never becomes success or failure and
never bypasses duplicate suppression.

The diagnostic supplies the exact `process-issues --only ... --force --retry`
command for a distinct operator-requested attempt, while explaining that it
does not guarantee session/edit resumption or bypass readiness, authority,
ownership, dependency, or provider gates. Live work must be inspected and
reconciled before replacement is considered.

An authoritatively bound paused job is reported as `invocation=paused` with a
bounded reason and the exact `--only <issue> --resume` command for its retained
edits and session. Its diagnostic does not recommend a distinct `--retry` attempt.
