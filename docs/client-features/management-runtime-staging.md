# Immutable management runtime staging

`ManagementVersionStager` prepares a management-service Python environment under a
controller-configured absolute, instance-specific root. Requests contain only an
exact commit and safe attempt identifier. The stager resolves the immutable annotated
tested-source ref and revalidates its original Actions run through a trusted authority;
repository, Python, pinned `uv`, runtime profile, and protected active and rollback
environments remain controller-owned.

The stager validates the source, lock, successful source-test identity, installed
package provenance, Python environment, and the installed `auto-coder management`
serving/configuration contract. It then hashes every installed file and symlink and
atomically publishes `MANAGEMENT_READY.json`, which binds those facts and the
configuration digest to the generic immutable environment receipt. Read-only lookup
reauthenticates the source proof, revalidates installed content and configuration, and
never accepts a caller-provided executable.

The root carries an instance reservation and refuses pre-existing worker or foreign
state. Preparation is serialized and idempotent through the shared immutable environment
producer. It does not activate the result, stop or restart a process, alter OAuth or
operation stores, collect versions, access Docker, select source automatically, or
modify active and rollback environments. Partial attempts remain non-ready and
cleanup is deliberately outside this component.
