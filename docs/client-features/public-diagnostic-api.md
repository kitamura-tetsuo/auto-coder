# Public Read-Only Diagnostic API

The daemon application (`webhook_server.create_app`, the same app that serves the
dashboard) can expose a minimal anonymous JSON API for inspecting Auto-Coder's
existing runtime status and bounded diagnostic events. It is implemented in
`src/auto_coder/public_api.py`.

## Enablement

Set `AUTO_CODER_PUBLIC_API_ENABLED=1` in the daemon's environment before it
starts. With any other value (or none) `GET/POST/... /api/`, `/api/status`,
`/api/logs` and `/api/validation-attempts` return plain 404 and disclose nothing, regardless of method or query.
Enabling publishes data only for the daemon's configured repository; no request
can select another repository, path, URL or command. No login, cookie, token,
OAuth or MCP is involved. Existing dashboard, webhook and operator-write
authorization is unchanged.

## Routes (GET only; other methods return 405)

```bash
curl -s https://HOST/api/                       # entry document: version, repository, process_run_id, routes, meanings
curl -s https://HOST/api/status                 # workers, queue, implementation_slots
curl -s 'https://HOST/api/logs?limit=50'        # newest 50 retained events, ascending sequence
curl -s 'https://HOST/api/logs?item_type=pr&item_number=123'
curl -s "https://HOST/api/logs?item_type=pr&item_number=123&execution_id=$EXEC&process_run_id=$RUN"
curl -s 'https://HOST/api/validation-attempts?pr_number=123&attempt_sequence=42'
curl -s 'https://HOST/api/validation-attempts?pr_number=123&attempt_id=0123456789abcdef0123456789abcdef'
```

Both data routes accept `limit` (integer 1-500, default 100, applied per
collection after filtering). `/api/logs` also accepts the paired `item_type`
(`issue`/`pr`) and `item_number` (positive integer), and `execution_id`, which
requires `process_run_id`. Unsupported parameters, half-specified pairs and
invalid values return 422 with a non-echoing error. A `process_run_id` that is
not the current process run returns 409 `process_run_changed`; a collector read
failure returns 503 `observation_unavailable`.

### `/api/status`

Three independent observations (not an atomic snapshot), each with
`availability` (`available`/`unavailable`), a source `sampled_at` (Unix seconds),
and an `error_code`; unavailable sections carry null data, never an empty or
last-known value. Workers report local occupancy (`idle` is distinct from
unavailable); queue entries keep their typed target (internal jobs such as
`dependency` are not presented as Issues/PRs) and priority. Slots come from the
controller's persisted slot snapshot: normal limit/usage/available, emergency
usage, and per-owner type/number/class, recorded execution IDs, implementation
PRs, opaque provider-session IDs and admission flags (null when not recorded).
Over-capacity usage is not clamped, and a slot stays visible with no local worker. Slots are read through the controller's existing store, or through a detached read-only repository when the controller has no binding yet; an API request never establishes or changes the controller's admission-store binding. A provider-session ID longer than 2,000 characters is never shortened: it is omitted, counted in the owner's `omitted_memberships`, and the slots section is marked `incomplete`.

### `/api/logs`

Returns the repository-scoped structured diagnostic events of this process's
`TraceCollector` (not stdout, log files, LLM transcripts or the legacy unscoped
`TraceLogger` buffer). Each event keeps its recorded identity, sequence,
original timestamp, origin, stage, label, kind and outcome. Only the facts
`reason`, `error`, `phase`, `backend`, `provider`, `attempt_id`, `request_id`,
`provider_task_id`, `head_sha` and `exit_code` are exported. Unscoped structured
events appear only in unfiltered reads and are marked `scope: "unscoped"`.

## `/api/validation-attempts` (durable attempt diagnostics)

Answers "why did this validation attempt report VERIFIED entries yet reject
their IDs?" from the attempt-bound record captured by ordinary PR adversarial
validation (see `validation-attempt-evidence.md`). It is read from the existing
review audit root (`AUTO_CODER_REVIEW_AUDIT_ROOT`, default
`~/.auto-coder/review_audit`) through the exact, read-only
`ReviewAuditStore.get_validation_evidence` lookup, so a committed record stays
selectable after the serving process restarts. It is not a log tail and
`/api/logs` is not turned into a durable archive.

Parameters: positive integer `pr_number` and **exactly one** of `attempt_id`
(the native 32-lowercase-hex attempt ID, as in the
`auto-coder-adversarial-validation-attempt:v1:<sequence>:<id>` PR marker) or
positive integer `attempt_sequence`. Neither, both, duplicate, unknown (for
example `repository`, `path`, `url`, `command`), malformed or oversized values
return a non-echoing 422. No newest-record guess, substring match, history scan or
current-GitHub reconstruction ever stands in for a missing exact match.

