# Prompt regression evaluations

This directory is the stable home of selective Promptfoo evaluations. Live
semantic results are advisory evidence for humans and are never ordinary CI,
merge, or automatic-repair prerequisites.

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

Selected targets with no executable cases are reported as `NOT_RUN` before
credentials are needed or Promptfoo is invoked. Selected targets with cases run
their own config with the version pinned in `run_prompt_evals.py`; a complete
semantic failure is `MISMATCH`, a complete success is `PASS`, and operational,
partial, or malformed output is `ERROR`. The CLI remains nonzero for mismatch
and error even though controller policy treats the workflow as advisory.
Registry, runner, or workflow changes select every registered target, making
evaluation-infrastructure changes fail closed rather than silently omitting a
suite.

Run directly with explicit revisions:

```console
python prompt-evals/run_prompt_evals.py --base <commit> --head <commit> --report report.json
```

Actions pull-request runs bind to the event's immutable base/head. Manual runs
accept `base` and `head`; omitted base means the resolved head's first parent.
Fork PR content never receives model credentials and is reported `NOT_RUN`.
Every attempt retains a structured artifact and Actions summary. `MISMATCH` and
`ERROR` attempts associated with exactly one PR may also receive an immutable
`github-actions[bot]` top-level comment. The comments state that no
acknowledgement or resolution is required.

Deploy controller advisory isolation before activating these workflows. Neither
workflow context may be configured as required. An authorized operator must
recheck branch protection/rulesets and remove legacy required contexts before
activation; this code intentionally does not modify those server-side settings.
