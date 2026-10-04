# Public Read-Only Diagnostic API

The daemon application (`webhook_server.create_app`, the same app that serves the
dashboard) can expose a minimal anonymous JSON API for inspecting Auto-Coder's
existing runtime status and bounded diagnostic events. It is implemented in
`src/auto_coder/public_api.py`.

## Enablement

Set `AUTO_CODER_PUBLIC_API_ENABLED=1` in the daemon's environment before it
starts. With any other value (or none) `GET/POST/... /api/`, `/api/status` and
`/api/logs` return plain 404 and disclose nothing, regardless of method or query.
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

## Bounds and redaction

At most 500 entries per collection, 2,000 characters per text value and 256 KiB
per body. `response.truncated`/section `truncated` mark response clipping,
distinct from the source `events_truncated`/`execution_metadata_truncated`
retention flags; `text_truncated` marks clipped text and `filtered` marks
redaction. Identity fields are never truncated; an event whose identity cannot be
represented exactly is omitted and counted in `omitted_unrepresentable`.
Redaction runs before clipping: GitHub/Google/OpenAI-style/AWS/Slack/GitLab token
patterns, `Bearer`/`Basic` values, and HTTP(S) URLs are replaced with markers.
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
