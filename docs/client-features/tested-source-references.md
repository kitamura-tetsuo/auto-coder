# Tested source references

Every successful `Test Source for Runtime` run on `main` can publish one immutable
annotated tag named `auto-coder-tested-<full-source-SHA>`. This workflow is
independent of Docker image publication. It uses Python 3.12 and the locked
`uv.lock` input, checks Black and isort without rewriting, runs Flake8 and
package-wide mypy, launches a real headless Chromium, and executes the complete
pytest selection excluding only `opencode_live`.

Pull requests also run that same complete selection against the exact PR head;
the source-quality job checks GitHub's merge commit. PR runs provide validation
evidence only and cannot publish a tested-source reference. Publication remains
restricted to successful push runs on `main`.

The tag annotation is bounded canonical JSON with schema version, repository,
full source SHA, distribution version, lock hash, Python version, originating
workflow ID/run/attempt/path, the fixed `source-required-v1` profile, and the
successful result. A tag alone is not eligibility evidence: consumers must also
read the tag object and direct commit and verify the recorded Actions run and
both required jobs are completed successfully for that exact attempt and SHA.

Only the completion-triggered publication workflow has `contents: write`; test
jobs have read-only contents permission. The fixed workflow repository, CLI
destination, and checked-out GitHub `origin` must all identify the same
repository before a Git-data client is created. Publication is create-only.
Existing same-SHA tags are reused only after strict tag, annotation, commit, run,
and job readback. Conflicting data is never overwritten. If a create response is
lost, only exact readback can establish success; absent or unavailable evidence
remains uncertain.

To investigate a conflict or unavailable result, inspect the recorded Actions
run and attempt, both required job conclusions, `git/ref/tags/<name>`, and its
`git/tags/<object-id>` object through the GitHub API. Do not delete or force-move
the ref. Credentials and API response bodies are not placed in annotations or
logs. The legacy `Publish Beta` image workflow and historical registry tags are
unchanged and remain available for bootstrap and operator use.
