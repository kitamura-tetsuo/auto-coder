# Authenticated management MCP service

Auto-Coder can run a management-only Streamable HTTP MCP service independently
of an automation worker. This stage exposes only the read-only,
idempotent `auto_coder_whoami` tool; it does not report worker health or provide
worker, update, restart, repository, Docker, or host-command capabilities.

Create a persistent directory outside release/beta state and workspaces. Its
`allowed_uids.json` uses the operator-policy format, and the service creates its
durable `oauth.sqlite3` and rotating `logs/management.log` there. A minimal
production configuration is:

```yaml
state_root: /var/lib/auto-coder-management
public_origin: https://management.example.com
firebase_project_id: example-project
bind_host: 127.0.0.1
port: 8081
allowed_browser_origins: [https://console.example.com]
firebase_web_config:
  apiKey: public-firebase-web-key
  authDomain: example-project.firebaseapp.com
  projectId: example-project
clients:
  - client_id: operator-desktop
    redirect_uris: [http://127.0.0.1:49152/callback]
```

Add the public origin to Firebase Authentication's authorized domains and
register each public client's exact redirect URI. Start only this service with
`auto-coder management serve --config /etc/auto-coder/management.yml`. The
canonical OAuth resource is always `<public_origin>/mcp`; incoming Host and
forwarding headers never change it.

Terminate HTTPS at a trusted proxy and keep the default loopback bind private:

```yaml
services:
  management:
    image: ghcr.io/kitamura-tetsuo/auto-coder:latest
    command: [auto-coder, management, serve, --config, /config/management.yml]
    volumes:
      - ./management.yml:/config/management.yml:ro
      - management-state:/var/lib/auto-coder-management
  proxy:
    image: caddy:2
    ports: ["443:443"]
    volumes: ["./Caddyfile:/etc/caddy/Caddyfile:ro"]
volumes:
  management-state: {}
```

For explicit local development only, set `public_origin` to a loopback HTTP
origin, `bind_host` to loopback, and `local_development: true`. This profile
still requires OAuth bearer authentication.

An MCP client begins at `/mcp`, follows the 401 protected-resource metadata
challenge, discovers the authorization server, opens the Firebase browser
login, and exchanges the authorization code using PKCE S256. It sends the
resulting bearer token on every initialize, list, call, GET, and DELETE request;
reconnecting does not preserve authorization. A 401 means credentials are
missing or invalid, 403 means scope or current operator policy forbids access,
and 503 means authoritative state cannot be read. Grant revocation, refresh
replay, and operator-policy removal affect the next request and survive service
restart. Firebase account disablement or token revocation is checked on renewal;
issued access tokens expire within 900 seconds.
