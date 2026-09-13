# Shared GitHub governor operations

Auto-Coder controllers on one Linux host may coordinate GitHub traffic through one
physical local runtime directory. Give every participant read/write access and set
`AUTO_CODER_RUNTIME_ROOT` to that directory (different bind-mount path spellings are
allowed). The governor database remains
`<runtime-root>/github/request_governor.sqlite3`; its SQLite WAL files and the
`github/owners` lifetime-lock directory must all be shared. Network filesystems,
multiple hosts, and independent runtime copies are not supported.

Each running controller holds a kernel-backed lifetime lock for a random incarnation.
This is not a lifetime-wide global governor lock: controllers continue to participate
concurrently, while database transactions serialize only state transitions. A paused
controller retains its lock and admission protection. When a process terminates, a
survivor detects the released lock during its next admission evaluation, retains the
uncertain attempt's budget charge, and commits the one-time recovery cooldown before
allowing another send. Missing or inaccessible ownership evidence closes admission
instead of treating the request as an orphan. A controller that loses database
coordination after admitting work keeps its lifetime lock: coordination failure is
not evidence that its active transports terminated.

First-use setup is serialized only across the short initialization window, beginning
before SQLite WAL configuration and ending after schema validation or migration. A
participant that encounters transient SQLite lock contention remains fail-closed and
retries initialization on later admission calls using the same governor instance;
corrupt, incompatible, invalid, and inaccessible stores remain permanently closed.

## Upgrading a version-1 store

Stop **all** controllers that can write the version-1 store before starting upgraded
Auto-Coder. Overlapping legacy writers are unsupported. The first upgraded controller
validates the legacy schema and data, then atomically adds incarnation ownership and
conservatively recovers every legacy ownerless unresolved admission. Charges, logical
clock checkpoint, mutation spacing, cooldown, and throttle episode state are retained.
If validation or migration cannot commit, admission remains closed and the existing
store must not be deleted or replaced to obtain a fresh allowance.

## Transient outcome persistence contention

A response whose reservation transaction cannot acquire the SQLite write lock is
retained by the controller. Its durable reservation remains unresolved and its
lifetime lock remains held. Subsequent admissions retry persisting that response
before sending any more requests; throttle evidence is applied before admission
is reconsidered. A controller restart uses the existing orphan recovery policy.
Other persistence failures continue to close admission. Do not delete the shared
store to recover from contention.
