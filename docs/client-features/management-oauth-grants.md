# Management OAuth grants

The management service owns a durable SQLite OAuth authority outside disposable
worker workspaces.  Its configured issuer, Firebase project, and exact MCP resource
are recorded in the database; a database cannot be reopened under a different
authority.  Deployments use an HTTPS origin and `<origin>/mcp`, except that an
explicit localhost development profile may use HTTP.  Clients are pre-registered
public clients with exact redirects and S256 PKCE. Scope configuration and delayed
operation authority are documented in `management-action-scopes.md`.

Authorization requests expire within ten minutes and codes within two minutes.
Codes are single-use. Access tokens last at most fifteen minutes, while grants and
their rotating refresh families have a non-renewable thirty-day maximum lifetime.
Losing a successful code or refresh response requires authorization again. Reusing
a spent refresh credential revokes its entire family, as does revoking either an
access or refresh token. Restarting the service preserves pending, denied,
consumed, spent, active, expired, and revoked state.

Codes, tokens, browser bindings, and CSRF values are opaque random values; only
SHA-256 hashes are stored. Raw Firebase credentials are neither stored nor logged.
The database and configuration directory are trusted management-owned state and
must be included in operator backup and access-control procedures.

Protocol adapters call `OAuthGrantService` to begin, approve, deny, exchange,
refresh, revoke, inspect, and authenticate. They translate its typed OAuth errors
without logging credential-bearing requests. Access authentication returns an
immutable Firebase project/UID, actual client, resource, scopes, and expiry. It
freshly checks local operator policy, while Firebase account status is checked only
when authority is issued or renewed. Browser pages, HTTP/MCP transport, worker
dispatch, updates, restarts, webhooks, diagnostics, and existing backend MCP
behavior remain outside this service.
