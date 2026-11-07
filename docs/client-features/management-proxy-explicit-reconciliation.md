# Explicit reconciliation of an already attached management proxy

The host watcher normally refuses a durable `stopped` record if the manager's
PID/start time/network namespace changes. Automatic tick remains conservative.
An operator who independently restores the exact configured proxy can explicitly
observe its correct attachment using the existing `--reconcile` command.

After approved deployment of this watcher code, stop only the watcher controller
service, preserving its config, exclusive lock and complete journal. Do not stop
management, proxy or workers for this observation. Run the verified installed
interpreter, not a mutable checkout:

```sh
python -I -B -m auto_coder.management.proxy_watch_cli \
  --config /protected/watcher/config.json --reconcile
```

It acquires the same exclusive watcher lock. A running second controller refuses;
never delete a lock file or journal to get past contention. Exact manager/proxy
IDs, image digests, Caddy invocation, readonly unshadowed config digest, repeated
current process lifetime and namespace observations must match. The proxy must
already be running in the new manager's namespace. A `stopped` old lifetime is
then preserved in an immutable `proxy-resolution-<prior-digest>.json` before the
journal advances to `done` for the observed lifetime. No stop/start is dispatched.
The manager ID/PID/start tuple establishes its process lifetime. A namespace
change alone with the same tuple is refused, even if the proxy already shares
that namespace. A reused PID with a genuinely different start remains a new
lifetime, subject to all the other binding and repeated-observation checks.

`ATTACHMENT_OBSERVED` permits restarting the same watcher controller. Verify its
`ATTACHED` output and independently check authenticated MCP. Namespace attachment
alone is not application readiness or worker/source deployment proof.

Resolution bytes are written and fsynced under a private staging name before
atomic no-overwrite publication. Abandoned staging files cannot block a fresh
retry. An interruption before publication or before the journal transition can
be retried with identical observations; conflicting final evidence still refuses.
Changed observations, wrong namespace, stopped proxy, unknown effects belonging
to another lifetime and malformed records remain `NEEDS_ATTENTION` or unavailable.
Do not replace their evidence. Existing same-lifetime uncertain-effect observation
is unchanged. Rolling back watcher code does not change the preserved journal
schema, but an unresolved old `stopped` case will again require operator attention.

This host-only observation emits existing watcher statuses and changes no worker
scheduler, provider or dashboard trace contract.