`result` values (HTTP 200): `attempt` (a recorded attempt; `evidence` set),
`no_retained_match` (nothing retained for that exact repository/PR/attempt; this
does **not** prove the attempt never occurred), `evidence_unavailable` (a
pre-feature audit retained the attempt without the extension; reason
`pre_feature_audit_record`; `evidence` carries the known attempt/review identity and
evaluated head, with every diagnostic section `unavailable` / `pre_feature_capture_not_recorded`) and `audit_not_initialized` (no audit exists yet; the
read never creates it). An unreadable, corrupt, unsupported or unprojectable
source returns 503 `observation_unavailable` with `data: null` and no source
detail; there is no fallback to another attempt or a cached/empty success.

An `attempt` carries (typed allowlist only):

* `identity`: exact attempt ID/sequence, review ID, repository/PR, evaluated head
  and base, and the producing process/execution when recorded
  (`identity.unavailable` lists what was not).
* `producing_artifact`: the installed controller version and embedded source
  revision recorded by the **producing** process at capture (with origin and
  explicit unavailable reasons) — never the reviewed PR head or latest main.
  `serving` (top level) separately gives the process that answered this request
  and its sampling time; a restart never relabels an old record.
* `input`: SHA-256/byte-length fingerprints of the consumed PR body, each resolved
  Issue body and the linked-Issue context, with observed source-time/retrieval
  provenance. `captured_at` is capture time, not freshness.
* `manifests` / `responses` / `coverage_checks`: supplied and checked manifest
  mode, ordered IDs, per-entry text digests, counts and identity digest; per
  response its stage, prompt/response fingerprints, backend/model provenance (only
  what was verified), parse state, model verdict and the returned `id/status`
  entries exactly as parsed; per check the expected/returned/missing/duplicate/
  unknown IDs, `verdict_before`/`verdict_after` and diagnostic category/reason,
  bound to its `response_id`; `final` with the result and `source_response_id`.
* `effects` and `reuse_observations`, attributed to their own review rows.

Meanings: returned `VERIFIED` counts, a recorded `PASS` or a confirmed
publication are recorded observations, never accepted coverage, provider
liveness, merge permission or current GitHub state. Sections can be
`not_recorded` (interrupted/partial capture), `omitted` (capture could not fit
it) or `unavailable`; none of these is "empty" or "complete". Hashes are
comparison fingerprints of capture-time input, not links to raw content; no
Issue/PR bodies, requirement text, prompts, diffs, raw responses, paths, URLs,
credentials or dereferencing endpoint are exposed.

Bounds: at most 256 KiB, 500 entries per collection and 2,000 characters per text.
Each collection reports `source_count`, `retained_in_source`, `returned`,
`source_omitted` (capture-time loss, also in `source_limits`) and `http_clipped`
(additional clipping by this response, also in `http_limits`) plus `incomplete`.
Redaction (credential patterns, Bearer/Basic, URLs, paths) runs before clipping.
Identities are exact or replaced by `{"omitted": true, "reason", "sha256",
"byte_length"}` — never shortened. Only requirement IDs matching the supported
grammar (`REQ-001`, `#99/REQ-001`, `owner/repo#99/REQ-001`) are plaintext;
other returned IDs are digest/length only, and when any ID in the record is not
plaintext, every `diagnostic_reason` (which can quote IDs) is withheld and replaced
by `diagnostic_reason_omitted` (digest/length). A section's `state.incomplete` is
true when its own, a nested collection's, or the capture's omission ledger shows loss. When the body is too large, entry caps
shrink stepwise while the exact attempt identity, verdicts and availability
states are kept. The route never mutates the audit, calls GitHub/providers/LLMs,
validates or repairs anything, and its synchronous SQLite read runs off the event loop.

## Incremental reads (`after_sequence` / `cursor`)

Recent-tail requests (no `after_sequence` and no `cursor`) are unchanged: they
return the newest matching events and may clip older ones. To read retained
events from a checkpoint without skipping any, opt in to incremental mode:

```bash
RUN=$(curl -s https://HOST/api/ | jq -r .process_run_id)
# Initial request: fixes the upper bound H (snapshot_upper_sequence) for the traversal.
curl -s "https://HOST/api/logs?after_sequence=290&process_run_id=$RUN&limit=500"
# Optional paired item_type/item_number and exact execution_id narrow the same way as recent reads.
curl -s "https://HOST/api/logs?after_sequence=290&process_run_id=$RUN&item_type=pr&item_number=123"
# Continue with the opaque cursor (only `limit` may accompany it) until has_more is false.
curl -s "https://HOST/api/logs?cursor=$NEXT_CURSOR"
```

`after_sequence` is a nonnegative integer, requires `process_run_id`, and may not
exceed the current `sequence_high_watermark` (otherwise 422). A cursor binds the
run, repository, selection, fixed `H` and consumed boundary; mixing it with
`after_sequence`, `process_run_id` or selectors is 422, as are malformed,
oversized, duplicate or unsupported parameters (bounded, non-echoing errors).

