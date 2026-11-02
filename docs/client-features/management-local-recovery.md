# Local management-service recovery

The immutable bootstrap can accept an explicit recovery request from a local
operator with write access as the bootstrap's Unix owner. The Supervisor state
directory must be owned by that user and must not be group- or world-writable;
the private mailbox is owned by that user and has mode 0600. OAuth credentials
are not accepted as recovery authority. The installed bootstrap itself must be
updated by an operator through the ordinary manual deployment process.

The management configuration supplies the existing bootstrap configuration;
the bootstrap fixes the instance, endpoint and runtime. An operator who has
inspected the current Supervisor incarnation and selected child record submits
an immutable UUID and those exact expected identities. The existing private
child ledger provides the values, for example:

```sh
sqlite3 /management/bootstrap-state/management-child.sqlite3 \
  'SELECT incarnation,pid,start_token FROM supervisor_presence; SELECT record_json FROM child_state;'
```

Use the current presence incarnation and the child record's `child_generation`,
`selected_runtime`, `runtime_receipt` and `source_receipt`. The Supervisor independently verifies
these expectations and the process evidence. Then submit:

```sh
auto-coder management recover --config /management/config.yml \
  --request-id 00000000-0000-4000-8000-000000000001 \
  --expected-supervisor SUPERVISOR-INCARNATION \
  --expected-generation 1 --expected-runtime SELECTED-RUNTIME \
  --expected-receipt sha256:EXECUTABLE-RECEIPT \
  --expected-source-receipt SOURCE-RECEIPT

auto-coder management recovery-status --config /management/config.yml \
  --request-id 00000000-0000-4000-8000-000000000001
```

For the initial profile, the source receipt can be empty; omit
`--expected-source-receipt` when the retained value is empty. For replacement
profiles, supply the exact retained source receipt.

The fixed mailbox is `management-recovery.sqlite3` in the configured bootstrap
state root. A retained mailbox identity in the child ledger prevents a missing
or replaced mailbox from being silently initialized again after a restart.
An inaccessible mailbox with a retained identity blocks self-update effects
until an operator reconciles the retained state. A running owner will not
recreate a missing child ledger, and reconstruction refuses when a retained
recovery, handoff, operation, or OAuth store exists without its child ledger. Request acceptance (`pending`), a durable one-attempt claim
(`claimed`), process launch (`launched`), and authenticated application
readiness (`succeeded`) are distinct. `refused` means no launch was admitted;
`claimed` or `unknown` after interruption means the effect is unresolved and
must not be retried under a different key. Replay the same request ID with the
same expectations to read its original status. A conflicting replay is refused.

The owning Supervisor checks existing child, operation and handoff records
without initializing missing stores, checks the retained previous Linux lifetime
and listeners in its own configured namespace, and verifies the already
selected runtime receipt. Missing or inconsistent evidence, a live previous
lifetime, a listener, an unfinished action or a changed owner or runtime
prevents admission. This is a conservative point-in-time listener observation:
a competing bind after the check may still win; such an outcome remains
non-success and is not retried. The journal, OAuth data, handoffs, versions and
retained child history are not reset.

A launched request exposes a non-secret `observation_id` through local status.
After the application has reconciled startup, a currently authorized OAuth
client calls `auto_coder_whoami` with that ID. The Supervisor separately checks
the fixed endpoint's unauthenticated challenge and consumes the fresh
launch-bound readback described in `management-authenticated-readback.md`.
The OAuth caller only demonstrates application usability; it cannot admit,
select, stop or launch a process. A missing response, expired observation,
failed bind or unavailable proof never supplies readiness or authorizes retry.
While a recovery is claimed, launched or unresolved, the Supervisor holds
physical self-update effects; it resumes them only after a successful recovery.
