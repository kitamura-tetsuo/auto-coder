# Scheduled dependency updates

Dependabot checks the root project and every nested Python project containing a
`pyproject.toml` with the `uv` ecosystem. It also checks GitHub Actions and the
production root `Dockerfile`. All checks run weekly. These checks only propose
pull requests: the normal CI, review, merge, and deployment gates remain
responsible for accepting and releasing an update, including Python, Node, and
other runtime-major updates.

The production Dockerfile pins the bootstrap `uv` container image as well as
its Python base images. Those image references are covered by the Docker
updates. `OPENCODE_VERSION` and `CODEX_VERSION` are bootstrap binary pins rather
than package-manager manifests, so Dependabot does not update them. Updating
either binary pin is a manual change and requires its separately maintained
OpenCode live/runtime verification before the new production image is
released. The installer-managed Muse CLI is not a pinned bootstrap tool.
