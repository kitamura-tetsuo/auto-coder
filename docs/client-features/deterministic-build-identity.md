# Deterministic build identity

`scripts/build_version.py` reports the committed-source identity of an explicitly
selected Git checkout without modifying or fetching from it:

```console
python scripts/build_version.py --repo /path/to/auto-coder --output /tmp/build-identity.json
```

The output is schema-version 1 JSON. Clean and dirty records contain the selected
HEAD's full object ID, UTC committer epoch, and the count of all distinct commits
reachable through every parent. Their PEP 440 versions are
`YYYY.M.D.N+g<FULL_SHA>` and `YYYY.M.D.N+g<FULL_SHA>.dirty`, respectively. Tags,
branches, build time, locale, replacement refs, and Git abbreviation settings do
not participate. Ignored files do not make a checkout dirty.

Missing Git, invalid or unborn checkouts, shallow or unreadable history, and failed
Git observations produce an `unknown` record with version `0+unknown`, a diagnostic,
and a nonzero command status. The resolver never fetches to complete history.

`auto_coder.build_identity` exposes the resolver plus strict JSON serialization and
validation APIs. The command writes only to an explicit destination outside the
checkout or ignored by it; tracked and nonignored in-checkout destinations are
rejected before being overwritten. Git metadata directories are always rejected,
including common directories and external indexes used by linked worktrees. It
prints the record only after the output is successfully written. Clean and dirty
results exit zero, unknown results exit one, and output failures exit two.
