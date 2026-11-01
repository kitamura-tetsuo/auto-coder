# Management authenticated readback

The management child can record observation-only evidence when a fresh authenticated
`auto_coder_whoami` call supplies a 32-character lowercase hexadecimal observation
ID. The observer generates a new unpredictable ID for each bounded observation and
passes it to the protected tool. After independently checking the unauthenticated
challenge, it calls `read_authenticated_readback` with that ID and the start and end
of the observation interval. An absent result means unavailable, including missing,
stale, conflicting, corrupt, inaccessible, or unpersisted proof. A protected
response alone does not guarantee proof was persisted.

The private `management-readiness.sqlite3` record contains the instance and fixed
listener, Supervisor/child lifetime identity, launch nonce, executable and source
receipts, executing application and management-configuration paths and digests, non-secret OAuth project, user,
client, grant and resource, the observation ID and observation time. It contains no
bearer, refresh, or Firebase token. The writer runs only after startup reconciliation
and successful current OAuth admission. The reader checks the current Supervisor,
Linux process lifetime, configured runtime receipts and actual command line again.
It accepts only proof from the supplied interval and ID, bounded by the configured
verification timeout. A new launch or request cannot reuse earlier evidence.

The initial configured executable is checked against its executable receipt without
requiring a self-update handoff or candidate READY receipt. Selected replacement
profiles retain their configured source or installed-runtime verification. Direct executables and scripts with a fixed absolute interpreter are supported.
For `env` shebangs, the executable must resolve through the child's configured
PATH and match procfs; unsupported `env` options produce unavailable evidence. Application readback, the HTTP challenge,
and startup/journal reconciliation remain separate facts. This contract does not
start or stop a process, select a runtime, authorize recovery, or grant the OAuth
user any process-control authority.
