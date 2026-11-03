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

For journaled follow-ups whose provider response does not echo the internal
request ID, the collector uses the retained prepared-message SHA-256 and native
turn graph. Complete history must contain the retained assistant baseline,
exactly one user child with that exact message, and its terminal assistant child.
Wrapper and nested identities and parent links must agree; missing ancestors,
cycles, competing requests, duplicate identities, incomplete pagination or an
unrelated terminal response remain unknown. Mapping order and task-level
completion alone do not establish request completion. Repeated matching input
anywhere in the returned history remains ambiguous, even after another terminal
assistant. The original prepared message cannot be matched by selecting only
the first child of the retained baseline. The resulting certificate
retains both native user and assistant identities; it never rewrites the original
delivery journal or sends another request.

Exposed child lists are checked against every native parent edge, not merely
searched for the expected child. Foreign, omitted or contradictory edges are
unknown. When a historical journal lacks producer metadata, its exact preserved
native request may still prove the supported existing-PR contract; the candidate
handoff alone never supplies publication/tracking completion. The collector
does not write that contract into the original source record.

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
