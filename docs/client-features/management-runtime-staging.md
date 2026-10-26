# Immutable management runtime staging

`ManagementVersionStager` prepares a management-service Python environment under a
controller-configured absolute, instance-specific root. Requests contain only an
exact commit, safe attempt identifier, and the immutable tested-source readback;
repository, Python, pinned `uv`, runtime profile, and protected active and rollback
environments remain controller-owned.

The stager validates the source, lock, successful source-test identity, installed
package provenance, Python environment, and the installed `auto-coder management`
entry point. It then atomically publishes `MANAGEMENT_READY.json`, which binds those
facts to the generic immutable environment receipt. Read-only lookup revalidates the
underlying environment and entry point and never accepts a caller-provided executable.

Preparation is serialized and idempotent through the shared immutable environment
producer. It does not activate the result, stop or restart a process, alter OAuth or
operation stores, collect versions, access Docker, select source automatically, or
modify active and rollback environments. Partial attempts remain non-ready and
cleanup is deliberately outside this component.
