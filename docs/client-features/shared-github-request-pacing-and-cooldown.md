# Shared GitHub request pacing and cooldown

Each running controller applies one synchronized admission policy per normalized
GitHub API origin across repositories, clients, workers, and credential roles.
The local protective ceilings are one in-flight request, 300 network attempts per
rolling 60 seconds, 60 mutations per rolling 60 seconds, and 400 mutations per
rolling hour, with one second between mutation completion and the next mutation.
These are conservative local ceilings, not a guarantee of GitHub allowance.
Durable GitHub-dependent obligations preserve repository/entity, processing stage,
semantic revision, independent unfinished effects, and the next eligible time across
restart. Actual throttle retries are bounded to three retries after the first response;
local admission refusals do not spend this budget, while authentication, forbidden,
indeterminate-delivery, and exhausted work remain visible operational blocks. Manual
workflow dispatch claims use holder tokens so only the current holder can publish a
definite non-send/rejection outcome; stale holders cannot unlock or overwrite a newer
claim. The supported manual retry route is to change the semantic input revision or
remove the blocked row from ~/.auto-coder/github_pending_work.db after correcting the
operational cause; neither route bypasses the shared governor.

Fresh primary, secondary, and GraphQL throttle evidence closes the origin-wide
gate using Retry-After, reset, and exponential local cooldown evidence; cache-only
reads and non-GitHub origins do not consume or alter these budgets. Work that
cannot enter immediately receives a typed, definitely-not-sent deferral carrying
the reason and earliest known retry time, allowing webhook and local work to
continue without quota sleeper tasks.

The two kinds of deferral are separated at the request boundary. Deferrals the
governor imposes on itself -- one in-flight request, mutation spacing, and the
rolling attempt/mutation windows -- resolve without any GitHub cooperation, so a
request waits for its stated eligibility (bounded, currently 90 seconds) and is
then sent, rather than failing a concurrent worker's read. Throttle cooldowns and
unusable governor state are not waited out: they propagate immediately as typed
deferrals to the callers that own durable resumption for them. Exhausting the
wait budget emits a `wait_exhausted` governor diagnostic and re-raises the
original deferral.

An active request schedules a fallback admission check after 0.5 seconds rather
than repeatedly polling at the 0.01-second floor. Completion and local webhook
notifications wake same-instance waiters immediately. Other controller processes
still discover released capacity within the bounded fallback interval. Repeated
polls preserve the original ticket without another attempted AUTOINCREMENT insert.
`wait_exhausted` records the measured monotonic elapsed time and is emitted only
when the local wait budget actually expires; a transition to real cooldown does
not report a fictitious 90-second timeout.

Callers may cancel an unsent observation GET when usable local Issue evidence
arrives during admission waiting. This is scoped to the requesting operation and
exact repository/Issue resource, excluding mutations and relationship/list reads.
The governor checks before polling and again before creating a charged reservation,
removes the unsent ticket, and emits `cancelled/local_observation_available`.
Cancellation creates no network response, quota charge, throttle retry, or recovery
cooldown, and never releases another request's live reservation. The caller must
recheck its observation before using it; advisory data never authorizes execution.
Unavailable cancellation evidence leaves the ordinary authoritative read enabled
and does not mark the governor's shared coordination state unavailable.

Blocking requests register ordered, unsent admission tickets in the shared SQLite
store. When origin capacity becomes free, the oldest currently eligible ticket
has priority across threads and controller processes; a completion owner or a
new one-shot `admit` caller cannot reclaim that slot ahead of it. Waiting for
another ticket reports `admission_queue`. A mutation waiting for spacing or its
mutation-specific rolling ceiling does not block reads, but takes priority once
eligible. Origin cooldowns, the global attempt ceiling, and active reservations
still gate every send. A ticket is not a network attempt and spends no quota.

The 90-second wait budget uses monotonic time. Tickets retain their original
deadline and position across polls, and are removed atomically on admission or
on cancellation, throttle deferral, or timeout. Expired unsent tickets and tickets
whose owner lifetime has ended can be released without a request-recovery
cooldown. A live or paused owner's sent reservation never expires with its ticket.
Queue registration and release emit secret-safe governor diagnostics; exhaustion
still returns a definitely-not-sent deferral for normal durable resumption.

Admission reservations, rolling budgets, mutation-completion spacing, throttle
episodes, and cooldown deadlines are durably recorded in the controller-wide
`~/.auto-coder/runtime/github/request_governor.sqlite3` store. Restart recovery
uses per-controller Linux lifetime locks: another live or paused controller retains
its reservation, while a released lifetime is recovered once on a survivor's next
admission evaluation. Recovery conservatively retains the attempt charge and adds
an origin-wide cooldown. Controllers for separate repositories share the same
per-origin limits when their `AUTO_CODER_RUNTIME_ROOT` values name the same physical
local directory; corrupt, incompatible, indeterminate-ownership, or unwritable
state fails GitHub admission closed. Persisted records and safe diagnostics contain
no credentials or request content. A participant that loses persistence after an
admission retains its lifetime evidence so another participant cannot recover a
possibly active transport. See `docs/github-governor-operations.md` for the supported
topology and schema upgrade procedure. Schema version 3 adds admission tickets;
stop all older controllers sharing the runtime before upgrading, preserving the
existing database and owner files.
Explicit `--only` target resolution uses strict reads and reports local governor
deferrals with their reason and retry deadline instead of reporting a missing
target. Reservation write-lock contention is retryable on the same participant;
outcome write-lock contention retains the completed response in memory and its
durable unresolved reservation until persistence succeeds, blocking subsequent
sends in the meantime. Other outcome persistence failures still close admission. State failures include the
underlying cause and database path in the console log.
Concurrent first use is protected by a short initialization lock that covers SQLite
WAL setup and schema creation. Transient SQLite lock contention never authorizes a
send or permanently poisons that participant: the same governor retries on subsequent
admission calls and joins the completed shared store, while invalid durable state
continues to fail closed.
