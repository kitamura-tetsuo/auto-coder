# Jules autonomous execution, checkpoint and recovery guidance

`src/auto_coder/prompts.yaml`'s `cloud_provider_instructions.jules.initial`
entry (consumed by `src/auto_coder/cloud_provider_instructions.py`, see
[`cloud-provider-initial-instruction-composition.md`](cloud-provider-initial-instruction-composition.md)
for the composition mechanism itself) carries English guidance text that is
composed, exactly once as a managed component, onto every new Jules
task/session — including an authorized failed-session replacement and a
recurrent-task launch — as long as the call is eligible (`recipient="jules"`,
a brand-new task, and `no_edit=False`). It is never delivered to a Claude
Routine or Codex Cloud dispatch, never re-delivered to an existing-session
follow-up (`send_followup`/`CONTINUATION`), and never delivered to an
explicit no-edit call. It is a provider-owned literal component: it is not
copied into AGENTS.md, shared policies, or any legacy `jules.*` prompt
template, and the Claude Routine and Codex Cloud initial entries deliberately
remain empty (Issue #2092; the composition boundary itself was built by
Issue #2091).

## Why

Operators reported Jules stopping to ask routine implementation questions
(for example, where to put an internal class, or whether to run targeted
tests before the full test script) and, separately, sessions that described
restoring lost edits and rebuilding patch scripts. Workspace instability is a
reported symptom, not a proven root cause, so the guidance encourages
inspection, recoverable checkpoints and bounded recovery rather than
asserting that every edit failure is a platform bug, and it never turns into
an automatic "ok" reply to every future question.

## What the guidance actually says

The shipped text (see `prompts.yaml` for the exact wording) makes the
following distinctions, each traceable to a Requirement of Issue #2092:

* **Autonomous implementation, not a permission checkpoint.** Jules should
  inspect the repository, choose internal design/architecture/class
  placement itself, and run inspection, targeted tests, regression coverage,
  failure diagnosis/repair and the task's required final verification
  without pausing to ask routine permission. A progress report, a test
  command, or an intermediate checkpoint is not a stop for user approval.
* **Genuine escalation stays available.** Jules asks a specific question only
  when the task's own Requirements leave materially different required
  observable outcomes unresolved, and reports a blocker only when further
  safe progress needs a real user action. Either way it must name the
  concrete undecided behavior or unavailable capability, the evidence, what
  was already attempted, and the exact action needed — never an open-ended
  "anything else?" question. Existing code and review comments are evidence
  for that judgment, never authority to invent a requirement, rewrite the
  task's Objective/Requirements, or silently keep the incumbent behavior.
* **Checkpoint coherent progress, don't wait for the whole task.** When the
  task permits commits, Jules commits once its relevant targeted checks pass
  and before the next substantial change — not only once the entire task or
  full suite finishes. Before each checkpoint it inspects the actual staged
  diff, excludes unrelated edits, real credentials, and temporary patch/debug
  scripts (unless the task explicitly asks for them as a deliverable), never
  creates an empty commit to look productive, and never treats a checkpoint
  as completion.
* **Recover from facts, not a guessed platform failure.** On apparent lost,
  reverted, or corrupted edits, Jules inspects the actual working
  directory/worktree, branch/HEAD, and git status/diff/history before
  diagnosing. It recovers or reapplies only confirmed missing work, preserves
  newer or unrelated changes, and re-verifies before continuing — without
  treating every failure as an environment bug, blindly repeating destructive
  recovery, or reaching for a wholesale reset/force-push as a default remedy.
* **Checkpoint permission grants no other authority.** Any explicit
  no-edit/no-commit/assigned-branch/no-push/no-new-PR/no-close/review-merge
  restriction on the task stays fully controlling; checkpointing is skipped
  wherever it is prohibited without blocking otherwise-permitted work, and it
  never licenses switching branches, publishing, force-pushing, merging,
  weakening tests, editing the task's own contract, or manufacturing an
  "addressed" claim.
* **Precise status claims.** A local commit is not by itself proof of an
  externally preserved copy and is not guaranteed to survive loss of `.git`,
  a VM rollback, or session replacement; Jules does not push purely for
  backup without task authority. "Attempted", "passed",
  "environment-blocked", "checkpointed", and "completed" stay distinct
  states — untested or failed work is never reported as passed, and success
  is never claimed merely because an instruction was accepted, a commit was
  made, or the session chose to continue. Unresolved operational failures
  stay visible rather than triggering an unbounded retry loop.

## Non-goals

This guidance does not assert a hard guarantee about remote model behavior
or workspace durability, does not change Jules infrastructure, does not
produce automatic "ok" replies or blanket approval of every question/plan,
is not added to Claude/Codex Cloud advice, is not added to AGENTS.md, does
not runtime-enforce Git checkpoints, and does not change task
routing/retry/adjudication/merge policy.

## Test coverage

* Deterministic regressions in `tests/test_cloud_provider_instructions.py`
  and `tests/test_cloud_provider_instructions_integration.py` drive the real
  default prompt configuration and real startup composition
  (`JulesClient.start_session`, failed-session replacement in
  `jules_engine.check_and_resume_or_archive_sessions`, and recurrent-task
  launch in `jules_engine.check_and_start_recurrent_jules_tasks`) through to
  the final captured Jules HTTP payload, and assert that every REQ-002
  through REQ-007 clause quoted above is actually present in the delivered
  text (not merely a marker), that exactly one managed component is
  delivered, that the original task is preserved byte-for-byte, and that
  non-Jules, no-edit and existing-session follow-up calls receive none of it.
* Advisory, non-deterministic semantic-evaluation cases for how a real model
  reading this guidance is expected to *behave* (distinguishing routine
  implementation from a genuine specification gap, checkpointing tested
  progress, recovering from actual workspace state, respecting an explicit
  no-edit/no-commit constraint, and rejecting an out-of-scope reviewer
  demand) are registered as the `jules-autonomy-checkpoint-recovery` target
  under `prompt-evals/targets/jules-autonomy-checkpoint-recovery/`, following
  the existing opt-in/advisory Promptfoo mechanism described in
  `prompt-evals/README.md`. These are evidence for humans, not a merge,
  readiness, or repair gate, and a canned model response or text-presence
  check is never treated as proof of remote-agent compliance.
