# Immutable CI uv setup

Every active GitHub Actions job that installs the checked-out Python project's
dependencies uses `astral-sh/setup-uv@v7` with uv `0.7.22`. The common pin keeps
source tests, live-test jobs, repository administration, and image publication on
the same supported dependency resolver.

CI dependency admission uses `uv sync --locked`. A stale `pyproject.toml` and
`uv.lock` pair, or a locked distribution that cannot be installed, therefore
stops the job before tests or publication and does not rewrite the committed
lockfile. Regression coverage discovers project-installing workflow jobs so a
new workflow cannot silently bypass the shared version and immutable-lock policy.
