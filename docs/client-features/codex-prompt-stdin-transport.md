# Codex prompt stdin transport

Local Codex executions use non-interactive `codex exec` and provide the exact
prepared task payload through finite UTF-8 stdin with `-` as the prompt operand.
This applies to initial calls, explicit `exec resume` continuations, and the
one-shot exec fallback owned by the persistent MCP client. Configuration,
permission flags, environment, output streaming, session evidence, and final
message capture remain intact; configured competing input sources fail before
the task process starts.
