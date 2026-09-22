# Unified ordinary backend selection

Ordinary Issue implementation uses one repository-scoped `[backend]` policy
for local and remote backend aliases. `priority_groups` is an ordered array of
non-empty arrays; quota strategy ranks only members of the same group and can
never promote a later group. `order` is the shorthand in which every entry is
its own group. Repeated normalized aliases are considered only at their first
position, while separately named aliases remain separate candidates even when
they resolve to the same backend type.

An explicitly empty `order` or `priority_groups` is an empty pool and starts no
implementation. `default` is used only when neither selector is present, and
the built-in fallback is `codex` only when `default` is also absent. Disabled,
quota-ineligible, and invalid candidates are never replaced with Jules or an
unlisted default.

The old top-level `backend_cloud` key is a configuration error, including an
empty or disabled table and including a key inherited from the base file. This
is an immediate breaking cutover: files are neither rewritten nor merged into
the new policy. Move inline provider settings to a named declaration and then
place that alias in `[backend]`, for example:

```toml
[backend]
priority_groups = [["remote-a", "local-b"], ["remote-c"]]
default = "local-b" # fallback only when neither selector key is present

[backends.remote-a]
backend_type = "codex-cloud"
environment_id = "example-environment"

[backends.local-b]
backend_type = "codex"
```

Repository overrides recursively inherit unmentioned scalar and table values,
but replace arrays as a whole. Consequently, inheriting `backend.order` while
adding `backend.priority_groups` is invalid; the base policy must remove the
conflicting selector. Dedicated high-score, no-edit, and adversarial-validation
policies remain independent of this ordinary pool.

When no dedicated no-edit policy is present, synchronous message generation
inherits only synchronous-capable aliases from the ordinary pool; task-only
cloud aliases are filtered without changing the remaining order. An all-cloud
inherited pool reports that no synchronous backend is available and does not
launch a task. Startup likewise checks only the first candidate's required
capabilities, so an earlier remote handoff is not blocked by an unused local
fallback executable or test script.

`process-issues` startup applies the same synchronous-only projection to the
general LLM manager it bootstraps for ad hoc synchronous prompts (conflict
resolution, generic fallback prompts, and similar call sites reached through
`get_llm_backend_manager()`), independently of the message/no-edit manager
above. This manager, like the message manager, is never the ordinary
per-Issue dispatcher: that dispatcher re-reads the full ordinary pool from
configuration on every Issue and routes a task-only remote candidate (Codex
Cloud, Claude Routine, or Jules) straight to its own asynchronous provider
adapter, never through a synchronous client. Building the general manager
from the *full* ordinary pool would therefore both be unnecessary and, for a
task-only remote type such as Jules, fail outright at startup. When the
ordinary pool has no synchronous candidate at all (for example, an
all-Jules policy), both the general and message/no-edit manager singletons
are simply left uninitialized rather than resurrecting an unlisted default
backend; ordinary Issue dispatch is unaffected and proceeds normally to the
selected remote candidate's provider adapter.

Both manager singletons are process-lifetime state, so a long-lived daemon
process that serves more than one repository (or is reconfigured between
runs) must never let a manager bound to an earlier repository or an
obsolete effective configuration answer a later synchronous call. Startup
therefore always rebinds a present synchronous manager with
`force_reinitialize=True` rather than silently keeping whatever the
singleton already held, and explicitly clears (`LLMBackendManager.
reset_singleton()` / `reset_noedit_singleton()`) any singleton left over
from a previous bootstrap when the current pool's synchronous projection is
empty. A later `get_llm_backend_manager()`/`get_noedit_backend_manager()`
call in that state raises rather than silently running with the wrong
repository's settings.

Building either manager also eagerly constructs its selected candidate's
client (for example an `OpenCodeClient`, which probes its CLI executable at
construction time). Since neither manager is on the path to ordinary
dispatch, `process-issues` startup isolates that construction failure: it
logs a warning, leaves the corresponding singleton uninitialized (clearing
any stale one), and continues. An earlier ordinary candidate that the
policy would actually select (for example Jules, first in the pool) is
therefore never blocked merely because a later, unused local fallback's own
prerequisites (missing executable, model, or similar) are not met.
