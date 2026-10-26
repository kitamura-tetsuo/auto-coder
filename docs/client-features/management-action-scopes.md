# Management action scopes

Management OAuth recognizes exactly `auto-coder.read`, `auto-coder.operate`, and
`auto-coder.deploy`. The service and each public client default to read only.
Operators enable wider consent explicitly with `enabled_scopes` and a matching
`allowed_scopes` client entry; every requested set must contain read and is
accepted exactly rather than intersected. Existing grants retain their original
authority when configuration changes, so additional privileges always require a
new browser authorization and explicit Allow.

For example, enabling deployment for one client is explicit at both boundaries:

```yaml
enabled_scopes: [auto-coder.read, auto-coder.deploy]
clients:
  - client_id: operator-console
    redirect_uris: [https://console.example/callback]
    allowed_scopes: [auto-coder.read, auto-coder.deploy]
```

The consent page labels the actual requested read, runtime-operation, and
deployment capabilities. Refresh may preserve or narrow the token scope but never
widen the durable consent. MCP tool registrations bind a fixed required set to a
tool name, and admission checks the actual access-token set plus current service
and client policy before dispatch. The built-in identity tool remains read-only;
this feature registers no worker, container, GitHub, or deployment effect.

An accepted operation may retain the non-secret `AcceptedAuthority` produced by a
successful bearer admission. Immediately before delayed dispatch, the internal
checker rereads the durable grant and current exact-UID policy. Access-token expiry
alone does not invalidate already accepted work, but revocation, grant expiry,
binding mismatch, removed consent, disabled current configuration, or UID removal
does. This check is not an endpoint or credential and does not authorize a wider
action. OAuth scope is also distinct from any later observe/beta/full per-channel
operating policy and does not prove that an effect succeeded.
