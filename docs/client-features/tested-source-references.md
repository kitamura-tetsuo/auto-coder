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
logs. The legacy `Publish Beta` image publication path and historical registry tags
remain available for bootstrap and operator use. Both complete source-test paths
prepare the locked Python-only management fixture before their unchanged test
selection; this is not an OpenCode live-runtime preparation or image publication.
The fixture is exported only after immutable Docker ID and installed source/version
readback match the clean checkout. Missing Docker, failed build or mismatched
provenance blocks the gate rather than skipping its readonly-bind test.

Nonfatal scheduled pytest diagnostics use an owned Python thread and the synchronous
all-thread faulthandler API while holding the GIL. This avoids the demonstrated
native watchdog race with freed code metadata on the supported Python runtime.
The configured Source 60-second and ordinary-shard 30-second intervals, exit
behavior, complete selections, assertions, retries and hard deadlines stay intact;
workflow files and production code do not change. Native fatal-signal handling
and the original pytest descriptor/handler restoration remain enabled.

Each timer owns a duplicate output descriptor and is cancelled and joined before
completion, interactive exceptions or debugger entry can release it. Diagnostic
errors fail visibly. Explicit disabled/default-zero settings remain unchanged.
A Python thread can be delayed while native code holds the GIL; absence of a
timely dump never proves a pass, and the existing test/CI hard failure deadlines
still apply. Diagnostic output is neither Source qualification nor runtime
recovery proof. The actual main Source/publication runs and installed worker
verification remain separate requirements.

The configured `faulthandler_exit_on_timeout=true` path delegates to pytest's
original native timeout protocol, including prompt exit while native code holds
the GIL. Only nonfatal scheduled dumps use the Python-owned timer; this change
does not replace a hard deadline with a GIL-dependent Python thread. A real
`ctypes.PyDLL` regression holds the GIL past the deadline and checks that the
process exits before the native call completes.
