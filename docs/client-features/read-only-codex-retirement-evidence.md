# Read-only Codex retirement evidence

Codex retirement evidence is collected as an immutable, fail-closed snapshot
over an Issue-owned implementation reservation. The collector consumes the
reservation's incarnation-bound work-accounting snapshot and includes every
retained Codex Cloud attempt rather than selecting only the latest attempt.

For each attempt, the production WHAM reader must provide coherent task,
environment, current-assistant, current-user, and latest-turn status. Running,
queued, and paused states remain active; completed, failed, and cancelled are
terminal candidates; unavailable, contradictory, unsupported, or wrong-identity
data is unknown. Follow-up operations additionally require their accepted user
request to occur after the durable pre-send assistant baseline and before the
terminal assistant turn. A returned operation settlement certificate is only
evidence: collection never records settlement.

The PR candidate set is the union of durable slot membership, all PRs retained
by every bound CloudRun, the complete verified-attribution registry, native
Issue associations, retained publication heads, and strict open-PR discovery.
Each candidate is then read through strict PR metadata. Open PRs are active;
only coherent closed or merged metadata is terminal. A failed enumeration,
ambiguous attribution, malformed identity, or failed strict read is retained as
an explicit incomplete reason rather than interpreted as absence.

Each candidate-source class contributes a completeness flag and consistency
identity to the observation. Slot membership, CloudRun/publication inventory,
and verified attribution are read again before return. Changes or unavailable
revalidation make the observation incomplete, and the captured provenance lets
a later guarded retirement consumer reject evidence that became stale after
collection.

The collector is strictly read-only. In particular, it uses the attribution
registry's snapshot/get operations and never the origin resolver that may
establish a binding; it does not mutate slots, CloudRuns, work accounting,
provider tasks, GitHub entities, or publication state. This adapter does not
change admission, routing, outcomes, or event schemas, so dashboard trace and
view contracts are observability-neutral.
