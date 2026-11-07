# In-container immutable worker versions

`WorkerVersionPreparer` stages an exact, full Git commit in a side-by-side Python
3.12 environment. Its configuration (source repository, persistent version root,
Python executable, pinned uv executable/version, and runtime profile) is trusted
controller configuration; a preparation request supplies only a full source SHA and
a safe attempt identifier. Runtime preparation never accepts a repository URL,
package, branch, command, or destination from an MCP caller and never uses Docker.

The bootstrap image supplies uv 0.7.22 and Git. Operators must mount a writable,
channel-private persistent version root with enough space, and arrange repository
authentication and network access before preparation. Missing system libraries or
an unsupported Python/native dependency require a future bootstrap-image update;
the preparer does not install system packages or modify system site-packages.

Each attempt gets a stable directory containing its checkout and `venv`. Installation
uses that revision's unchanged `uv.lock` with `uv sync --frozen --no-editable`.
Attempts are serialized across processes. A failed or interrupted attempt is retained
but cannot become ready or be reused; retrying uses a new attempt ID. Existing READY
versions are retained and this stage performs no garbage collection.

`READY.json` schema 1 is published by an atomic rename only after the installed
environment reports its embedded source revision and distribution version. The
receipt binds the resolved commit, absolute environment and interpreter, SHA-256 of
the lock bytes actually installed, runtime profile, attempt ID, installation time,
installed observation, a stable opaque runtime ID, and a content identity over those
bindings. Every read re-hashes the retained lock and invokes the installed runtime;
it also verifies that the imported `auto_coder` module and its distribution root
remain inside the recorded environment and that distribution metadata does not mark
the installation editable. Directory existence, matching provenance values, or
installer success alone is never readiness.

Preparation deliberately does not select a channel, change an executable pointer,
stop or start a worker, activate an environment, grant management authority, or
modify a release reference. Activation and bounded retention cleanup belong to a
later authorized lifecycle component. Recovery is limited to verified reuse of an
unchanged READY attempt; partial, corrupt, conflicting, or unverifiable attempts are
reported without repair in place.

READY readback verifies both the imported package and the provenance module's
resolved origins within the recorded environment in the same interpreter that
produces the provenance observation. A provenance-module symlink to the source
checkout or a checkout package selected by the working directory cannot pass
through a separate isolated origin probe and then supply the accepted observation.
Such origin mismatches use the existing `VERIFICATION_MISMATCH` failure path.
