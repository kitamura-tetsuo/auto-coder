# Advisory Python 3.14 compatibility evidence

The independent `Python 3.14 Compatibility (Advisory)` GitHub Actions
workflow runs for pull requests changing its workflow or shared dependency
policy inputs, manual dispatch and a weekly check. Ordinary application-only
pull requests do not incur this separate, potentially expensive advisory job. It
uses the same supported `astral-sh/setup-uv@v7` and pinned uv `0.7.22`
as the required Python 3.12 workflows. Its `uv sync --locked --dev --extra
test` installs the committed dependency graph without regenerating the
lockfile. Stale or incompatible dependencies, missing Python 3.14 wheels,
source-quality failures, and test failures remain visible red job outcomes.
A failed or incomplete run is not compatibility evidence.

The workflow verifies the selected interpreter and clean exact Git checkout,
then runs Black, isort, Flake8 and package-wide mypy before the complete
`not opencode_live` pytest selection. Like the required source workflow, it
prepares the Python-only management Docker fixture from the tested source and
verifies real headless Chromium before running the tests. These prerequisites
are required to avoid incorrectly reporting success with eligible tests
deselected or skipped for missing fixtures; they do not run OpenCode live
tasks. The fixture's isolated container Python is not the interpreter under
evaluation: Python 3.14 is selected for the installed application and host
pytest process.

This independent workflow is **advisory**, not a publication, merge or
deployment authority. It publishes no artifact, grants no package write
permission and does not alter any required Python 3.12 workflow, Docker
base image, production worker, release selection or branch-protection rule.
Green advisory evidence alone is not approval to migrate the production
baseline. See `immutable-ci-uv-setup.md` for the shared immutable-lock gate.
