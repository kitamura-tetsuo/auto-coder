# Scheduled dependency updates

Dependabot monitors the root project and four nested Python projects containing
`pyproject.toml` using one cross-directory `uv` configuration. The monitored
Python paths are `/`, `/scripts/process_issues_watcher`,
`/src/auto_coder/mcp_servers/test_watcher`, `/utils/clone-issue`, and
`/utils/github-sub-issue`. It also monitors the production root
`Dockerfile` and GitHub Actions dependencies.

Regular **version updates** run weekly and are grouped into three separate
ecosystem-scoped pull request groups:

- `python-dependencies`: eligible `uv` updates across all five Python paths.
- `docker-dependencies`: eligible root Docker image updates.
- `github-actions-dependencies`: eligible GitHub Actions updates.

Each group uses a catch-all dependency pattern. The root Python configuration
continues to ignore `fastcore>=2.0.0` until the incompatible `ghapi<2.0`
constraint is resolved. Grouping ordinary version updates does not group or
disable Dependabot **security updates**, which follow GitHub's separate
security-update behavior. Not every week necessarily produces three PRs:
there may be no eligible updates, and platform compatibility constraints may
result in separate PRs. The next scheduled Dependabot run is the authoritative
check that GitHub accepts the grouping configuration; repository tests only
validate its declared structure and manifest coverage.

Dependabot only proposes pull requests. Existing CI, review, merge, and
deployment gates still determine whether updates are accepted and released,
including Python, Node, and other runtime-major updates. Grouping does not
change Auto-Coder's dependency-bot PR admission or merge policy.

The production Dockerfile pins the bootstrap `uv` container image as well as
its Python base images. Those image references are covered by the Docker
updates. `OPENCODE_VERSION` and `CODEX_VERSION` are bootstrap binary pins
rather than package-manager manifests, so Dependabot does not update them.
Updating either binary pin is a manual change and requires its separately
maintained OpenCode live/runtime verification before the new production image
is released. The installer-managed Muse CLI is not a pinned bootstrap tool.
