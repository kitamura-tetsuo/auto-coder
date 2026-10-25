# Management OAuth browser authorization

`create_management_auth_app` publishes a standalone OAuth authorization server for
pre-registered public clients. Configure an issuer origin, a Firebase project and
its public Web configuration, and exact client redirect URIs. The sole resource is
`<issuer>/mcp` and the sole permission is `auto-coder.read`. Production issuers and
callbacks use HTTPS; explicit-port loopback callbacks and an HTTP localhost issuer
are intended only for development and tests.

Discovery is at `/.well-known/oauth-authorization-server`. Public clients send
`client_id` without a secret to `/token` and `/revoke`. Authorization-code requests
use S256 PKCE and the exact redirect and resource. Refresh requests may omit scope
to retain read access; reusing a rotated refresh token revokes the grant family and
requires a fresh browser login.

The browser uses the configured Firebase Google provider. Selecting an identity is
not consent: the user must separately choose **Allow**, or may choose **Deny**. The
server verifies the Firebase ID token and current operator policy through the
caller-owned grant authority before issuing a code.

This endpoint only authorizes management access. Protected MCP transport,
management update/restart commands, and task lifecycle tools are not implemented by
this application factory.
