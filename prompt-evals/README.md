# Prompt regression evaluations

This directory is the stable home of CI-only Promptfoo evaluations. The initial
registry deliberately has no targets or validation cases, so CI exits without
installing or invoking Promptfoo and without requiring provider credentials.

## Registering a target

Add an entry to `registry.json`:

```json
{
  "id": "pull-request-review",
  "prompt_dependencies": [
    {"path": "src/auto_coder/prompts.yaml", "keys": ["pr.review", "shared.safety"]}
  ],
  "config": "prompt-evals/targets/pull-request-review/promptfooconfig.yaml",
  "cases": ["prompt-evals/targets/pull-request-review/cases/*.yaml"]
}
```

Dependencies without `keys` select the target whenever the file changes. YAML
dependencies with dot-addressable keys are compared at those keys between the
base and head revisions. Declaring the same shared key for multiple targets
therefore fans a shared change out only to those targets. Changes beneath a
target's config path or matching one of its case globs also select it.

Selected targets with no matching case files are reported and skipped before
Promptfoo is invoked. Selected targets with cases run their own config with the
version pinned in `run_prompt_evals.py`; any Promptfoo failure fails the job.
Registry, runner, or workflow changes select every registered target, making
evaluation-infrastructure changes fail closed rather than silently omitting a
suite.