Each page returns the oldest unread matching events with sequence in
`(boundary, H]`, in increasing order, at most `limit` (1-500, default 100) events
and 256 KiB. A count or byte limit leaves the unread suffix for the next page and
never advances past it. Fields: `snapshot_upper_sequence`, `next_after_sequence`
(the safely consumed boundary, `H` when exhausted, including an interval with no
matching events), `has_more`, `next_cursor` (null when exhausted),
`response.truncated` (page-bound clipping, distinct from source `events_truncated`),
`retention` (current high-water, oldest retained and discarded-through sequences)
and `omissions`. Events published after the initial request have sequences above
`H` and appear in a later initial request starting at `H`.

Safe checkpointing: the server keeps no session and consumes nothing for you.
Persist `next_after_sequence` only after you processed the page; retrying the same
checkpoint or cursor may return the same events while they remain retained.

Failure and gap states:

- `409 process_run_changed`: the daemon restarted; the old run cannot be continued.
- `409 retention_gap`: for a pending interval, records after the checkpoint may
  have been evicted or cleared (`discarded_through_sequence` is greater than the
  checkpoint). Coverage of your filter is unknown; no events or next checkpoint are
  returned. Numeric holes, non-matching records and a historical `events_truncated`
  flag alone are not a gap, and a completed interval (checkpoint equals `H`) is
  never refused for retention. Retain the gap in your own records; you may
  explicitly choose a new baseline (for example `after_sequence=<sequence_high_watermark>`).
- `omissions`: matching records consumed but not returned, each with `reason`
  (`identity_unrepresentable`, `representation_exceeds_response_bound`), `count`
  and the scanned `first_sequence`/`last_sequence`; the page is then
  `response.incomplete`. Identities are never shortened and raw content is not exposed.
- `503 observation_unavailable`: the collector could not be read consistently or a
  projection failed; no cursor is returned, retry from the same checkpoint.

Finishing an interval means only that its retained matching diagnostic evidence
was consumed. It is not overall health, provider liveness, or proof that every
business operation was recorded, and it provides no durable history or continuity
guarantee across restart or source eviction. Reads stay anonymous, repository-scoped,
non-mutating, and run off the event loop. Bounds and redaction are identical to
recent-tail reads.

## Bounds and redaction

At most 500 entries per collection, 2,000 characters per text value and 256 KiB
per body. `response.truncated`/section `truncated` mark response clipping,
distinct from the source `events_truncated`/`execution_metadata_truncated`
retention flags; `text_truncated` marks clipped text and `filtered` marks
redaction. Identity fields are never truncated; an event whose identity cannot be
represented exactly is omitted and counted in `omitted_unrepresentable`.
Redaction runs before clipping: GitHub/Google/OpenAI-style/AWS/Slack/GitLab token
patterns, `Bearer`/`Basic` values, and HTTP(S) URLs are replaced with markers, and
absolute POSIX/home/Windows filesystem paths in event labels and facts become
`[REDACTED_PATH]` (identity fields are never rewritten).
This is bounded hygiene for the exported fields, not universal secret discovery.

## Limitations and interpretation

Events are process-local and bounded: they reset on restart and older records
are evicted. `no_retained_match` does not mean an item does not exist, nothing
ran, or everything is healthy; HTTP 200, an empty queue or a recorded
`completed` outcome is not proof of overall health, and sampling time is not
progress. Compare these local observations with GitHub independently and treat
missing evidence as unknown. The API does not detect anomalies or guarantee
continuous coverage. Data recorded or code deployed normally is visible at the
same routes on the next request; no tool registration is involved.

## Collector retention-continuity evidence

`TraceCollector.get_snapshot()` also returns collector-wide continuity fields,
captured under the publication lock together with `process_run_id` and the
retained events, and computed before any item/repository filter or limit:

- `sequence_high_watermark`: greatest sequence allocated in this process run
  (0 initially). A sequence consumed by a failed publication counts, so numeric
  holes are not evidence that a matching event was lost.
- `oldest_retained_sequence`: least retained event sequence, `null` when the
  buffer is empty.
- `discarded_through_sequence`: greatest sequence of an actually stored event
  removed by eviction or `clear()` (0 initially). It never decreases; `clear()`
  keeps the run ID and sequence counter, so an old checkpoint cannot look
  complete again.

A fresh collector (restart) has a new `process_run_id` and zero/null state.
These values describe stored process-local diagnostics only: not durable
history, not proof that every business operation produced an event, and not a
per-target loss verdict. The existing `events_truncated` flags keep their
meaning. Reading them mutates no operational state.

Observability checklist: no event emission, event schema or dashboard rendering
changes; only snapshot metadata is added, so this is observability-neutral.

Incremental pages only consume this snapshot metadata through the existing HTTP
adapter: no business origin, outcome or event-emission authority is added, so the
change is observability-neutral for `docs/dashboard-observability.md`.
