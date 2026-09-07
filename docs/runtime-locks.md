# Runtime coordination locks

Auto-Coder keeps repository coordination files under
`$AUTO_CODER_RUNTIME_ROOT/locks/<owner>/<repo>/`. If the variable is unset or
empty, the runtime root is `~/.auto-coder/runtime`. Configuration and durable
JSON/CSV/SQLite state remain at their existing locations. Lock names include a
digest of the resolved absolute state-file path, their purpose, and (for keyed
operations) the logical key.

Every process sharing protected state must use the same physical runtime lock
tree and consistent canonical state paths. In particular, processes with
different home directories must set `AUTO_CODER_RUNTIME_ROOT` to the same
shared directory. The implementation-slot namespace uses group-readable and
group-writable files and setgid, group-traversable directories; provision the
runtime root and state directory with the same shared group.

## Upgrade and rollback

This change intentionally does not support mixed versions. For either an
upgrade or rollback, stop **all** Auto-Coder processes that access the shared
state, replace the version, and only then restart them. There is no dual-path
locking or automatic lock migration.

After every process has stopped, legacy sidecars may be removed offline. Remove
only sidecars corresponding to the selected stores:

* `implementation_slots.lock` and `implementation-<kind>-<number>.lock`
* `cloud_runs.json.lock`
* `adversarial_validation_attempts.lock` and
  `adversarial_validation_attempts.transition.lock`
* `specification_validations.<identity-or-repository-state>.lock`
* `decomposition_validations.<identity-or-repository-state>.lock`
* `individual_review_history.lock` and
  `decomposition_review_history.history.lock`
* `reissue_required.lock` and `specification_repair_rounds.lock`

Inspect the selected state directories and delete those exact recognized
sidecars; do not recursively delete `*.lock`. In particular, preserve
`.git/auto-coder.lock`, dependency lockfiles, SQLite coordination files,
deployment ownership-registry locks, unrelated lock files, durable state, and
the new runtime tree. Leaving legacy sidecars in place is also safe after the
stop-and-replace upgrade: the replacement neither reads nor recreates them.
