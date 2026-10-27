# Locked production container environment

The production Dockerfile installs Auto-Coder and all Python runtime dependencies
from the committed `uv.lock`. Its `uv sync --locked --no-dev --no-editable` build
step creates a non-editable production environment and fails instead of resolving
or rewriting the lock when `pyproject.toml` and `uv.lock` disagree.

The final image copies that complete environment without running a second Python
package installer. Its `PATH` selects the locked environment for the `auto-coder`
entrypoint, direct Python execution, and build-provenance embedding while retaining
the pinned OpenCode and Codex executables and the existing multi-architecture image
build.

Static regression coverage checks the Dockerfile contract. The dedicated
`opencode_live` selection verifies the prepared production image's installed
distribution versions and provenance, and runs the real dependency stage against
an intentionally stale lock to prove that image construction fails without
modifying the lockfile.
