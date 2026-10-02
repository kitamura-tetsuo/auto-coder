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

## Fair admission and bounded waiting

Schema version 3 stores `admission_waiters` separately from sent reservations.
Tickets order blocking requests by registration, per origin, across all participating
controllers. The oldest request that satisfies the current request-kind limits
gets the next available slot. A pending mutation does not suppress an eligible
read while mutation spacing or mutation-specific budgets prevent its transmission.
Both blocking and one-shot admissions honor existing eligible tickets.

Tickets contain only attempt/incarnation identity, normalized origin, request kind,
sequence, and the original wait deadline. They are unsent and uncharged. Admission
consumes a ticket and creates a charged reservation in the same transaction.
Timeout, interruption, or a real cooldown cancels the caller's ticket; transient
cancellation contention is retried by the same participant on its next admission.
Other participants can release expired tickets, or tickets whose lifetime lock has
been released, without adding a recovery cooldown. This never releases a sent
reservation, even when its live owner is paused beyond the 90-second wait budget.

Governor DEBUG diagnostics identify queue registration (`queued`,
`admission_queue`) and release (`released`, `cancelled_waiter`, `expired_waiter`,
or `terminated_waiter`). Keep `AUTO_CODER_FILE_LOG_LEVEL=DEBUG` when investigating
contention. A queue wait that exhausts its budget reports `wait_exhausted` and a
typed definitely-not-sent deferral; it does not establish a hung network request.

Eligible local observation replacement reports `cancelled` with reason
`local_observation_available`, followed by the unsent ticket's release when one
was registered. This is not a sent-request outcome. In-flight capacity checks use
a 0.5-second fallback; completion and local webhook intake may wake them sooner.
Repeated polls neither change ticket order/deadline nor increment its sequence.
Timeout diagnostics contain measured monotonic `waited_seconds`; receiving real
cooldown evidence partway through a wait does not emit `wait_exhausted`.

## Upgrading a version-1 or version-2 store

Stop **all** older controllers sharing the runtime before upgrading to version 3.
The first upgraded controller validates existing state, adds the admission queue,
and advances the schema version atomically. Version-2 reservations, budgets,
cooldowns, and ownership remain intact; adding a queue does not recover a live
reservation. Running older controllers alongside queue-aware controllers is
unsupported because older code does not honor ticket order. Preserve the database,
its WAL files, and owner evidence; do not delete them to obtain a fresh allowance.

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
