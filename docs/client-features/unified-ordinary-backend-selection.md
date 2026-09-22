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
