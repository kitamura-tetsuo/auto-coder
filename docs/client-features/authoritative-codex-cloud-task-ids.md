# Authoritative Codex Cloud task IDs

Codex Cloud task associations use one shared parser across PR metadata, PR bodies,
issue sessions and comments, CI continuation, adversarial feedback, review repair,
and conflict repair. Only the provider-issued `task_e_<token>` shape is accepted;
arbitrary prefixed values such as `task_id` and `task_fake` are rejected and do not
block fallback to a valid provider task URL from a lower-priority source.

New operational links are formatted from the already-selected valid task ID as
`https://chatgpt.com/codex/cloud/tasks/<task-id>`. Readers continue to accept
the historical `/codex/tasks/` route and the current `/codex/cloud/tasks/`
route on supported ChatGPT hosts; normalization does not alter retained raw
provider evidence or rewrite existing GitHub text.
