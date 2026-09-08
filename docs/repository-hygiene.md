# Repository hygiene

The repository hygiene policy keeps disposable files out of commits and makes the
small set of maintained files at the repository root explicit. The authoritative
checker is `scripts/check_repository_hygiene.py`; its allowlist is
`scripts/repository_hygiene_allowlist.json`.

## Where files belong

- Put disposable investigation and debugging scripts (for example,
  `check_adc.py`), one-shot rewrite scripts, and their outputs under
  `.agent-tmp/`. They must remain untracked and must not be committed.
- Put maintained command-line helpers and other maintained scripts under
  `scripts/`.
- Put formal regression tests under `tests/`.
- Never create a Python file in the repository root. A legitimate new
  non-Python root configuration file needs an explicit, reviewed addition to
  `scripts/repository_hygiene_allowlist.json`.

Do not evade the policy by force-adding ignored files, bypassing hooks, disabling
the checker, or adding disposable artifacts to the allowlist. Remove a violation,
move maintained content to its proper directory, or submit a reviewed allowlist
change for a legitimate root file. If an artifact was already tracked, `.gitignore`
does not untrack it; remove it from the index as well.

## CI checks the committed tree

The `Lint & Type Check` job in the `PR Tests` workflow runs, after checkout and
dependency installation:

```console
scripts/check_repository_hygiene.py --source head --repo .
```

Head mode reads both the complete path set and the allowlist from `HEAD`. It
therefore detects violations even if local hooks were not installed or were
bypassed. Missing policy files, invalid policy data, and Git inspection failures
also fail the job. Untracked temporary files are not part of `HEAD`, so CI does not
reject them.

## Pre-commit checks the staged index

Install the repository hooks after installing development dependencies:

```console
uv sync --dev
uv run pre-commit install
```

On every commit, the local hook runs the equivalent of:

```console
python scripts/check_repository_hygiene.py --source index --repo .
```

Index mode reads the complete staged tracked state and the allowlist from the
index. It does not depend on the filenames passed by pre-commit and runs even when
no Python file changed. Consequently, deleting a staged violation only from the
working tree does not make the hook pass: stage that deletion before retrying.
Likewise, an unstaged allowlist edit cannot authorize staged content.

`.gitignore` and pre-commit are supplementary safeguards. Ignore rules make normal
staging of known disposable paths less likely, but cannot excuse an already
tracked or force-added violation. Hooks are local and can be absent or bypassed;
the CI head check remains the shared tracked-state check.

## Server-side enforcement boundary

CI integration alone does not prevent every merge or direct-push path. GitHub-side
merge enforcement requires the actual `PR Tests / Lint & Type Check` status context
to be configured as required in branch protection or a repository ruleset. During
the 2026-09-07 investigation, the `main` branch was not protected, required status
checks were disabled, and no repository rulesets were configured.

The `Update Version` workflow also commits and pushes directly to `main`. That
direct-push behavior must be reconciled before strict server-side protection is
enabled. This integration does not change branch protection, rulesets, bypass
permissions, or direct-push/merge policy.
