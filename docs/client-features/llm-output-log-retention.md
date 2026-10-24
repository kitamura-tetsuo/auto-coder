# LLM output log retention

`LLMOutputLogger` bounds its active JSONL file to 10 MiB and retains five
archives by default, limiting the managed family to 60 MiB. The per-instance
construction settings `AUTO_CODER_LLM_OUTPUT_LOG_MAX_BYTES` (integer, minimum
4096) and `AUTO_CODER_LLM_OUTPUT_LOG_BACKUP_COUNT` (nonnegative integer) change
these limits independently; an invalid value receives a bounded diagnostic and
uses only that setting's default. The enable environment variable remains
dynamic and is checked on each public logging call. Writers constructed with
different settings do not establish one immutable global quota.

The public active path remains stable. Owned siblings append
`.auto-coder-archive.<generation>.jsonl`, where the unsigned numeric generation
has at least 20 zero-padded digits, plus `.auto-coder.lock` and the fixed
`.auto-coder-stage.jsonl`. Numeric generation, rather than lexical discovery or
filesystem time, determines archive history; readers process those archives
oldest to newest and the active file last.

An ordinary enriched and redacted record is unchanged when it fits. An
oversized record is replaced by a valid bounded summary containing
`log_record_truncated`, its original encoded byte count, valid public length
counters, and names of invalid omitted counters. Maintenance may discard
malformed lines, incomplete tails, oversized legacy lines, and older segments
outside retention. It keeps each surviving segment's newest complete JSON
objects without moving records between segments.

Custom paths receive the same sibling family and restrictive file permissions.
Disabled construction and calls create none of these paths. This lifecycle is
specific to structured LLM interaction output; separate review-audit storage
is excluded.
