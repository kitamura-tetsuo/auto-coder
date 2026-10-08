# Muse MSP Invocation Diagnostics

Every call that enters the Muse adapter's turn-execution path (fresh or explicit continuation, default or named `backend_type = "muse"` backends, editable or no-edit) produces a bounded, metadata-only diagnostic timeline. It localizes where Auto-Coder is waiting and which transport, turn and host-lifecycle observations it has actually received. It changes no execution behavior: no timeout, retry, fallback, probe, ping or extra provider operation is added, and a failing or rejecting diagnostic sink never replaces the primary error or the result.

## Record format

Records go through the application's existing Loguru configuration as one line each: the fixed marker `Muse diagnostic: ` followed by a single-line JSON object (`"schema": 1`). Kinds are `start`, `phase`, `heartbeat` and `end`. `start`, `phase`, `heartbeat` and a returned `end` are INFO; a raised or interrupted `end` is WARNING. The console line may carry ANSI colors outside the JSON, so strip them before parsing. No sink is added per call: records follow the console stream selected by `setup_logger` (stdout with `AUTOCODER_VERBOSE=1`, otherwise stderr), the application log file (default `~/.auto-coder/logs/auto-coder.log`, `--log-file`/`AUTO_CODER_LOG_FILE`), their log levels (`AUTO_CODER_FILE_LOG_LEVEL`) and rotation, and are absent from a file sink disabled with `LLM_LOGGING_DISABLED=1`. Nothing is written to the MSP streams, the returned answer or the workspace.

Every record carries `diagnostic_id` (distinct per call; never a session, workspace, retry or result authority), an increasing per-call `seq`, a UTC `ts` and `elapsed_s` (monotonic). Group concurrent workers sharing a log by `diagnostic_id` and order by `seq`. `context` carries what was available: backend alias/type, model, reasoning effort, effective `edit`/`no-edit` mode, fresh/continuation, controller PID, host PID (only after the host process exists), CLI version from the constructor's `--version` probe, host version and schema metadata (when initialized), and the controller `execution_id`/`invocation_id` when set. Missing values read `unavailable`. `identity` distinguishes `requested_session` (what the caller asked to continue, or `none-fresh`), `confirmed_session` (confirmed by this call's start/resume response) and `turn` (acknowledged for this call); `unobserved` means not seen.

Strings are redacted, control characters escaped and each value is limited to 128 characters with a `...[truncated]` suffix. A payload never exceeds 8192 UTF-8 bytes; optional detail is shed before core status and correlation fields. Records contain no prompts, assistant/tool text, reasoning, raw frames, raw stderr, exception messages, command arguments, environment or credentials.

## Phases and cadence

`phase` records mark the first entry into each reached phase, before its blocking operation: `preparation`, `host_startup`, `initialization`, `session_start_resume`, `approval_mode_change` (only when requested), `turn_submission`, `turn_terminal_wait`, `post_terminal_host_exit_wait`, `writer_settlement`, `result_validation`. Skipped phases are not fabricated.

A `heartbeat` snapshot is emitted every 30 seconds of monotonic time while a monitored operation is active: a pipe write, a wait for an MSP response or turn terminal, or the post-terminal wait for the host to exit. The schedule is anchored at the first monitored operation of the call; phase changes, stderr, partial stdout or continuous notifications do not reset it, and a delayed wake-up emits one current snapshot, not a burst. Reporting wake-ups are not timeouts: the unchanged execution and pending-approval deadlines still decide expiry. Outside monitored operations (including arbitrary controller suspension) no periodic snapshot is produced, and this feature is not an external watchdog or recovery mechanism.

A snapshot contains `wait` (reason `pipe_write`, `response_wait`, `notification_wait` or `host_exit_wait`; the latest client request with `bytes_written`/`total_bytes`, `write` = `not_started|partial|complete`, `response_received`, `ack_accepted`; ids are typed `int`/`str` with a direction), `activity` (separate cumulative stdout bytes, stderr bytes and decoded frames with first/last UTC and monotonic `age_s`, `stdout_buffered_bytes`, EOF flags, last frame category and recognized method or `other`), `milestones`, `host` (state `not_started|running|exited|unknown`, PID, exit code/signal, `stdin_closed`, `stdout_eof`, `writers`) and `budgets` (remaining execution and pending-approval seconds). Times are controller receive/decode times, never network-arrival or model-generation times; never-observed values are `null`/`unobserved`.

`milestones.assistant_text` and `milestones.turn_terminal` record the first local decode time of a completed assistant item and of the turn terminal for the confirmed session and acknowledged turn (terminal normalized to `completed|failed|cancelled|unknown`). Evidence that arrives before the turn acknowledgement is `unconfirmed` and keeps its original decode time once confirmed (`before_ack: true`). Later items or duplicate terminals never reset them. They do not change which message the adapter returns.

The single `end` record is attempted after all handled cleanup and validation and reports `summary.outcome` (`returned|raised|interrupted`), the propagated `exception_class`, per-phase seconds, `primary_failure` (phase, class, bounded category) separately from `secondary_failures` (cleanup/validation), and a correlated numeric `rpc_error_code` when one was observed. `returned` means only that the adapter returned normally.

## Reading a timeline

* Pending write: `wait.reason = pipe_write`, `write = partial`, fewer `bytes_written` than `total_bytes`; a complete write still does not mean the host accepted the request (`response_received = false`).
* No stdout: `stdout.bytes = 0` or an old `stdout.age_s` with `response_received = false`.
* Partial/undecoded stdout: `stdout_buffered_bytes > 0` while `frames.count`/`frames.age_s` do not advance.
* Unrelated-frame activity: `frames.count` grows with `last_method = other` (or unrelated methods) while the milestones stay `unobserved` and `response_received = false`.
* Text without terminal: `assistant_text.state = observed`, `turn_terminal.state = unobserved`, phase `turn_terminal_wait`.
* Terminal received but host alive: phase `post_terminal_host_exit_wait`, `turn_terminal.terminal = completed`, `host.state = running`, `stdin_closed = true`.
* Cleanup failure: `summary.secondary_failures` or a `primary_failure.phase` of `writer_settlement`/`result_validation` after observed text and terminal; `host.writers = confirmed|failed` is separate from host exit.

## Limits

Auto-Coder observes only its pipes to the local Muse host, not the host's HTTP/SSE connection to Meta (`boundary.provider_transport = unobserved`). Silence, byte counts, an alive PID, a heartbeat or model text cannot establish a Meta outage, that the model is still reasoning, or a CLI defect. A call that ends abruptly (killed controller, unavailable logging) leaves only earlier records; the absence of an `end` record is inconclusive, never success. Operators can share these records without prompts, credentials or raw session logs.
