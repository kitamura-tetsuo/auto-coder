# Controller artifact observation

`auto_coder.build_provenance.observe_controller_artifact()` returns a versioned
(`schema_version: 1`), read-only observation of the Auto-Coder artifact running
in the current controller process. It is the producer side of later
attempt-bound diagnostics and does not itself gate, dispatch or validate
anything.

## Fields

Each field carries `value`, `origin`, `available` and an explicit `reason` when
unavailable.

* `process_run_id` / `process_run`: the existing `TraceCollector.process_run_id`
  of this process. A new controller process has its own identity; an earlier
  process's copied observation is never rewritten.
* `distribution_version`: the installed `auto-coder` distribution version
  (origin `installed_distribution_metadata`). It is independent of the source
  revision and is never treated as a commit.
* `source_revision`: the full commit SHA that was the trusted
  `AUTO_CODER_SOURCE_REVISION` build input (origin `build_embedded`), or `null`.

## Production positive path

The production `Dockerfile` runs `python -m auto_coder.build_provenance embed`
after installing the wheel. It writes `build_provenance.json` into the installed
package from the existing `AUTO_CODER_SOURCE_REVISION` build arg; the Publish
Beta workflow keeps supplying `github.sha` to that arg, and the OCI revision
label is unchanged. A valid 40- or 64-hex SHA is recorded; anything else
(including the `unknown` default) is recorded as `null`.

## Unknown fallback

The record is resolved from the executed installation, never from the working
directory, target repository/branch/PR head, remote main, launch environment,
GitHub, Git or Docker. A missing (non-container or pre-feature install),
unreadable, malformed, unsupported-schema or invalid record yields
`source_revision.value = null` with a reason, leaving other fields usable. No
image digest is reported.

## Distinctions and safety

The embedded revision identifies what was built, not the reviewed PR head, not
that an image was published, and not that it was deployed. Reading performs no
network request, Docker control, install or state mutation, never raises, is not
a readiness/merge gate, and exposes no paths, environment or secrets.

## Observability

No processing origin, admission gate, outcome, routing or structured event
schema changed, so `docs/dashboard-observability.md` needs no update:
the observation is a standalone read helper with no production trace emission
yet (its consumer is a later change).
