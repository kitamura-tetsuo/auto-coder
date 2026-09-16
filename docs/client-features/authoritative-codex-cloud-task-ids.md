# Authoritative Codex Cloud task IDs

Codex Cloud task associations use one shared parser across PR metadata, PR bodies,
issue sessions and comments, CI continuation, adversarial feedback, review repair,
and conflict repair. Only the provider-issued `task_e_<token>` shape is accepted;
arbitrary prefixed values such as `task_id` and `task_fake` are rejected and do not
block fallback to a valid provider task URL from a lower-priority source.
